"""Per-thread heartbeat for the 3-thread bot architecture.

Each thread writes its name to a small timestamped file on every tick.
The external watchdog reads these files and alerts via Telegram on staleness.
File-based (not DB) to avoid adding write contention under the 3-thread SQLite setup.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_HEARTBEAT_DIR = Path("data/heartbeats")

# Staleness thresholds per thread (seconds).
# A thread is "stale" when its last heartbeat is older than this value.
# tail_exit is most critical: a hung tail-exit thread leaves open positions unmanaged.
STALE_THRESHOLDS: dict[str, int] = {
    "scheduler": 90,    # 3 × 30 s main loop tick
    "tail_exit": 660,   # 2.2 × 300 s tail-exit tick (most critical)
    "telegram": 180,    # 3 × 60 s async heartbeat interval
}


def write_heartbeat(thread_name: str) -> None:
    """Write current UTC timestamp to the thread's heartbeat file. Never raises."""
    try:
        _HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
        path = _HEARTBEAT_DIR / f"{thread_name}.ts"
        path.write_text(datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        logger.debug(f"[heartbeat] write failed for {thread_name}: {exc}")


def read_heartbeat(thread_name: str) -> datetime | None:
    """Return last heartbeat datetime (UTC-aware) for thread, or None if missing."""
    try:
        path = _HEARTBEAT_DIR / f"{thread_name}.ts"
        raw = path.read_text().strip()
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def seconds_since(thread_name: str) -> float | None:
    """Seconds elapsed since last heartbeat, or None if no heartbeat file exists."""
    hb = read_heartbeat(thread_name)
    if hb is None:
        return None
    return (datetime.now(timezone.utc) - hb).total_seconds()


def is_stale(thread_name: str) -> bool:
    """Return True if the thread has not ticked within its staleness threshold.

    A missing heartbeat file (thread never started or file deleted) is treated
    as stale to fail-closed.
    """
    elapsed = seconds_since(thread_name)
    if elapsed is None:
        return True
    return elapsed > STALE_THRESHOLDS.get(thread_name, 300)
