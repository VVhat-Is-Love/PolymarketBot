"""A1/A2: local-city-time helpers + tz-aware tail-exit decision."""
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
    # Houston is UTC−5 (DST) / −6. −5h = −18000s.
    g = _grp(-18000)
    expected = (datetime.utcnow() + timedelta(seconds=-18000)).hour
    assert local_hour(g) == expected
    assert utc_offset_seconds(g) == -18000


def test_local_hour_none_when_offset_unknown():
    assert local_hour(_grp(None)) is None
    assert local_hour(None) is None
    # local_now falls back to UTC (no crash) when offset is unknown
    assert abs((local_now(_grp(None)) - datetime.utcnow()).total_seconds()) < 2


# ---------------------------------------------------------------------------
# A2: decision branches in order (acceptance cases verbatim)
# ---------------------------------------------------------------------------

def test_take_profit_fires_any_time():
    assert decide_tail_exit(0.99, in_window=False, take_profit=TP, hard_floor=HF, time_gate=TG) == "take_profit"
    assert decide_tail_exit(0.995, in_window=True, take_profit=TP, hard_floor=HF, time_gate=TG) == "take_profit"


def test_mid_bid_before_window_holds():
    # bid 0.50 before peak / outside window → hold (NOT exit)
    assert decide_tail_exit(0.50, in_window=False, take_profit=TP, hard_floor=HF, time_gate=TG) == "hold"


def test_mid_bid_after_peak_time_gate():
    # bid 0.50 in window → time_gate SELL
    assert decide_tail_exit(0.50, in_window=True, take_profit=TP, hard_floor=HF, time_gate=TG) == "time_gate"


def test_low_bid_after_peak_hard_floor():
    # bid 0.44 in window → hard_floor (checked before time_gate)
    assert decide_tail_exit(0.44, in_window=True, take_profit=TP, hard_floor=HF, time_gate=TG) == "hard_floor"


def test_low_bid_before_peak_holds_noise():
    # bid 0.44 before peak → hold (intraday noise, Chicago 60¢→plus)
    assert decide_tail_exit(0.44, in_window=False, take_profit=TP, hard_floor=HF, time_gate=TG) == "hold"


def test_hard_floor_checked_before_time_gate():
    # exactly at the floor in window → hard_floor, not time_gate
    assert decide_tail_exit(0.45, in_window=True, take_profit=TP, hard_floor=HF, time_gate=TG) == "hard_floor"


def test_high_but_sub_take_profit_in_window_holds():
    # 0.80 in window: not ≤0.45, not <0.55 → hold (a healthy winner, wait for par)
    assert decide_tail_exit(0.80, in_window=True, take_profit=TP, hard_floor=HF, time_gate=TG) == "hold"
