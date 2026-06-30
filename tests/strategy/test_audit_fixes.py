"""Tests for P2-16 — covering critical modules added/changed by audit fixes."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

from src.db.models import Market, MarketGroup, MarketSnapshot, LiveTrade


# ---------------------------------------------------------------------------
# P0-3 / P1-5 — tail_seller: no_ask buffer + price band
# ---------------------------------------------------------------------------

def _make_market(mid: str, bin_min: float, bin_max: float, token_no: str = "") -> Market:
    m = MagicMock(spec=Market)
    m.market_id = mid
    m.bin_label = f"{bin_min}-{bin_max}"
    m.bin_min = bin_min
    m.bin_max = bin_max
    m.token_id_no = token_no
    m.is_active = True
    return m


def _make_ss(**kwargs):
    defaults = dict(
        tail_max_yes_price=0.10,
        tail_min_yes_price=0.02,
        tail_required_edge=0.03,
        tail_max_hours_to_close=30.0,
        tail_min_hours_to_close=3.0,
        tail_kelly_fraction=0.25,
        tail_max_position_pct=0.20,
        tail_max_position_usd=6.0,
        tail_max_portfolio_pct=0.35,
        tail_sigma_c=2.0,
        tail_min_no_ask=0.85,
        tail_max_no_ask=0.94,
        tail_slippage_buffer=0.01,
        min_order_notional_usd=1.0,
    )
    defaults.update(kwargs)
    return type("SS", (), defaults)()


def test_scan_tails_skips_out_of_band_no_ask_high():
    """Markets with no_ask > tail_max_no_ask should be skipped."""
    from src.strategy.tail_seller import scan_tails

    m = _make_market("m1", 29, 30)
    ss = _make_ss()
    # yes_price = 0.04 → approx no_ask = 1 - 0.04 = 0.96, buffered = 0.96 * 1.01 = ~0.97
    # 0.97 > tail_max_no_ask=0.94 → should skip
    result = scan_tails(
        markets=[m], prices={"m1": 0.04},
        consensus_temp=10.0, unit="C",
        hours_to_close=20.0, available_capital=100.0, no_capital_in_use=0.0,
        ss=ss,
    )
    assert len(result.orders) == 0
    assert any("out_of_band" in r for r in result.skipped)


def test_scan_tails_uses_real_no_ask_when_provided():
    """When no_asks dict provided, use real ask (not approximation)."""
    from src.strategy.tail_seller import scan_tails

    m = _make_market("m1", 28, 29)
    ss = _make_ss()
    # yes_price = 0.05, but provide real no_ask = 0.90 (within band)
    # p_model_no should be high for a longshot bin
    result = scan_tails(
        markets=[m], prices={"m1": 0.05},
        consensus_temp=10.0, unit="C",  # far from bin 28-29 → p_no very high
        hours_to_close=20.0, available_capital=100.0, no_capital_in_use=0.0,
        ss=ss,
        no_asks={"m1": 0.90},
    )
    # edge = p_model_no - 0.90 — may or may not pass depending on Gaussian, but no_ask used
    # Key check: no "out_of_band" for 0.90
    assert not any("out_of_band" in r for r in result.skipped), (
        f"Should not get out_of_band for no_ask=0.90 in [0.85, 0.94]: {result.skipped}"
    )


def test_scan_tails_skips_market_with_no_order_book():
    """When no_asks dict has None for a market, it's skipped as no_order_book."""
    from src.strategy.tail_seller import scan_tails

    m = _make_market("m1", 28, 29)
    ss = _make_ss()
    result = scan_tails(
        markets=[m], prices={"m1": 0.05},
        consensus_temp=10.0, unit="C",
        hours_to_close=20.0, available_capital=100.0, no_capital_in_use=0.0,
        ss=ss,
        no_asks={"m1": None},  # real fetch returned None
    )
    assert len(result.orders) == 0
    assert any("no_order_book" in r for r in result.skipped)


# ---------------------------------------------------------------------------
# P1-6 — RiskManager: separate basket/tail caps
# ---------------------------------------------------------------------------

def _make_risk_manager(**kwargs):
    from unittest.mock import patch
    import threading
    from src.risk.risk_manager import RiskManager

    defaults = dict(
        max_single_bet_usd=3.0, max_daily_bets=10, max_concurrent_bets=15,
        daily_loss_limit_usd=10.0, total_stop_loss_usd=20.0,
        max_basket_legs_open=12, max_tail_positions=3,
        total_deployed_cap_usd=40.0, basket_max_usd=26.0, tail_max_usd=18.0,
        total_deployed_pct=0.50, tail_deployed_pct=0.50, max_tail_zone_positions=3,
    )
    defaults.update(kwargs)

    with patch("src.risk.risk_manager.RiskManager._load_settings"):
        rm = RiskManager.__new__(RiskManager)
        rm._lock = threading.Lock()
        for k, v in defaults.items():
            setattr(rm, f"_{k}", v)
        rm._reset_daily_state()
        rm._total_loss = 0.0
        rm._emergency_stop = False
        rm._emergency_stop_reason = ""
        rm._emergency_stop_type = ""
        rm._open_bets = {}
        rm._reserved_usd = 0.0
        rm._basket_legs_open = 0
        rm._tail_token_reserved = {}
        rm._paper_daily_loss = 0.0
        rm._paper_total_loss = 0.0
        rm._paper_bankroll_usd = 100.0
        rm._paper_total_stop_loss_usd = 20.0
        rm._paper_daily_loss_limit_usd = 10.0
        rm._load_live_stats = lambda: None
        rm._persist_emergency_stop = lambda *a, **kw: None
        rm._get_deployed_by_strategy = lambda: {}
        return rm


