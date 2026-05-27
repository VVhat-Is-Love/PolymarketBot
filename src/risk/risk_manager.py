"""
Central risk controller — all trading decisions pass through here.
State is held in memory (thread-safe) and mirrored to the DB.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime
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
    max_single_bet_usd: float
    max_daily_bets: int
    max_concurrent_bets: int
    daily_loss_limit_usd: float
    total_stop_loss_usd: float


class RiskManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._load_settings()
        self._reset_daily_state()
        self._total_loss: float = 0.0
        self._emergency_stop: bool = False
        self._emergency_stop_reason: str = ""
        # group_id → bet_size_usd for currently open bets
        self._open_bets: dict[str, float] = {}

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

    def _reset_daily_state(self) -> None:
        self._today = date.today()
        self._daily_pnl: float = 0.0
        self._daily_loss: float = 0.0
        self._daily_bets_count: int = 0

    def _maybe_roll_day(self) -> None:
        if date.today() != self._today:
            logger.info("RiskManager: rolling daily state for new UTC day")
            self._reset_daily_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_place_order(self, bet_size_usd: float) -> tuple[bool, str]:
        """
        Returns (True, 'ok') or (False, reason).
        Checks are applied in spec order (1-7).
        Capping bet_size to MAX_SINGLE_BET_USD is done in-place before callers use it.
        """
        with self._lock:
            self._maybe_roll_day()

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
                return False, f"Daily loss limit reached: ${self._daily_loss:.2f}"

            # 4. Total stop-loss → triggers emergency stop
            if self._total_loss >= self._total_stop_loss_usd:
                reason = f"Total loss ${self._total_loss:.2f} >= TOTAL_STOP_LOSS_USD ${self._total_stop_loss_usd:.2f}"
                self._emergency_stop = True
                self._emergency_stop_reason = reason
                logger.error(f"RiskManager: TOTAL STOP-LOSS triggered — {reason}")
                return False, f"Total stop-loss triggered: {reason}"

            # 5. Concurrent open bets
            if len(self._open_bets) >= self._max_concurrent_bets:
                return False, f"Max concurrent bets reached ({len(self._open_bets)}/{self._max_concurrent_bets})"

            # 6. Daily bet count
            if self._daily_bets_count >= self._max_daily_bets:
                return False, f"Daily bet count limit reached ({self._daily_bets_count}/{self._max_daily_bets})"

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
            return RiskStatus(
                daily_pnl=self._daily_pnl,
                daily_loss=self._daily_loss,
                daily_bets_count=self._daily_bets_count,
                concurrent_open_bets=len(self._open_bets),
                total_loss=self._total_loss,
                emergency_stop=self._emergency_stop,
                emergency_stop_reason=self._emergency_stop_reason,
                max_single_bet_usd=self._max_single_bet_usd,
                max_daily_bets=self._max_daily_bets,
                max_concurrent_bets=self._max_concurrent_bets,
                daily_loss_limit_usd=self._daily_loss_limit_usd,
                total_stop_loss_usd=self._total_stop_loss_usd,
            )

    def set_emergency_stop(self, reason: str) -> None:
        with self._lock:
            self._emergency_stop = True
            self._emergency_stop_reason = reason
            logger.warning(f"RiskManager: EMERGENCY STOP SET — {reason}")

    def clear_emergency_stop(self) -> None:
        with self._lock:
            self._emergency_stop = False
            self._emergency_stop_reason = ""
            logger.info("RiskManager: emergency stop cleared")

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
