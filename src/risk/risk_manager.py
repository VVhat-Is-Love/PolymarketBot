"""
Central risk controller — all trading decisions pass through here.
State is held in memory and refreshed from DB before each check/display.
Emergency stop is persisted to the `bot_state` table so it survives restarts.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger


@dataclass
class RiskStatus:
    daily_pnl: float
    daily_loss: float           # absolute value of today's losses
    daily_bets_count: int
    concurrent_open_bets: int
    total_loss: float           # cumulative all-time losses
    emergency_stop: bool
    emergency_stop_reason: str
    emergency_stop_type: str    # 'manual' | 'daily_loss' | 'total_loss' | ''
    max_single_bet_usd: float
    max_daily_bets: int
    max_concurrent_bets: int
    daily_loss_limit_usd: float
    total_stop_loss_usd: float
    # Deployed capital by strategy
    basket_deployed_usd: float = 0.0
    tail_deployed_usd: float = 0.0
    total_deployed_usd: float = 0.0
    total_deployed_cap_usd: float = 0.0
    basket_max_usd: float = 0.0
    tail_max_usd: float = 0.0


class RiskManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._load_settings()
        self._reset_daily_state()
        self._total_loss: float = 0.0
        self._emergency_stop: bool = False
        self._emergency_stop_reason: str = ""
        self._emergency_stop_type: str = ""   # 'manual' | 'daily_loss' | 'total_loss'
        # group_id → bet_size_usd for currently open bets
        self._open_bets: dict[str, float] = {}
        # In-session reservations: notional pre-approved but not yet in DB
        self._reserved_usd: float = 0.0
        self._basket_legs_open: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        from src.config.settings import settings
        self._max_single_bet_usd = settings.max_single_bet_usd
        self._max_daily_bets = settings.max_daily_bets
        self._max_concurrent_bets = settings.max_concurrent_bets
        self._daily_loss_limit_usd = settings.daily_loss_limit_usd
        self._total_stop_loss_usd = settings.total_stop_loss_usd
        self._max_basket_legs_open = settings.max_basket_legs_open
        self._max_tail_positions = settings.max_tail_positions
        self._total_deployed_cap_usd = settings.total_deployed_cap_usd
        self._basket_max_usd = settings.basket_max_usd
        self._tail_max_usd = settings.tail_max_usd

    def _reset_daily_state(self) -> None:
        self._today = date.today()
        self._daily_pnl: float = 0.0
        self._daily_loss: float = 0.0
        self._daily_bets_count: int = 0

    def _maybe_roll_day(self) -> None:
        if date.today() != self._today:
            logger.info("RiskManager: rolling daily state for new UTC day")
            self._reset_daily_state()

    def _load_live_stats(self) -> None:
        """
        Refresh counters from DB so they survive process restarts.
        Also loads emergency_stop from bot_state table.
        Safe to call inside self._lock; catches all errors.
        """
        try:
            from sqlalchemy import select, func, or_
            from src.db.session import get_session
            from src.db.models import LiveTrade, BotState, MarketGroup

            today_start = datetime.combine(date.today(), datetime.min.time())
            session = get_session()
            try:
                # -- Emergency stop (DB-persistent) --
                stop_row = session.get(BotState, "emergency_stop")
                if stop_row and stop_row.value == "1":
                    self._emergency_stop = True
                    reason_row = session.get(BotState, "emergency_stop_reason")
                    self._emergency_stop_reason = reason_row.value if reason_row else "DB-persisted stop"
                    type_row = session.get(BotState, "emergency_stop_type")
                    self._emergency_stop_type = type_row.value if type_row else "manual"
                elif not self._emergency_stop:
                    # Only clear in-memory if it wasn't set locally this session
                    pass  # keep in-memory flag if set

                # -- Daily bet count (all trades placed today) --
                self._daily_bets_count = int(
                    session.execute(
                        select(func.count()).select_from(LiveTrade).where(
                            LiveTrade.placed_at >= today_start
                        )
                    ).scalar() or 0
                )

                # -- Concurrent open / pending positions --
                # Exclude positions on deactivated (Gamma-resolved) groups: a trade on
                # a group with is_active=False has curPrice ∈ {0,1} and is awaiting
                # reconciliation, not a live open position.
                rows = session.execute(
                    select(LiveTrade.group_id, LiveTrade.id, LiveTrade.stake_usd)
                    .join(MarketGroup, LiveTrade.group_id == MarketGroup.group_id, isouter=True)
                    .where(
                        LiveTrade.status.in_(["pending", "open"]),
                        # is_active NULL (no group row) or True → passes; False → excluded
                        MarketGroup.is_active.is_not(False),
                    )
                ).all()
                self._open_bets = {
                    (r.group_id or r.id): (r.stake_usd or 0.0) for r in rows
                }

                # -- Daily loss (trailing 36h window, reconciled live trades only) --
                cutoff_36h = datetime.utcnow() - timedelta(hours=36)
                v = session.execute(
                    select(func.coalesce(func.sum(LiveTrade.pnl_usd), 0.0)).where(
                        LiveTrade.resolved_at >= cutoff_36h,
                        LiveTrade.pnl_usd < 0,
                        LiveTrade.reconciled_with_polymarket == True,  # noqa: E712
                    )
                ).scalar() or 0.0
                self._daily_loss = abs(float(v))

                # -- Total loss (all-time, reconciled live trades only) --
                v = session.execute(
                    select(func.coalesce(func.sum(LiveTrade.pnl_usd), 0.0)).where(
                        LiveTrade.pnl_usd < 0,
                        LiveTrade.reconciled_with_polymarket == True,  # noqa: E712
                    )
                ).scalar() or 0.0
                self._total_loss = abs(float(v))

                # -- Daily PnL (resolved today) --
                v = session.execute(
                    select(func.coalesce(func.sum(LiveTrade.pnl_usd), 0.0)).where(
                        LiveTrade.resolved_at >= today_start,
                        LiveTrade.pnl_usd.isnot(None),
                    )
                ).scalar() or 0.0
                self._daily_pnl = float(v)

            finally:
                session.close()

        except Exception as exc:
            logger.warning(
                f"RiskManager: DB sync failed — using cached in-memory values: {exc}"
            )

    def _persist_emergency_stop(
        self, active: bool, reason: str = "", stop_type: str = "manual"
    ) -> None:
        """Write emergency stop state to bot_state table."""
        try:
            from src.db.session import get_session
            from src.db.models import BotState

            session = get_session()
            try:
                now = datetime.utcnow()
                session.merge(BotState(key="emergency_stop", value="1" if active else "0", updated_at=now))
                session.merge(BotState(key="emergency_stop_reason", value=reason, updated_at=now))
                session.merge(BotState(key="emergency_stop_type", value=stop_type if active else "", updated_at=now))
                session.commit()
            finally:
                session.close()
        except Exception as exc:
            logger.error(f"RiskManager: failed to persist emergency stop to DB: {exc}")

    def _get_deployed_by_strategy(self) -> dict[str, float]:
        """DB query: sum of stake_usd per strategy for non-resolved trades."""
        try:
            from sqlalchemy import select, func
            from src.db.session import get_session
            from src.db.models import LiveTrade

            session = get_session()
            try:
                rows = session.execute(
                    select(
                        LiveTrade.strategy_name,
                        func.coalesce(func.sum(LiveTrade.stake_usd), 0.0),
                    )
                    .where(LiveTrade.status.in_(["pending", "open", "filled"]))
                    .group_by(LiveTrade.strategy_name)
                ).all()
                return {name: float(val) for name, val in rows}
            finally:
                session.close()
        except Exception as exc:
            logger.warning(f"RiskManager: _get_deployed_by_strategy failed: {exc}")
            return {}

    def _log_loss_breakdown(self, stop_type: str, total_amount: float) -> None:
        """Query and log the individual trades that contributed to the triggering loss."""
        try:
            from sqlalchemy import select
            from src.db.session import get_session
            from src.db.models import LiveTrade

            session = get_session()
            try:
                if stop_type == "daily_loss":
                    cutoff = datetime.utcnow() - timedelta(hours=36)
                    rows = session.execute(
                        select(LiveTrade.id, LiveTrade.city, LiveTrade.pnl_usd, LiveTrade.resolved_at)
                        .where(
                            LiveTrade.resolved_at >= cutoff,
                            LiveTrade.pnl_usd < 0,
                            LiveTrade.reconciled_with_polymarket == True,  # noqa: E712
                        )
                    ).all()
                else:
                    rows = session.execute(
                        select(LiveTrade.id, LiveTrade.city, LiveTrade.pnl_usd)
                        .where(
                            LiveTrade.pnl_usd < 0,
                            LiveTrade.reconciled_with_polymarket == True,  # noqa: E712
                        )
                    ).all()
                detail = " | ".join(
                    f"#{r.id} {r.city or '?'} ${r.pnl_usd:.4f}" for r in rows
                ) or "(none)"
                logger.warning(
                    f"RiskManager [{stop_type}] total=${total_amount:.4f} "
                    f"from {len(rows)} trade(s): {detail}"
                )
            finally:
                session.close()
        except Exception as exc:
            logger.warning(f"RiskManager: _log_loss_breakdown failed: {exc}")

    def _trigger_total_stop(self, reason: str) -> None:
        """Edge-triggered: fires once on 0→1 transition; skips alert if already stopped."""
        if self._emergency_stop:
            return
        self._log_loss_breakdown("total_loss", self._total_loss)
        self._emergency_stop = True
        self._emergency_stop_reason = reason
        self._emergency_stop_type = "total_loss"
        self._persist_emergency_stop(True, reason, "total_loss")
        try:
            from src.config.settings import settings
            if settings.trading_mode.lower() == "live":
                from src.notifications.telegram import get_notifier
                get_notifier().send(f"🛑 TOTAL STOP-LOSS: {reason}")
        except Exception:
            pass

    def _trigger_daily_loss_stop(self, reason: str) -> None:
        """Edge-triggered: activate daily-loss emergency stop (auto-cleared next trading day)."""
        if self._emergency_stop:
            return
        self._log_loss_breakdown("daily_loss", self._daily_loss)
        self._emergency_stop = True
        self._emergency_stop_reason = reason
        self._emergency_stop_type = "daily_loss"
        self._persist_emergency_stop(True, reason, "daily_loss")
        try:
            from src.config.settings import settings
            if settings.trading_mode.lower() == "live":
                from src.notifications.telegram import get_notifier
                get_notifier().send(f"⚠️ DAILY LOSS LIMIT: {reason}")
        except Exception:
            pass

    def is_emergency_stopped(self) -> bool:
        """Check DB directly — safe to call at startup before _load_live_stats."""
        try:
            from src.db.session import get_session
            from src.db.models import BotState

            session = get_session()
            try:
                row = session.get(BotState, "emergency_stop")
                return row is not None and row.value == "1"
            finally:
                session.close()
        except Exception as exc:
            logger.warning(f"RiskManager: is_emergency_stopped DB check failed: {exc}")
            return self._emergency_stop  # fall back to in-memory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_place_basket(self, notional: float, n_legs: int) -> tuple[bool, str]:
        """
        Check if we can deploy `notional` USD across `n_legs` basket bins.
        Checks: emergency stop, daily loss, total deployed cap, basket cap, leg slots.
        Reserves the notional in-memory until release_basket_reservation() is called.
        """
        with self._lock:
            self._maybe_roll_day()
            self._load_live_stats()

            if self._emergency_stop:
                return False, f"Emergency stop: {self._emergency_stop_reason}"

            if self._daily_loss >= self._daily_loss_limit_usd:
                self._trigger_daily_loss_stop(
                    f"Daily loss ${self._daily_loss:.2f} >= limit ${self._daily_loss_limit_usd:.2f}"
                )
                return False, f"Daily loss limit reached: ${self._daily_loss:.2f}"

            if self._total_loss >= self._total_stop_loss_usd:
                reason = (
                    f"Total loss ${self._total_loss:.2f} >= "
                    f"TOTAL_STOP_LOSS ${self._total_stop_loss_usd:.2f}"
                )
                self._trigger_total_stop(reason)
                return False, reason

            deployed = self._get_deployed_by_strategy()
            basket_deployed = (
                deployed.get("basket_wide", 0.0) + deployed.get("basket_narrow", 0.0)
            )
            total_deployed = sum(deployed.values()) + self._reserved_usd

            if total_deployed + notional > self._total_deployed_cap_usd:
                return False, (
                    f"total_cap ${total_deployed + notional:.2f}"
                    f">${self._total_deployed_cap_usd:.2f}"
                )

            if basket_deployed + notional > self._basket_max_usd:
                return False, (
                    f"basket_cap ${basket_deployed + notional:.2f}"
                    f">${self._basket_max_usd:.2f}"
                )

            if self._basket_legs_open + n_legs > self._max_basket_legs_open:
                return False, (
                    f"basket_slots {self._basket_legs_open + n_legs}"
                    f">{self._max_basket_legs_open}"
                )

            # Reserve to prevent double-counting within same scheduler cycle
            self._reserved_usd += notional
            self._basket_legs_open += n_legs
            return True, "ok"

    def can_place_tail(self, notional: float) -> tuple[bool, str]:
        """
        Check if we can deploy `notional` USD in a tail NO position.
        Checks: emergency stop, daily loss, total deployed cap, tail cap.
        """
        with self._lock:
            self._maybe_roll_day()
            self._load_live_stats()

            if self._emergency_stop:
                return False, f"Emergency stop: {self._emergency_stop_reason}"

            if self._daily_loss >= self._daily_loss_limit_usd:
                self._trigger_daily_loss_stop(
                    f"Daily loss ${self._daily_loss:.2f} >= limit ${self._daily_loss_limit_usd:.2f}"
                )
                return False, f"Daily loss limit reached: ${self._daily_loss:.2f}"

            if self._total_loss >= self._total_stop_loss_usd:
                reason = (
                    f"Total loss ${self._total_loss:.2f} >= "
                    f"TOTAL_STOP_LOSS ${self._total_stop_loss_usd:.2f}"
                )
                self._trigger_total_stop(reason)
                return False, reason

            deployed = self._get_deployed_by_strategy()
            tail_deployed = deployed.get("tail_no", 0.0)
            total_deployed = sum(deployed.values()) + self._reserved_usd

            if total_deployed + notional > self._total_deployed_cap_usd:
                return False, (
                    f"total_cap ${total_deployed + notional:.2f}"
                    f">${self._total_deployed_cap_usd:.2f}"
                )

            if tail_deployed + notional > self._tail_max_usd:
                return False, (
                    f"tail_cap ${tail_deployed + notional:.2f}"
                    f">${self._tail_max_usd:.2f}"
                )

            return True, "ok"

    def release_basket_reservation(self, notional: float, n_legs: int) -> None:
        """Release in-memory reservation after basket placement completes (success or failure)."""
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - notional)
            self._basket_legs_open = max(0, self._basket_legs_open - n_legs)

    def can_place_order(self, bet_size_usd: float) -> tuple[bool, str]:
        """
        Returns (True, 'ok') or (False, reason).
        Checks are applied in spec order (1-7).
        """
        with self._lock:
            self._maybe_roll_day()
            self._load_live_stats()  # syncs emergency_stop from DB too

            # 1. Emergency stop
            if self._emergency_stop:
                return False, f"Emergency stop active: {self._emergency_stop_reason}"

            # 2. Cap single bet (warn, don't reject)
            if bet_size_usd > self._max_single_bet_usd:
                logger.warning(
                    f"RiskManager: bet ${bet_size_usd:.2f} exceeds MAX_SINGLE_BET_USD "
                    f"${self._max_single_bet_usd:.2f} — will be capped"
                )

            # 3. Daily loss limit
            if self._daily_loss >= self._daily_loss_limit_usd:
                self._trigger_daily_loss_stop(
                    f"Daily loss ${self._daily_loss:.2f} >= limit ${self._daily_loss_limit_usd:.2f}"
                )
                return False, f"Daily loss limit reached: ${self._daily_loss:.2f}"

            # 4. Total stop-loss → triggers persistent emergency stop (edge-triggered alert)
            if self._total_loss >= self._total_stop_loss_usd:
                reason = (
                    f"Total loss ${self._total_loss:.2f} >= "
                    f"TOTAL_STOP_LOSS_USD ${self._total_stop_loss_usd:.2f}"
                )
                self._trigger_total_stop(reason)
                return False, f"Total stop-loss triggered: {reason}"

            # 5. Concurrent open bets
            if len(self._open_bets) >= self._max_concurrent_bets:
                return False, (
                    f"Max concurrent bets reached "
                    f"({len(self._open_bets)}/{self._max_concurrent_bets})"
                )

            # 6. Daily bet count
            if self._daily_bets_count >= self._max_daily_bets:
                return False, (
                    f"Daily bet count limit reached "
                    f"({self._daily_bets_count}/{self._max_daily_bets})"
                )

            # 7. All ok
            return True, "ok"

    def cap_bet_size(self, bet_size_usd: float) -> float:
        return min(bet_size_usd, self._max_single_bet_usd)

    def record_bet_placed(self, group_id: str, bet_size_usd: float, strategy: str = "live") -> None:
        with self._lock:
            self._maybe_roll_day()
            capped = min(bet_size_usd, self._max_single_bet_usd)
            self._open_bets[group_id] = capped
            self._daily_bets_count += 1
            logger.info(
                f"RiskManager: bet recorded group={group_id} size=${capped:.2f} "
                f"strategy={strategy} daily_count={self._daily_bets_count}"
            )

    def record_bet_result(self, trade_id: Any, group_id: str, pnl_usd: float) -> None:
        with self._lock:
            self._maybe_roll_day()
            self._daily_pnl += pnl_usd
            if pnl_usd < 0:
                self._daily_loss += abs(pnl_usd)
                self._total_loss += abs(pnl_usd)
            self._open_bets.pop(group_id, None)
            logger.info(
                f"RiskManager: result trade_id={trade_id} group={group_id} "
                f"pnl=${pnl_usd:+.2f} daily_pnl=${self._daily_pnl:+.2f} "
                f"total_loss=${self._total_loss:.2f}"
            )

    def get_status(self) -> RiskStatus:
        with self._lock:
            self._maybe_roll_day()
            self._load_live_stats()  # always show live DB values
            deployed = self._get_deployed_by_strategy()
            basket_deployed = (
                deployed.get("basket_wide", 0.0) + deployed.get("basket_narrow", 0.0)
            )
            tail_deployed = deployed.get("tail_no", 0.0)
            total_deployed = sum(deployed.values())
            return RiskStatus(
                daily_pnl=self._daily_pnl,
                daily_loss=self._daily_loss,
                daily_bets_count=self._daily_bets_count,
                concurrent_open_bets=len(self._open_bets),
                total_loss=self._total_loss,
                emergency_stop=self._emergency_stop,
                emergency_stop_reason=self._emergency_stop_reason,
                emergency_stop_type=self._emergency_stop_type,
                max_single_bet_usd=self._max_single_bet_usd,
                max_daily_bets=self._max_daily_bets,
                max_concurrent_bets=self._max_concurrent_bets,
                daily_loss_limit_usd=self._daily_loss_limit_usd,
                total_stop_loss_usd=self._total_stop_loss_usd,
                basket_deployed_usd=basket_deployed,
                tail_deployed_usd=tail_deployed,
                total_deployed_usd=total_deployed,
                total_deployed_cap_usd=self._total_deployed_cap_usd,
                basket_max_usd=self._basket_max_usd,
                tail_max_usd=self._tail_max_usd,
            )

    def set_emergency_stop(self, reason: str, stop_type: str = "manual") -> None:
        """Activate emergency stop — persisted to DB, survives restarts."""
        with self._lock:
            self._emergency_stop = True
            self._emergency_stop_reason = reason
            self._emergency_stop_type = stop_type
            logger.warning(f"RiskManager: EMERGENCY STOP SET [{stop_type}] — {reason}")
        self._persist_emergency_stop(True, reason, stop_type)

    def clear_emergency_stop(self) -> None:
        """Clear emergency stop — persisted to DB."""
        with self._lock:
            self._emergency_stop = False
            self._emergency_stop_reason = ""
            self._emergency_stop_type = ""
            logger.info("RiskManager: emergency stop cleared")
        self._persist_emergency_stop(False, "", "")

    def get_stop_type(self) -> str:
        """Return the type of the active emergency stop from DB ('manual'|'daily_loss'|'total_loss'|'')."""
        try:
            from src.db.session import get_session
            from src.db.models import BotState

            session = get_session()
            try:
                row = session.get(BotState, "emergency_stop_type")
                return row.value if row else ""
            finally:
                session.close()
        except Exception as exc:
            logger.warning(f"RiskManager: get_stop_type DB check failed: {exc}")
            return self._emergency_stop_type

    def get_stop_triggered_at(self) -> "datetime | None":
        """Return the timestamp when the active emergency stop was set (from updated_at of the DB row)."""
        try:
            from src.db.session import get_session
            from src.db.models import BotState

            session = get_session()
            try:
                row = session.get(BotState, "emergency_stop")
                if row and row.value == "1":
                    return row.updated_at
                return None
            finally:
                session.close()
        except Exception as exc:
            logger.warning(f"RiskManager: get_stop_triggered_at DB check failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Runtime limit setters (called by Telegram bot /setlimit, /setstake)
    # Changes are in-memory only and reset on process restart.
    # ------------------------------------------------------------------

    def set_daily_loss_limit(self, value: float) -> None:
        with self._lock:
            self._daily_loss_limit_usd = value
            logger.info(f"RiskManager: daily_loss_limit_usd → ${value:.2f}")

    def set_max_single_bet(self, value: float) -> None:
        with self._lock:
            self._max_single_bet_usd = value
            logger.info(f"RiskManager: max_single_bet_usd → ${value:.2f}")

    def set_total_stop_loss(self, value: float) -> None:
        with self._lock:
            self._total_stop_loss_usd = value
            logger.info(f"RiskManager: total_stop_loss_usd → ${value:.2f}")


# Module-level singleton — shared across scheduler and Telegram bot threads
risk_manager = RiskManager()
