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
    # --- Paper contour (independent from live) ---
    paper_daily_loss: float
    paper_total_loss: float
    paper_bankroll_usd: float
    paper_total_stop_loss_usd: float
    paper_daily_loss_limit_usd: float
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
        # --- Live contour ---
        self._total_loss: float = 0.0
        self._emergency_stop: bool = False
        self._emergency_stop_reason: str = ""
        self._emergency_stop_type: str = ""   # 'manual' | 'daily_loss' | 'total_loss'
        # --- Paper contour (independent virtual budget) ---
        self._paper_daily_loss: float = 0.0
        self._paper_total_loss: float = 0.0
        # group_id → bet_size_usd for currently open bets
        self._open_bets: dict[str, float] = {}
        # In-session reservations: notional pre-approved but not yet in DB
        self._reserved_usd: float = 0.0
        self._basket_legs_open: int = 0
        # G4-20: per-token_id in-run reservations for tail aggregate cap
        self._tail_token_reserved: dict[str, float] = {}

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
        # Paper contour limits (virtual, never block live)
        self._paper_bankroll_usd = settings.paper_bankroll_usd
        self._paper_total_stop_loss_usd = settings.paper_total_stop_loss_usd
        self._paper_daily_loss_limit_usd = settings.paper_daily_loss_limit_usd

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
            from src.db.models import LiveTrade, BotState, MarketGroup, PaperTrade

            today_start = datetime.combine(date.today(), datetime.min.time())
            cutoff_36h = datetime.utcnow() - timedelta(hours=36)
            session = get_session()
            try:
                # -- Emergency stop (DB-persistent, live contour only) --
                stop_row = session.get(BotState, "emergency_stop")
                if stop_row and stop_row.value == "1":
                    self._emergency_stop = True
                    reason_row = session.get(BotState, "emergency_stop_reason")
                    self._emergency_stop_reason = reason_row.value if reason_row else "DB-persisted stop"
                    type_row = session.get(BotState, "emergency_stop_type")
                    self._emergency_stop_type = type_row.value if type_row else "manual"
                elif not self._emergency_stop:
                    pass  # keep in-memory flag if set locally this session

                # -- Daily bet count (all live trades placed today) --
                self._daily_bets_count = int(
                    session.execute(
                        select(func.count()).select_from(LiveTrade).where(
                            LiveTrade.placed_at >= today_start,
                            LiveTrade.status != "not_filled",  # phantom isn't a bet
                        )
                    ).scalar() or 0
                )

                # -- Concurrent open / pending positions (exclude Gamma-resolved groups) --
                rows = session.execute(
                    select(LiveTrade.group_id, LiveTrade.id, LiveTrade.stake_usd)
                    .join(MarketGroup, LiveTrade.group_id == MarketGroup.group_id, isouter=True)
                    .where(
                        LiveTrade.status.in_(["pending", "open"]),
                        MarketGroup.is_active.is_not(False),
                    )
                ).all()
                self._open_bets = {
                    (r.group_id or r.id): (r.stake_usd or 0.0) for r in rows
                }

                # ── LIVE CONTOUR ─────────────────────────────────────────────
                # Only reconciled LiveTrade rows — paper trades physically can't appear here.

                # Daily loss (36h, reconciled live trades only)
                v = session.execute(
                    select(func.coalesce(func.sum(LiveTrade.pnl_usd), 0.0)).where(
                        LiveTrade.resolved_at >= cutoff_36h,
                        LiveTrade.pnl_usd < 0,
                        LiveTrade.reconciled_with_polymarket == True,  # noqa: E712
                    )
                ).scalar() or 0.0
                self._daily_loss = abs(float(v))

                # Total loss (all-time, reconciled live trades only)
                v = session.execute(
                    select(func.coalesce(func.sum(LiveTrade.pnl_usd), 0.0)).where(
                        LiveTrade.pnl_usd < 0,
                        LiveTrade.reconciled_with_polymarket == True,  # noqa: E712
                    )
                ).scalar() or 0.0
                self._total_loss = abs(float(v))

                # Detail for log: up to 5 contributing trades
                live_detail = session.execute(
                    select(LiveTrade.id, LiveTrade.city, LiveTrade.pnl_usd)
                    .where(
                        LiveTrade.pnl_usd < 0,
                        LiveTrade.reconciled_with_polymarket == True,  # noqa: E712
                    )
                    .order_by(LiveTrade.resolved_at.desc())
                    .limit(5)
                ).all()
                live_ids_str = (
                    ", ".join(f"#{r.id}({r.city or '?'} ${r.pnl_usd:.2f})" for r in live_detail)
                    or "none"
                )
                logger.info(
                    f"live_loss_calc: daily=${self._daily_loss:.4f} "
                    f"total=${self._total_loss:.4f} "
                    f"from {len(live_detail)} reconciled live [{live_ids_str}]"
                )

                # Daily PnL (resolved today, live)
                v = session.execute(
                    select(func.coalesce(func.sum(LiveTrade.pnl_usd), 0.0)).where(
                        LiveTrade.resolved_at >= today_start,
                        LiveTrade.pnl_usd.isnot(None),
                    )
                ).scalar() or 0.0
                self._daily_pnl = float(v)

                # ── PAPER CONTOUR ────────────────────────────────────────────
                # Exclusively PaperTrade rows — never touches live limits.

                # Paper daily loss (36h) — exclude voided legacy (pre-ledger-fix) sims
                v = session.execute(
                    select(func.coalesce(func.sum(PaperTrade.pnl_usd), 0.0)).where(
                        PaperTrade.resolved_at >= cutoff_36h,
                        PaperTrade.pnl_usd < 0,
                        PaperTrade.status != "voided_legacy",
                    )
                ).scalar() or 0.0
                self._paper_daily_loss = abs(float(v))

                # Paper total loss (all-time) — clean post-fix simulations only
                v = session.execute(
                    select(func.coalesce(func.sum(PaperTrade.pnl_usd), 0.0)).where(
                        PaperTrade.pnl_usd < 0,
                        PaperTrade.status != "voided_legacy",
                    )
                ).scalar() or 0.0
                self._paper_total_loss = abs(float(v))

                # Detail for log: up to 5 contributing paper trades
                paper_detail = session.execute(
                    select(PaperTrade.id, PaperTrade.city, PaperTrade.pnl_usd)
                    .where(
                        PaperTrade.pnl_usd < 0,
                        PaperTrade.status != "voided_legacy",
                    )
                    .order_by(PaperTrade.resolved_at.desc())
                    .limit(5)
                ).all()
                paper_ids_str = (
                    ", ".join(f"#{r.id}({r.city or '?'} ${r.pnl_usd:.2f})" for r in paper_detail)
                    or "none"
                )
                logger.info(
                    f"paper_loss_calc: daily=${self._paper_daily_loss:.4f} "
                    f"total=${self._paper_total_loss:.4f} "
                    f"from {len(paper_detail)} paper [{paper_ids_str}]"
                )

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

    def can_run_paper(self) -> tuple[bool, str]:
        """
        Check if paper trading is allowed against the virtual bankroll.
        Completely independent from live risk: paper losses NEVER block live.

        G4-21: any alert from the paper contour is PAPER-labelled and uses neutral
        wording — never the live-stop emoji/words (🛑 TOTAL STOP-LOSS /
        ⚠️ DAILY LOSS LIMIT), so a paper threshold can never be mistaken for a
        real live stop. Edge-triggered (one alert per block→unblock transition).
        """
        with self._lock:
            self._maybe_roll_day()
            self._load_live_stats()

            blocked_reason = ""
            if self._paper_daily_loss >= self._paper_daily_loss_limit_usd:
                blocked_reason = (
                    f"Paper daily loss ${self._paper_daily_loss:.2f} >= "
                    f"limit ${self._paper_daily_loss_limit_usd:.2f}"
                )
            elif self._paper_total_loss >= self._paper_total_stop_loss_usd:
                blocked_reason = (
                    f"Paper total loss ${self._paper_total_loss:.2f} >= "
                    f"virtual stop ${self._paper_total_stop_loss_usd:.2f}"
                )

            if blocked_reason:
                if not getattr(self, "_paper_block_alerted", False):
                    self._paper_block_alerted = True
                    try:
                        from src.notifications.telegram import get_notifier
                        get_notifier().send(
                            f"📊 PAPER (тест): симуляция на паузе — {blocked_reason}. "
                            f"Это виртуальный бюджет, на live-торговлю не влияет."
                        )
                    except Exception:
                        pass
                return False, blocked_reason

            # Unblocked → reset edge-trigger so the next block re-alerts
            self._paper_block_alerted = False
            return True, "ok"

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

    def _tail_committed_for_token(self, token_id: str) -> float:
        """DB: Σ stake_usd of live tail positions already on this token_id."""
        try:
            from sqlalchemy import select, func
            from src.db.session import get_session
            from src.db.models import LiveTrade
            session = get_session()
            try:
                v = session.execute(
                    select(func.coalesce(func.sum(LiveTrade.stake_usd), 0.0)).where(
                        LiveTrade.token_id == token_id,
                        LiveTrade.strategy_name == "tail_no",
                        LiveTrade.status.in_(["pending", "open", "filled"]),
                    )
                ).scalar() or 0.0
                return float(v)
            finally:
                session.close()
        except Exception as exc:
            logger.warning(f"RiskManager: _tail_committed_for_token failed: {exc}")
            return 0.0

    def can_place_tail_token(self, token_id: str, notional: float) -> tuple[bool, str]:
        """
        G4-20: aggregate exposure cap PER token_id (double-guard).

        committed = (open+pending+filled in DB) + (reserved earlier THIS run).
        If committed + notional > tail_max_position_usd → reject token_cap_reached.
        On pass, RESERVES notional for this token so a second order in the same
        cycle sees the first order's commitment. Reservation is released by
        release_tail_token_reservation() (call on failure) — successful orders
        keep the reservation until _load_live_stats picks up the DB row.
        """
        from src.strategy.config import strategy_settings as ss
        cap = getattr(ss, "tail_max_position_usd", 6.0)
        if not token_id:
            return True, "ok"
        with self._lock:
            committed = self._tail_committed_for_token(token_id)
            committed += self._tail_token_reserved.get(token_id, 0.0)
            if committed + notional > cap + 1e-9:
                return False, (
                    f"token_cap_reached: ${committed + notional:.2f} > "
                    f"tail_max_position ${cap:.2f} (token={token_id[:10]}…)"
                )
            self._tail_token_reserved[token_id] = (
                self._tail_token_reserved.get(token_id, 0.0) + notional
            )
            return True, "ok"

    def release_tail_token_reservation(self, token_id: str, notional: float) -> None:
        """Release a per-token tail reservation (on placement failure/cancel)."""
        with self._lock:
            cur = self._tail_token_reserved.get(token_id, 0.0)
            new = max(0.0, cur - notional)
            if new <= 1e-9:
                self._tail_token_reserved.pop(token_id, None)
            else:
                self._tail_token_reserved[token_id] = new

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
                paper_daily_loss=self._paper_daily_loss,
                paper_total_loss=self._paper_total_loss,
                paper_bankroll_usd=self._paper_bankroll_usd,
                paper_total_stop_loss_usd=self._paper_total_stop_loss_usd,
                paper_daily_loss_limit_usd=self._paper_daily_loss_limit_usd,
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
