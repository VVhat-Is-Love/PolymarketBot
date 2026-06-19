"""A1: local-city-time helpers; A2: tail-exit decision (FIX-2: take_profit only)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.strategy.local_time import local_now, local_hour, utc_offset_seconds
from src.strategy.live_trader import decide_tail_exit

TP, HF, TG = 0.99, 0.45, 0.55   # take_profit / hard_floor / time_gate


def _grp(offset):
    return SimpleNamespace(utc_offset_seconds=offset)


# ---------------------------------------------------------------------------
# A1: local time
# ---------------------------------------------------------------------------

def test_local_hour_applies_offset():
    g = _grp(-18000)
    expected = (datetime.utcnow() + timedelta(seconds=-18000)).hour
    assert local_hour(g) == expected
    assert utc_offset_seconds(g) == -18000


def test_local_hour_none_when_offset_unknown():
    assert local_hour(_grp(None)) is None
    assert local_hour(None) is None
    assert abs((local_now(_grp(None)) - datetime.utcnow()).total_seconds()) < 2


# ---------------------------------------------------------------------------
# A2: FIX-2 — take_profit active; time_gate / hard_floor removed → hold
# ---------------------------------------------------------------------------

def test_take_profit_fires_at_or_above_threshold():
    """bid ≥ take_profit → take_profit regardless of in_window."""
    assert decide_tail_exit(0.99, in_window=False, take_profit=TP, hard_floor=HF, time_gate=TG) == "take_profit"
    assert decide_tail_exit(0.995, in_window=True, take_profit=TP, hard_floor=HF, time_gate=TG) == "take_profit"
    assert decide_tail_exit(1.0, in_window=False, take_profit=TP, hard_floor=HF, time_gate=TG) == "take_profit"


def test_below_take_profit_always_holds():
    """FIX-2: time_gate and hard_floor branches removed — all other bids → hold."""
    cases = [
        (0.989, False),  # just below TP, pre-window
        (0.989, True),   # just below TP, post-peak
        (0.80, True),    # healthy mid-range
        (0.50, True),    # former time_gate territory → now hold
        (0.44, True),    # former hard_floor territory → now hold
        (0.44, False),   # thin-book noise pre-window → hold
        (0.45, True),    # former hard_floor exact boundary → hold
        (0.01, True),    # near-zero bid → hold (no sub-notional SELL attempt)
    ]
    for bid, win in cases:
        result = decide_tail_exit(bid, in_window=win, take_profit=TP, hard_floor=HF, time_gate=TG)
        assert result == "hold", f"Expected hold for bid={bid} in_window={win}, got {result!r}"
