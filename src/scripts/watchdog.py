"""
Thread watchdog — run every 5 minutes via systemd timer or cron.

Reads per-thread heartbeat files written by each of the 3 bot threads.
If any thread has not ticked within its staleness threshold, sends a Telegram
alert to the operator. Exits 0 (all OK) or 1 (stale alert sent).

Systemd timer example (every 5 min):
    [Timer]
    OnBootSec=5min
    OnUnitActiveSec=5min

Cron example:
    */5 * * * * cd /path/to/bot && python -m src.scripts.watchdog

Usage:
    python -m src.scripts.watchdog
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from src.monitoring.heartbeat import STALE_THRESHOLDS, seconds_since

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Human-readable labels for each thread. tail_exit is flagged CRITICAL because
# a hung tail-exit thread leaves open tail positions without an exit manager.
_THREAD_LABELS: dict[str, str] = {
    "scheduler": "scheduler (main loop)",
    "tail_exit": "tail-exit-ticker — CRITICAL: positions may be unmanaged",
    "telegram": "telegram-bot",
}


def _check_threads() -> list[str]:
    """Return list of human-readable stale-thread descriptions (empty = all OK)."""
    stale: list[str] = []
    for thread, label in _THREAD_LABELS.items():
        elapsed = seconds_since(thread)
        threshold = STALE_THRESHOLDS[thread]
        if elapsed is None:
            logger.warning(f"{thread}: no heartbeat file — thread may never have started")
            stale.append(f"• {label}: no heartbeat on record")
        elif elapsed > threshold:
            mins = elapsed / 60
            logger.warning(
                f"{thread}: STALE — {mins:.1f} min since last tick "
                f"(threshold {threshold // 60} min)"
            )
            stale.append(
                f"• {label}: last tick {mins:.1f} min ago "
                f"(threshold {threshold // 60} min)"
            )
        else:
            logger.info(f"{thread}: OK — {elapsed:.0f}s since last tick")
    return stale


def _check_matic() -> list[str]:
    """Return alert lines if EOA MATIC gas balance is below threshold (empty = OK)."""
    from src.config.settings import settings

    wallet = settings.eoa_wallet_address
    if not wallet:
        logger.debug("eoa_wallet_address not configured — skipping MATIC check")
        return []

    threshold = settings.matic_alert_threshold
    try:
        from src.blockchain.balance import get_matic_balance
        balance = get_matic_balance(wallet)
        if balance < threshold:
            logger.warning(
                f"MATIC LOW — {balance:.4f} MATIC (threshold {threshold}) on {wallet[:10]}…"
            )
            return [
                f"⛽ MATIC низкий: {balance:.4f} MATIC < {threshold} MATIC — "
                f"редимы могут застрять в pending_redeem (EOA {wallet[:10]}…)"
            ]
        logger.info(f"MATIC: OK — {balance:.4f} MATIC (threshold {threshold})")
    except Exception as exc:
        logger.warning(f"MATIC balance check failed: {exc}")
    return []


def main() -> int:
    stale = _check_threads()
    matic_issues = _check_matic()

    if not stale and not matic_issues:
        logger.info("All checks OK")
        return 0

    parts: list[str] = []
    if stale:
        parts.append(
            "⚠️ watchdog: thread staleness detected\n\n"
            + "\n".join(stale)
        )
    parts.extend(matic_issues)

    msg = "\n\n".join(parts) + f"\n\nChecked at {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    try:
        from src.notifications.telegram import get_notifier
        get_notifier().send(msg)
        logger.info("Alert sent via Telegram")
    except Exception as exc:
        logger.error(f"Failed to send Telegram alert: {exc}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
