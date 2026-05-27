"""
Thread-safe in-memory flags for enabling/disabling each trading strategy at runtime.
Toggled via Telegram bot /strategy commands.
Resets to all-enabled on process restart — intentional (safe default).
"""
import threading

KNOWN_STRATEGIES = ("basket_wide", "basket_narrow", "tail_no")

_lock = threading.Lock()
_flags: dict[str, bool] = {s: True for s in KNOWN_STRATEGIES}


def is_enabled(name: str) -> bool:
    """Return True if the strategy is currently enabled (unknown names default to True)."""
    with _lock:
        return _flags.get(name, True)


def set_enabled(name: str, value: bool) -> None:
    """Enable or disable a strategy by name."""
    with _lock:
        _flags[name] = value


def get_all() -> dict[str, bool]:
    """Return a snapshot of all strategy flags."""
    with _lock:
        return dict(_flags)
