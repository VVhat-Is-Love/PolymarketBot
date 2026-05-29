"""Tests for RiskManager — all in-memory, no DB or network."""
import pytest
from unittest.mock import patch


def _make_rm(**kwargs):
    """Create a fresh RiskManager with overridden settings."""
    defaults = dict(
        max_single_bet_usd=3.0,
        max_daily_bets=5,
        max_concurrent_bets=3,
        daily_loss_limit_usd=10.0,
        total_stop_loss_usd=20.0,
        order_timeout_minutes=30,
        kelly_fraction=0.25,
        trading_mode="live",
        # Capital cap settings (P1-6)
        max_basket_legs_open=12,
        max_tail_positions=3,
        total_deployed_cap_usd=40.0,
        basket_max_usd=26.0,
        tail_max_usd=18.0,
    )
    defaults.update(kwargs)

    with patch("src.risk.risk_manager.RiskManager._load_settings"):
        from src.risk.risk_manager import RiskManager
        rm = RiskManager.__new__(RiskManager)
        import threading
        rm._lock = threading.Lock()
        rm._max_single_bet_usd = defaults["max_single_bet_usd"]
        rm._max_daily_bets = defaults["max_daily_bets"]
        rm._max_concurrent_bets = defaults["max_concurrent_bets"]
        rm._daily_loss_limit_usd = defaults["daily_loss_limit_usd"]
        rm._total_stop_loss_usd = defaults["total_stop_loss_usd"]
        rm._max_basket_legs_open = defaults["max_basket_legs_open"]
        rm._max_tail_positions = defaults["max_tail_positions"]
        rm._total_deployed_cap_usd = defaults["total_deployed_cap_usd"]
        rm._basket_max_usd = defaults["basket_max_usd"]
        rm._tail_max_usd = defaults["tail_max_usd"]
        rm._reset_daily_state()
        rm._total_loss = 0.0
        rm._emergency_stop = False
        rm._emergency_stop_reason = ""
        rm._emergency_stop_type = ""
        rm._open_bets = {}
        rm._reserved_usd = 0.0
        rm._basket_legs_open = 0
        # Disable DB sync so tests run against pure in-memory state
        rm._load_live_stats = lambda: None
        rm._get_deployed_by_strategy = lambda: {}
        return rm


# ---------------------------------------------------------------------------
# can_place_order
# ---------------------------------------------------------------------------

def test_allows_normal_bet():
    rm = _make_rm()
    ok, reason = rm.can_place_order(2.0)
    assert ok is True
    assert reason == "ok"


def test_emergency_stop_blocks():
    rm = _make_rm()
    rm.set_emergency_stop("test reason")
    ok, reason = rm.can_place_order(1.0)
    assert ok is False
    assert "Emergency stop" in reason
    assert "test reason" in reason


def test_clears_emergency_stop():
    rm = _make_rm()
    rm.set_emergency_stop("temporary")
    rm.clear_emergency_stop()
    ok, _ = rm.can_place_order(1.0)
    assert ok is True


def test_daily_loss_limit_blocks():
    rm = _make_rm(daily_loss_limit_usd=10.0)
    rm._daily_loss = 10.0
    ok, reason = rm.can_place_order(1.0)
    assert ok is False
    assert "Daily loss limit" in reason


def test_total_stop_loss_triggers_emergency_stop():
    rm = _make_rm(total_stop_loss_usd=20.0)
    rm._total_loss = 20.0
    ok, reason = rm.can_place_order(1.0)
    assert ok is False
    assert rm._emergency_stop is True
    assert "Total stop-loss" in reason


def test_max_concurrent_bets_blocks():
    rm = _make_rm(max_concurrent_bets=2)
    rm._open_bets = {"g1": 1.0, "g2": 1.0}
    ok, reason = rm.can_place_order(1.0)
    assert ok is False
    assert "concurrent" in reason.lower()


def test_daily_bet_count_blocks():
    rm = _make_rm(max_daily_bets=3)
    rm._daily_bets_count = 3
    ok, reason = rm.can_place_order(1.0)
    assert ok is False
    assert "Daily bet count" in reason


def test_cap_bet_size():
    rm = _make_rm(max_single_bet_usd=3.0)
    assert rm.cap_bet_size(10.0) == 3.0
    assert rm.cap_bet_size(2.0) == 2.0


# ---------------------------------------------------------------------------
# record_bet_placed / record_bet_result
# ---------------------------------------------------------------------------

def test_record_bet_placed_increments_count():
    rm = _make_rm()
    rm.record_bet_placed("g1", 2.0)
    assert rm._daily_bets_count == 1
    assert "g1" in rm._open_bets


def test_record_bet_result_updates_pnl_and_removes_open_bet():
    rm = _make_rm()
    rm.record_bet_placed("g1", 2.0)
    rm.record_bet_result(trade_id=1, group_id="g1", pnl_usd=1.5)
    assert rm._daily_pnl == pytest.approx(1.5)
    assert "g1" not in rm._open_bets


def test_record_bet_result_tracks_loss():
    rm = _make_rm()
    rm.record_bet_placed("g1", 2.0)
    rm.record_bet_result(trade_id=1, group_id="g1", pnl_usd=-2.0)
    assert rm._daily_loss == pytest.approx(2.0)
    assert rm._total_loss == pytest.approx(2.0)
    assert rm._daily_pnl == pytest.approx(-2.0)


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

def test_get_status_reflects_state():
    rm = _make_rm()
    rm._daily_pnl = 3.0
    rm._daily_loss = 1.0
    rm._open_bets = {"g1": 1.5}
    rm._daily_bets_count = 2

    s = rm.get_status()
    assert s.daily_pnl == pytest.approx(3.0)
    assert s.daily_loss == pytest.approx(1.0)
    assert s.concurrent_open_bets == 1
    assert s.daily_bets_count == 2
    assert s.emergency_stop is False


# ---------------------------------------------------------------------------
# Order priority (checks applied in spec order 1→7)
# ---------------------------------------------------------------------------

def test_emergency_stop_checked_before_daily_loss():
    """Emergency stop (check 1) must fire before daily loss (check 3)."""
    rm = _make_rm(daily_loss_limit_usd=10.0)
    rm._daily_loss = 15.0
    rm.set_emergency_stop("priority test")
    ok, reason = rm.can_place_order(1.0)
    assert ok is False
    assert "Emergency stop" in reason  # not "Daily loss"
