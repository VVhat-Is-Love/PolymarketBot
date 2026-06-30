"""Local city time from a MarketGroup's captured UTC offset (G-A1).

The bot lives in UTC; weather peaks are a local-afternoon phenomenon, so the
time-gate / hard-floor exits need the city's local hour. MarketGroup.
utc_offset_seconds is populated from Open-Meteo (timezone="auto", DST-aware).
"""
from __future__ import annotations

from datetime import datetime, timedelta


def utc_offset_seconds(group) -> int | None:
    """The group's captured city UTC offset, or None if not yet populated."""
    if group is None:
        return None
    off = getattr(group, "utc_offset_seconds", None)
    if off is None:
        return None
    try:
        return int(off)
    except (TypeError, ValueError):
        return None


def local_now(group) -> datetime:
    """UTC now shifted into the group's city local time. Falls back to UTC when
    the offset is unknown (caller treats unknown local time as 'not in window')."""
    off = utc_offset_seconds(group)
    base = datetime.utcnow()
    return base if off is None else base + timedelta(seconds=off)


def local_hour(group) -> int | None:
    """City local hour [0-23], or None when the offset is unknown — so exits that
    require a post-peak window safely hold rather than firing on UTC time."""
    if utc_offset_seconds(group) is None:
        return None
    return local_now(group).hour
