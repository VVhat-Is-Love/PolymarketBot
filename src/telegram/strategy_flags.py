"""
Thread-safe in-memory flags for enabling/disabling each trading strategy.
Toggled via Telegram bot /strategy commands.

HARD DISABLE (deploy guard): strategies in _HARD_DISABLED are forced OFF
unconditionally — is_enabled() always returns False for them and set_enabled()
refuses to turn them on. This does NOT rely on caps (basket_deployed_pct=0) or on
discovery/filters not surfacing a candidate: even if a basket candidate is found
and passes the checklist, the engine's _strat_enabled() gate returns False and no
order is placed. To re-enable basket trading, remove it from _HARD_DISABLED.
"""
import threading

from loguru import logger

KNOWN_STRATEGIES = ("basket_wide", "basket_narrow", "tail_no")

# Hard, code-level kill: these never trade regardless of runtime toggles.
_HARD_DISABLED: frozenset[str] = frozenset({"basket_wide", "basket_narrow"})

_lock = threading.Lock()
# Hard-disabled strategies start (and stay) False; others default enabled.
_flags: dict[str, bool] = {s: (s not in _HARD_DISABLED) for s in KNOWN_STRATEGIES}


def is_hard_disabled(name: str) -> bool:
    return name in _HARD_DISABLED


def is_enabled(name: str) -> bool:
    """True if the strategy may trade. Hard-disabled strategies are ALWAYS False."""
    if name in _HARD_DISABLED:
        return False
    with _lock:
        return _flags.get(name, True)


def set_enabled(name: str, value: bool) -> None:
    """Enable/disable a strategy. Turning ON a hard-disabled strategy is refused."""
    if value and name in _HARD_DISABLED:
        logger.warning(
            f"[strategy_flags] refused to enable '{name}' — HARD-DISABLED in code "
            f"(remove from _HARD_DISABLED to allow basket trading)"
        )
        return
    with _lock:
        _flags[name] = value


def get_all() -> dict[str, bool]:
    """Snapshot of effective flags (hard-disabled always shown False)."""
    with _lock:
        snap = dict(_flags)
    for s in _HARD_DISABLED:
        snap[s] = False
    return snap


def log_startup_flags() -> None:
    """One-line startup banner of strategy state; spells out any hard disable."""
    flags = get_all()
    enabled = [s for s, on in flags.items() if on]
    if _HARD_DISABLED:
        logger.info(
            f"Strategy flags: enabled={enabled} | "
            f"basket DISABLED (hard flag: {sorted(_HARD_DISABLED)}) — "
            f"engine will NOT place basket orders under any condition"
        )
    else:
        logger.info(f"Strategy flags: enabled={enabled}")
