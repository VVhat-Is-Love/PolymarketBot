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
    # Deployed capital by strategy (caps computed from equity × pct)
    basket_deployed_usd: float = 0.0
    tail_deployed_usd: float = 0.0
    total_deployed_usd: float = 0.0
    equity_usd: float = 0.0
    total_deployed_pct: float = 0.0
    tail_deployed_pct: float = 0.0
    # Computed $-equivalents (equity × pct) — for display
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
        # Drawdown stop state (set each cycle by evaluate_drawdown_stop)
        self._dd_last: float = 0.0
        self._dd_size_multiplier: float = 1.0   # 1.0 normal, 0.5 when dd ≥ soft
        # Set to True by clear_emergency_stop() when clearing a "drawdown" hard stop.
        # Prevents evaluate_drawdown_stop from immediately re-arming the same event
        # after /reset_stop.  Cleared when dd recovers below soft threshold.
        self._drawdown_hard_reset: bool = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        from src.config.settings import settings
        self._max_single_bet_usd = settings.max_single_bet_usd
        self._max_daily_bets = settings.max_daily_bets
        self._max_concurrent_bets = settings.max_concurrent_bets
        self._daily_loss_limit_usd = settings.daily_loss_limit_usd
        self._total_stop_loss_usd = settings.total_stop_loss_usd  # retired (no trigger)
        # Drawdown-from-equity stop (replaces the gross-loss total stop)
        self._drawdown_soft_pct = settings.drawdown_soft_pct
        self._drawdown_hard_stop_pct = settings.drawdown_hard_stop_pct
        self._drawdown_hwm_window_days = settings.drawdown_hwm_window_days
        self._max_basket_legs_open = settings.max_basket_legs_open
        self._max_tail_positions = settings.max_tail_positions
        # Equity-based cap fractions
        self._total_deployed_pct = settings.total_deployed_pct
        self._tail_deployed_pct = settings.tail_deployed_pct
        self._max_tail_zone_positions = settings.max_tail_zone_positions
        # Kept for settings load compatibility — no longer enforced
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

    def _get_deployed_by_strategy(self) -> "dict[str, float] | None":
        """DB query: sum of stake_usd per strategy for non-resolved trades.
        Returns None on DB error so callers can fail-closed rather than see $0 deployed."""
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
            return None

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

    # _trigger_total_stop REMOVED (G-section 10b): the gross-loss total stop is
    # retired, not kept as a secondary trigger — it summed Σ|losses| ignoring wins,
    # was monotonic and never reset, and re-armed after /reset_stop, bricking a
    # net-positive account. Replaced by evaluate_drawdown_stop (drawdown-from-equity).

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

    # ------------------------------------------------------------------
    # Drawdown-from-equity emergency stop  (replaces gross-loss total stop)
    # ------------------------------------------------------------------

    def evaluate_drawdown_stop(self) -> None:
        """
        Drawdown-from-equity emergency stop. Runs EVERY cycle regardless of stop
        state so a recovered equity lifts the stop automatically — the retired
        gross-loss stop could never un-stop (monotonic Σ|losses|, recomputed from
        the same DB rows, so it re-armed a cycle after /reset_stop and bricked the
        bot while the account was net-positive).

            equity_now = cash + Σ(position × BID)      (stats.get_equity_bid, realizable)
            HWM_30d    = max equity over a rolling 30-day window (per-cycle samples)
            dd         = (HWM_30d − equity_now) / HWM_30d

            dd ≥ drawdown_soft_pct       → position sizes ×0.5 (soft, via
                                           position_size_multiplier())
            dd ≥ drawdown_hard_stop_pct  → emergency stop (hard)
            dd <  drawdown_soft_pct      → drawdown/total_loss stop auto-cleared
                                           (hysteresis band soft..hard holds state)

        Drawdown reads ONLY current equity vs its own peak — historical realized
        losses (e.g. the retired basket_narrow trades) never enter the metric.
        Live mode only. Defers (no HWM/dd change) when equity-by-bid is unavailable.
        """
        from src.config.settings import settings
        if settings.trading_mode.lower() != "live":
            return

        from src.telegram.stats import get_equity_bid
        snap = get_equity_bid()
        if not snap.get("ok"):
            logger.warning("[risk] drawdown: equity (bid) unavailable — defer, no status change")
            return

        equity = float(snap.get("total_bid", 0.0))
        hwm = self._update_equity_hwm(equity)          # persists sample, returns HWM_30d
        dd = (hwm - equity) / hwm if hwm > 0 else 0.0

        with self._lock:
            self._dd_last = dd
            self._dd_size_multiplier = 0.5 if dd >= self._drawdown_soft_pct else 1.0
            mult = self._dd_size_multiplier

        logger.info(
            f"[risk] drawdown: HWM_30d=${hwm:.2f} equity=${equity:.2f} "
            f"dd={dd * 100:.1f}% (soft≥{self._drawdown_soft_pct * 100:.0f}% "
            f"hard≥{self._drawdown_hard_stop_pct * 100:.0f}% size×{mult:g})"
        )

        if dd >= self._drawdown_hard_stop_pct:
            self._trigger_drawdown_stop(
                f"Drawdown {dd * 100:.1f}% ≥ {self._drawdown_hard_stop_pct * 100:.0f}% "
                f"(HWM_30d=${hwm:.2f} → equity=${equity:.2f})"
            )
        else:
            # dd < hard threshold: clear the /reset_stop re-arm guard so a future
            # breach triggers a fresh stop (not suppressed by the old clear).
            # Clears on ANY de-escalation below hard, not just below soft — otherwise
            # dd oscillating 15-20% after /reset_stop would block all future stops.
            with self._lock:
                if self._drawdown_hard_reset:
                    logger.info(
                        f"[risk] dd {dd * 100:.1f}% < {self._drawdown_hard_stop_pct * 100:.0f}% "
                        f"hard — drawdown_hard_reset cleared, next breach will re-arm"
                    )
                    self._drawdown_hard_reset = False
            if dd < self._drawdown_soft_pct:
                self._maybe_clear_drawdown_stop(dd)

    def _trigger_drawdown_stop(self, reason: str) -> None:
        """Edge-triggered drawdown stop; cleared only by /reset_stop (not auto-cleared)."""
        with self._lock:
            if self._emergency_stop and self._emergency_stop_type == "drawdown":
                return  # already drawdown-stopped — no re-alert
            # getattr: guard against old in-memory singletons missing this attribute
            if getattr(self, "_drawdown_hard_reset", False):
                return  # operator manually cleared this drawdown event — don't re-arm
            self._emergency_stop = True
            self._emergency_stop_reason = reason
            self._emergency_stop_type = "drawdown"
            logger.warning(f"RiskManager: EMERGENCY STOP SET [drawdown] — {reason}")
        self._persist_emergency_stop(True, reason, "drawdown")
        try:
            from src.config.settings import settings
            if settings.trading_mode.lower() == "live":
                from src.notifications.telegram import get_notifier
                get_notifier().send(f"🛑 DRAWDOWN STOP: {reason}")
        except Exception:
            pass

    def _maybe_clear_drawdown_stop(self, dd: float) -> None:
        """
        Lift a retired total_loss-stop once equity is back under the soft threshold.
        Hard drawdown stops (type="drawdown") are NOT auto-cleared — they require
        /reset_stop so the operator consciously reviews the situation first.
        Manual and daily_loss stops are left untouched (own lifecycle).
        """
        with self._lock:
            if not self._emergency_stop:
                return
            if self._emergency_stop_type not in ("total_loss",):
                return
            prev_type = self._emergency_stop_type
            self._emergency_stop = False
            self._emergency_stop_reason = ""
            self._emergency_stop_type = ""
        self._persist_emergency_stop(False, "", "")
        logger.info(
            f"[risk] drawdown recovered (dd={dd * 100:.1f}% < "
            f"{self._drawdown_soft_pct * 100:.0f}%) — {prev_type} stop cleared"
        )
        try:
            from src.config.settings import settings
            if settings.trading_mode.lower() == "live":
                from src.notifications.telegram import get_notifier
                get_notifier().send(
                    f"✅ Drawdown восстановлен (dd={dd * 100:.1f}%) — стоп снят, торговля возобновлена"
                )
        except Exception:
            pass

    def _update_equity_hwm(self, equity: float) -> float:
        """
        Persist today's equity peak, prune to the rolling 30-day window, and return
        HWM_30d = max(equity_now, daily peaks in window). Stored in bot_state as
        {date_iso: max_equity} — bounded at ≤ window+1 entries. On any DB error the
        current equity is returned as HWM (dd=0 → fail-safe, never a false stop).
        """
        import json
        from src.db.session import get_session
        from src.db.models import BotState

        today = date.today()
        cutoff = today - timedelta(days=self._drawdown_hwm_window_days)
        hwm = equity
        try:
            session = get_session()
            try:
                row = session.get(BotState, "equity_hwm_30d")
                data: dict[str, float] = {}
                if row and row.value:
                    try:
                        data = dict(json.loads(row.value))
                    except Exception:
                        data = {}

                pruned: dict[str, float] = {}
                for d_str, v in data.items():
                    try:
                        if date.fromisoformat(d_str) >= cutoff:
                            pruned[d_str] = float(v)
                    except (ValueError, TypeError):
                        continue

                tkey = today.isoformat()
                pruned[tkey] = max(float(pruned.get(tkey, 0.0)), equity)
                hwm = max([equity, *pruned.values()])

                session.merge(BotState(
                    key="equity_hwm_30d",
                    value=json.dumps(pruned),
                    updated_at=datetime.utcnow(),
                ))
                session.commit()
            finally:
                session.close()
        except Exception as exc:
            logger.warning(f"[risk] HWM update failed: {exc} — using equity as HWM (dd=0)")
        return hwm

    def position_size_multiplier(self) -> float:
        """
        Drawdown soft-sizing factor: 1.0 normally, 0.5 once dd ≥ drawdown_soft_pct.
        Set by evaluate_drawdown_stop each cycle. Exposed for the entry engine to
        scale stake; NOT yet wired into run_tail_engine (that is an entry-path
        touch the current task's ЗАПРЕТ excludes — wire on request).
        """
        with self._lock:
            return getattr(self, "_dd_size_multiplier", 1.0)

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
                    # G4-28: the alert quotes _paper_total_loss / _paper_daily_loss —
                    # the SAME fields just logged as paper_loss_calc by
                    # _load_live_stats. No legacy/unfiltered source. Log the exact
                    # value sent so the prod Telegram and the log provably agree.
                    logger.info(
                        f"PAPER pause alert: {blocked_reason} "
                        f"(paper_total=${self._paper_total_loss:.4f} "
                        f"paper_daily=${self._paper_daily_loss:.4f}) — "
                        f"NEUTRAL 📊 wording, no live stop"
                    )
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

    def can_place_basket(self, notional: float, n_legs: int, equity: float = 0.0) -> tuple[bool, str]:
        """
        Check if we can deploy `notional` USD across `n_legs` basket bins.
        Checks: emergency stop, daily loss, total deployed cap (equity-based), leg slots.
        Reserves the notional in-memory until release_basket_reservation() is called.
        equity: current cash+MTM total, used to compute pct-based caps dynamically.
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

            # Gross-loss total stop RETIRED — replaced by drawdown-from-equity stop
            # (evaluate_drawdown_stop). It set _emergency_stop above when armed.

            deployed = self._get_deployed_by_strategy()
            if deployed is None:
                logger.warning("[risk] equity cap: DB unavailable (fail-closed) — denying basket")
                return False, "equity_cap: DB error (fail-closed)"
            total_deployed = sum(deployed.values()) + self._reserved_usd

            if equity > 0:
                total_cap = equity * self._total_deployed_pct
                if total_deployed + notional > total_cap:
                    return False, (
                        f"total_cap ${total_deployed + notional:.2f}"
                        f">{self._total_deployed_pct:.0%}×equity=${total_cap:.2f}"
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

    def can_place_tail(self, notional: float, equity: float = 0.0) -> tuple[bool, str]:
        """
        Check if we can deploy `notional` USD in a tail NO position.
        Equity-based caps: total deployed ≤ total_deployed_pct × equity,
                           tail deployed  ≤ tail_deployed_pct  × equity.
        Also checks: emergency stop, daily loss, total loss, concurrent bets, daily count.
        equity: current cash+MTM total, used to compute pct-based caps dynamically.
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

            # Gross-loss total stop RETIRED — drawdown stop (evaluate_drawdown_stop)
            # owns the hard cut now and already set _emergency_stop above when armed.

            if len(self._open_bets) >= self._max_concurrent_bets:
                return False, (
                    f"Max concurrent bets reached "
                    f"({len(self._open_bets)}/{self._max_concurrent_bets})"
                )

            if self._daily_bets_count >= self._max_daily_bets:
                return False, (
                    f"Daily bet count limit reached "
                    f"({self._daily_bets_count}/{self._max_daily_bets})"
                )

            if equity > 0:
                deployed = self._get_deployed_by_strategy()
                if deployed is None:
                    logger.warning("[risk] equity cap: DB unavailable (fail-closed) — denying tail")
                    return False, "equity_cap: DB error (fail-closed)"
                tail_deployed = deployed.get("tail_no", 0.0)
                total_deployed = sum(deployed.values()) + self._reserved_usd

                total_cap = equity * self._total_deployed_pct
                if total_deployed + notional > total_cap:
                    return False, (
                        f"total_cap ${total_deployed + notional:.2f}"
                        f">{self._total_deployed_pct:.0%}×equity=${total_cap:.2f}"
                    )

                tail_cap = equity * self._tail_deployed_pct
                if tail_deployed + notional > tail_cap:
                    return False, (
                        f"tail_cap ${tail_deployed + notional:.2f}"
                        f">{self._tail_deployed_pct:.0%}×equity=${tail_cap:.2f}"
                    )

            return True, "ok"

    def can_place_tail_zone(self, city: str) -> tuple[bool, str]:
        """
        Anti-correlation entry guard: block if ≥ max_tail_zone_positions active
        tail_no trades already exist for cities in the same weather zone as `city`.
        Counts only ('pending','open','filled') rows — same statuses as deployed capital.
        """
        from src.config.cities import CITIES_WHITELIST
        zone = CITIES_WHITELIST.get(city, {}).get("zone")
        if not zone:
            return True, "ok"  # unknown city — allow
        zone_cities = [c for c, d in CITIES_WHITELIST.items() if d.get("zone") == zone]
        try:
            from sqlalchemy import select, func
            from src.db.session import get_session
            from src.db.models import LiveTrade
            session = get_session()
            try:
                count = int(
                    session.execute(
                        select(func.count()).where(
                            LiveTrade.strategy_name == "tail_no",
                            LiveTrade.status.in_(["pending", "open", "filled"]),
                            LiveTrade.city.in_(zone_cities),
                        )
                    ).scalar()
                    or 0
                )
            finally:
                session.close()
        except Exception as exc:
            logger.warning(f"[risk] zone gate fail-closed: DB error for {city}: {exc}")
            return False, "zone gate fail-closed: DB error"
        if count >= self._max_tail_zone_positions:
            return False, (
                f"zone_cap: {zone} has {count}/{self._max_tail_zone_positions} "
                f"active tail positions"
            )
        return True, "ok"

    def release_basket_reservation(self, notional: float, n_legs: int) -> None:
        """Release in-memory reservation after basket placement completes (success or failure)."""
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - notional)
            self._basket_legs_open = max(0, self._basket_legs_open - n_legs)

    def _tail_committed_for_token(self, token_id: str) -> float:
        """
        Committed USD already on this token_id = max(DB intent, on-chain /activity).

        G4-25: take the MAX of the local DB sum and the actual on-chain BUY
        exposure from /activity. A transient placement error (status=None /
        timeout) can leave an order recorded cancelled/absent in the DB while it
        executed on-chain; counting only the DB would let a retry double the fill
        (the Dallas $12 bug). max() picks up the on-chain reality without
        double-counting an order present in both.
        """
        db_val = 0.0
        try:
            from sqlalchemy import select, func
            from src.db.session import get_session
            from src.db.models import LiveTrade
            session = get_session()
            try:
                db_val = float(session.execute(
                    select(func.coalesce(func.sum(LiveTrade.stake_usd), 0.0)).where(
                        LiveTrade.token_id == token_id,
                        LiveTrade.strategy_name == "tail_no",
                        LiveTrade.status.in_(["pending", "open", "filled"]),
                    )
                ).scalar() or 0.0)
            finally:
                session.close()
        except Exception as exc:
            logger.warning(f"RiskManager: _tail_committed_for_token (DB) failed: {exc}")

        onchain = self._onchain_committed_for_token(token_id)
        return max(db_val, onchain)

    def _onchain_committed_for_token(self, token_id: str) -> float:
        """Actual open USD exposure on this token from /activity (BUY−SELL)."""
        if not token_id:
            return 0.0
        try:
            from src.config.settings import settings
            from src.market.polymarket_data import get_activity, open_cost_by_token
            wallet = settings.proxy_wallet_address
            if not wallet:
                return 0.0
            return float(open_cost_by_token(get_activity(wallet)).get(token_id, 0.0))
        except Exception as exc:
            logger.warning(f"RiskManager: _onchain_committed_for_token failed: {exc}")
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
        # Compute committed (DB + on-chain /activity) OUTSIDE the lock — it may
        # hit the network; holding the risk lock through pagination would stall
        # every other risk check. The reservation update below stays atomic.
        committed_external = self._tail_committed_for_token(token_id)
        with self._lock:
            committed = committed_external + self._tail_token_reserved.get(token_id, 0.0)
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

            # 4. Total stop-loss RETIRED — the gross-loss metric (Σ|losses| ignoring
            #    wins, monotonic, never reset) bricked a net-positive account. The
            #    drawdown-from-equity stop (evaluate_drawdown_stop) replaces it and
            #    sets _emergency_stop (caught at check 1) when dd ≥ hard threshold.

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

    def get_status(self, equity: float = 0.0) -> RiskStatus:
        with self._lock:
            self._maybe_roll_day()
            self._load_live_stats()  # always show live DB values
            deployed = self._get_deployed_by_strategy() or {}  # display: {} on DB error
            basket_deployed = (
                deployed.get("basket_wide", 0.0) + deployed.get("basket_narrow", 0.0)
            )
            tail_deployed = deployed.get("tail_no", 0.0)
            total_deployed = sum(deployed.values())
            # Compute $-equivalents from equity for display (0 if equity unknown)
            total_cap_usd = equity * self._total_deployed_pct if equity > 0 else 0.0
            tail_cap_usd = equity * self._tail_deployed_pct if equity > 0 else 0.0
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
                equity_usd=equity,
                total_deployed_pct=self._total_deployed_pct,
                tail_deployed_pct=self._tail_deployed_pct,
                total_deployed_cap_usd=total_cap_usd,
                basket_max_usd=0.0,   # basket disabled
                tail_max_usd=tail_cap_usd,
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
            was_drawdown = self._emergency_stop_type == "drawdown"
            self._emergency_stop = False
            self._emergency_stop_reason = ""
            self._emergency_stop_type = ""
            if was_drawdown:
                # Prevent evaluate_drawdown_stop from immediately re-arming while
                # dd is still ≥ hard threshold.  Flag cleared on natural recovery.
                self._drawdown_hard_reset = True
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