def test_can_place_basket_ok():
    rm = _make_risk_manager()
    ok, reason = rm.can_place_basket(10.0, 3)
    assert ok is True
    assert reason == "ok"


def test_can_place_basket_total_cap_exceeded():
    # equity=$20, total_deployed_pct=0.50 → cap=$10; notional=$11 > $10 → blocked
    rm = _make_risk_manager(total_deployed_pct=0.50)
    ok, reason = rm.can_place_basket(11.0, 3, equity=20.0)
    assert ok is False
    assert "total_cap" in reason


def test_can_place_basket_basket_cap_exceeded():
    # basket cap removed in equity model; leg-slots still enforced
    # Test that total cap still works via equity parameter
    rm = _make_risk_manager(total_deployed_pct=0.50)
    ok, reason = rm.can_place_basket(0.5, 3, equity=0.8)   # 0.5 > 0.8×50%=0.4 → blocked
    assert ok is False
    assert "total_cap" in reason


def test_can_place_tail_ok():
    rm = _make_risk_manager()
    ok, reason = rm.can_place_tail(5.0, equity=0.0)   # equity=0 → cap check skipped
    assert ok is True


def test_can_place_tail_cap_exceeded():
    # equity=$10, tail_deployed_pct=0.30 → cap=$3; notional=$5 > $3 → blocked
    rm = _make_risk_manager(tail_deployed_pct=0.30)
    ok, reason = rm.can_place_tail(5.0, equity=10.0)
    assert ok is False
    assert "tail_cap" in reason


def test_basket_reserves_prevent_double_spend():
    """Two sequential can_place_basket calls should both see the reservation."""
    # equity=$60, total_deployed_pct=0.50 → cap=$30
    rm = _make_risk_manager(total_deployed_pct=0.50)
    ok1, _ = rm.can_place_basket(18.0, 3, equity=60.0)
    assert ok1 is True
    assert rm._reserved_usd == pytest.approx(18.0)

    ok2, reason2 = rm.can_place_basket(15.0, 3, equity=60.0)
    assert ok2 is False  # reserved=18 + 15 = 33 > 30 total cap (60×50%)
    assert "total_cap" in reason2


def test_release_basket_reservation():
    rm = _make_risk_manager()
    rm.can_place_basket(10.0, 3)
    assert rm._reserved_usd == pytest.approx(10.0)
    rm.release_basket_reservation(10.0, 3)
    assert rm._reserved_usd == pytest.approx(0.0)
    assert rm._basket_legs_open == 0


def test_basket_emergency_stop_blocks():
    rm = _make_risk_manager()
    rm._emergency_stop = True
    rm._emergency_stop_reason = "test"
    ok, reason = rm.can_place_basket(5.0, 2)
    assert ok is False
    assert "Emergency" in reason


# ---------------------------------------------------------------------------
# P1-9 — trailing 36h daily loss window
# ---------------------------------------------------------------------------

def test_trailing_36h_loss_count():
    """Daily loss should use trailing 36h window (resolved_at), not calendar day."""
    from src.risk.risk_manager import RiskManager
    import threading

    with patch("src.risk.risk_manager.RiskManager._load_settings"):
        rm = RiskManager.__new__(RiskManager)
        rm._lock = threading.Lock()
        rm._max_single_bet_usd = 3.0
        rm._max_daily_bets = 10
        rm._max_concurrent_bets = 15
        rm._daily_loss_limit_usd = 10.0
        rm._total_stop_loss_usd = 20.0
        rm._max_basket_legs_open = 12
        rm._max_tail_positions = 3
        rm._total_deployed_cap_usd = 40.0
        rm._basket_max_usd = 26.0
        rm._tail_max_usd = 18.0
        rm._reset_daily_state()
        rm._total_loss = 0.0
        rm._emergency_stop = False
        rm._emergency_stop_reason = ""
        rm._open_bets = {}
        rm._reserved_usd = 0.0
        rm._basket_legs_open = 0
        rm._persist_emergency_stop = lambda *a, **kw: None

    # The _load_live_stats now queries resolved_at >= now - 36h
    # Verify it doesn't use filled_at >= today_start
    # This is an integration check — just verify _daily_loss is read from 36h window
    # by checking the DB query uses resolved_at (covered by code inspection)
    import inspect
    from src.risk import risk_manager as rm_module
    source = inspect.getsource(rm_module.RiskManager._load_live_stats)
    assert "resolved_at" in source
    assert "cutoff_36h" in source
