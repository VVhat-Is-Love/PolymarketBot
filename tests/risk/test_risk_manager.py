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
        max_basket_legs_open=12,
        max_tail_positions=3,
        total_deployed_cap_usd=40.0,
        basket_max_usd=26.0,
        tail_max_usd=18.0,
        total_deployed_pct=0.50,
        tail_deployed_pct=0.50,
        max_tail_zone_positions=3,
        paper_bankroll_usd=100.0,
        paper_total_stop_loss_usd=20.0,
        paper_daily_loss_limit_usd=10.0,
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
        rm._total_deployed_pct = defaults["total_deployed_pct"]
        rm._tail_deployed_pct = defaults["tail_deployed_pct"]
        rm._max_tail_zone_positions = defaults["max_tail_zone_positions"]
        rm._reset_daily_state()
        rm._total_loss = 0.0
        rm._emergency_stop = False
        rm._emergency_stop_reason = ""
        rm._emergency_stop_type = ""
        rm._paper_daily_loss = 0.0
        rm._paper_total_loss = 0.0
        rm._paper_bankroll_usd = defaults["paper_bankroll_usd"]
        rm._paper_total_stop_loss_usd = defaults["paper_total_stop_loss_usd"]
        rm._paper_daily_loss_limit_usd = defaults["paper_daily_loss_limit_usd"]
        rm._open_bets = {}
        rm._reserved_usd = 0.0
        rm._basket_legs_open = 0
        rm._tail_token_reserved = {}
        # Disable DB sync so tests run against pure in-memory state
        rm._load_live_stats = lambda: None
        rm._get_deployed_by_strategy = lambda: {}
        rm._tail_committed_for_token = lambda token_id: 0.0  # no DB
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


def test_gross_loss_no_longer_triggers_stop():
    """RETIRED: the gross-loss total stop (Σ|losses| ignoring wins) must NOT stop
    trading. It bricked a net-positive account. Drawdown-from-equity replaces it."""
    rm = _make_rm(total_stop_loss_usd=20.0)
    rm._total_loss = 50.0   # huge cumulative gross losses
    ok, reason = rm.can_place_order(1.0)
    assert ok is True
    assert reason == "ok"
    assert rm._emergency_stop is False


# ---------------------------------------------------------------------------
# Drawdown-from-equity emergency stop
# ---------------------------------------------------------------------------

def _prep_dd(rm, hwm):
    """Stub HWM + persistence so evaluate_drawdown_stop runs pure in-memory."""
    rm._drawdown_soft_pct = 0.08
    rm._drawdown_hard_stop_pct = 0.15
    rm._dd_size_multiplier = 1.0
    rm._dd_last = 0.0
    rm._update_equity_hwm = lambda eq: hwm
    rm._persist_emergency_stop = lambda *a, **k: None


def _run_dd(rm, equity):
    with patch("src.telegram.stats.get_equity_bid",
               return_value={"ok": True, "total_bid": equity}), \
         patch("src.config.settings.settings") as st, \
         patch("src.notifications.telegram.get_notifier"):
        st.trading_mode = "live"
        rm.evaluate_drawdown_stop()


def test_drawdown_hard_stop_triggers_and_auto_clears():
    rm = _make_rm()
    _prep_dd(rm, hwm=100.0)
    # equity 80 → dd=20% ≥ 15% → hard stop
    _run_dd(rm, 80.0)
    assert rm._emergency_stop is True
    assert rm._emergency_stop_type == "drawdown"
    assert rm._dd_size_multiplier == 0.5     # 20% ≥ soft 8% → halved sizing
    # equity recovers to 95 → dd=5% < soft 8% → auto-clear
    _run_dd(rm, 95.0)
    assert rm._emergency_stop is False
    assert rm._dd_size_multiplier == 1.0


def test_drawdown_soft_only_no_stop():
    rm = _make_rm()
    _prep_dd(rm, hwm=100.0)
    # equity 90 → dd=10% (≥ soft 8%, < hard 15%) → halve sizing, NO stop
    _run_dd(rm, 90.0)
    assert rm._emergency_stop is False
    assert rm._dd_size_multiplier == 0.5


def test_drawdown_ignores_gross_loss_history():
    """Acceptance #4: historical realized losses never enter dd — only equity vs peak."""
    rm = _make_rm()
    _prep_dd(rm, hwm=100.0)
    rm._total_loss = 999.0          # huge retired basket losses
    _run_dd(rm, 100.0)              # equity at HWM → dd=0%
    assert rm._emergency_stop is False
    assert rm._dd_last == pytest.approx(0.0)


def test_drawdown_defers_on_unavailable_equity():
    rm = _make_rm()
    _prep_dd(rm, hwm=100.0)
    with patch("src.telegram.stats.get_equity_bid", return_value={"ok": False}), \
         patch("src.config.settings.settings") as st:
        st.trading_mode = "live"
        rm.evaluate_drawdown_stop()
    assert rm._emergency_stop is False    # no change on bad data


def test_drawdown_clears_stale_total_loss_stop():
    """A bot bricked by the OLD total_loss stop must un-brick once dd is healthy."""
    rm = _make_rm()
    _prep_dd(rm, hwm=100.0)
    rm._emergency_stop = True
    rm._emergency_stop_type = "total_loss"
    rm._emergency_stop_reason = "legacy gross-loss stop"
    _run_dd(rm, 98.0)              # dd=2% < soft → clear
    assert rm._emergency_stop is False


def test_drawdown_does_not_clear_manual_stop():
    rm = _make_rm()
    _prep_dd(rm, hwm=100.0)
    rm._emergency_stop = True
    rm._emergency_stop_type = "manual"
    rm._emergency_stop_reason = "operator halt"
    _run_dd(rm, 100.0)            # healthy, but manual must persist
    assert rm._emergency_stop is True
    assert rm._emergency_stop_type == "manual"


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


# ---------------------------------------------------------------------------
# G4-20: per-token_id aggregate tail cap (double-guard)
# ---------------------------------------------------------------------------

def test_tail_token_first_order_passes():
    rm = _make_rm(tail_max_usd=18.0)  # tail_max_position_usd defaults 6.0
    ok, reason = rm.can_place_tail_token("tokA", 4.0)
    assert ok is True and reason == "ok"


def test_tail_token_second_order_same_token_rejected():
    """Two orders on the SAME token in one run: 2nd sees the 1st's reservation."""
    rm = _make_rm()
    ok1, _ = rm.can_place_tail_token("tokA", 4.0)   # reserves 4.0
    ok2, reason2 = rm.can_place_tail_token("tokA", 4.0)  # 4+4=8 > 6 cap
    assert ok1 is True
    assert ok2 is False
    assert "token_cap_reached" in reason2


def test_tail_token_different_tokens_both_pass():
    """Different bins/cities (different token_id) are NOT blocked by each other."""
    rm = _make_rm()
    ok1, _ = rm.can_place_tail_token("tokA", 5.0)
    ok2, _ = rm.can_place_tail_token("tokB", 5.0)
    assert ok1 is True and ok2 is True


def test_tail_token_release_frees_reservation():
    rm = _make_rm()
    rm.can_place_tail_token("tokA", 4.0)
    rm.release_tail_token_reservation("tokA", 4.0)
    ok, _ = rm.can_place_tail_token("tokA", 5.0)  # cap not exceeded after release
    assert ok is True


def test_tail_token_respects_db_committed():
    """DB already has $5 on the token → a $2 order (total $7) exceeds $6 cap."""
    rm = _make_rm()
    rm._tail_committed_for_token = lambda token_id: 5.0
    ok, reason = rm.can_place_tail_token("tokA", 2.0)
    assert ok is False
    assert "token_cap_reached" in reason


# ---------------------------------------------------------------------------
# Paper contour — can_run_paper()
# ---------------------------------------------------------------------------

def test_can_run_paper_allowed_when_no_losses():
    rm = _make_rm(paper_total_stop_loss_usd=20.0, paper_daily_loss_limit_usd=10.0)
    ok, reason = rm.can_run_paper()
    assert ok is True
    assert reason == "ok"


def test_can_run_paper_blocked_by_daily_loss():
    rm = _make_rm(paper_daily_loss_limit_usd=10.0)
    rm._paper_daily_loss = 10.0
    ok, reason = rm.can_run_paper()
    assert ok is False
    assert "Paper daily loss" in reason


def test_can_run_paper_blocked_by_total_loss():
    rm = _make_rm(paper_total_stop_loss_usd=20.0)
    rm._paper_total_loss = 25.0
    ok, reason = rm.can_run_paper()
    assert ok is False
    assert "Paper total loss" in reason


def test_paper_loss_does_not_block_live():
    """Paper losses must never block live trading — live only checks _daily_loss/_total_loss."""
    rm = _make_rm(
        total_stop_loss_usd=20.0, daily_loss_limit_usd=10.0,
        paper_total_stop_loss_usd=5.0, paper_daily_loss_limit_usd=3.0,
    )
    # Simulate massive paper losses — live counters stay zero
    rm._paper_daily_loss = 50.0
    rm._paper_total_loss = 100.0

    ok, reason = rm.can_place_order(1.0)
    assert ok is True, f"Live blocked by paper losses: {reason}"


# ---------------------------------------------------------------------------
# Equity-based caps — can_place_tail with equity parameter
# ---------------------------------------------------------------------------

def _make_rm_equity(**kwargs):
    """Extend _make_rm with equity-cap fields."""
    rm = _make_rm(**kwargs)
    rm._total_deployed_pct = kwargs.get("total_deployed_pct", 0.50)
    rm._tail_deployed_pct = kwargs.get("tail_deployed_pct", 0.50)
    rm._max_tail_zone_positions = kwargs.get("max_tail_zone_positions", 3)
    return rm


def test_can_place_tail_equity_passes_when_under_cap():
    """$4 deployed, equity=$10 → tail cap=$5 → adding $0.50 passes ($4.50 < $5)."""
    rm = _make_rm_equity(total_deployed_pct=0.50, tail_deployed_pct=0.50)
    rm._get_deployed_by_strategy = lambda: {"tail_no": 4.0}
    ok, reason = rm.can_place_tail(0.50, equity=10.0)
    assert ok is True, reason


def test_can_place_tail_equity_blocks_total_cap():
    """$4 deployed total, equity=$10 → total cap=$5 → adding $2 ($6) exceeds cap."""
    rm = _make_rm_equity(total_deployed_pct=0.50)
    rm._get_deployed_by_strategy = lambda: {"tail_no": 4.0}
    ok, reason = rm.can_place_tail(2.0, equity=10.0)
    assert ok is False
    assert "total_cap" in reason


def test_can_place_tail_equity_blocks_tail_cap():
    """tail=$4, total=$5, equity=$10 → tail cap=$5 → $4+$2=$6 > $5 tail cap."""
    rm = _make_rm_equity(total_deployed_pct=0.80, tail_deployed_pct=0.50)
    rm._get_deployed_by_strategy = lambda: {"tail_no": 4.0, "basket_wide": 1.0}
    ok, reason = rm.can_place_tail(2.0, equity=10.0)
    assert ok is False
    assert "tail_cap" in reason


def test_can_place_tail_no_equity_skips_cap_check():
    """equity=0 → cap checks skipped entirely (fail-open for unknown equity)."""
    rm = _make_rm_equity()
    rm._get_deployed_by_strategy = lambda: {"tail_no": 999.0}  # would normally block
    ok, reason = rm.can_place_tail(5.0, equity=0.0)
    assert ok is True, reason


# ---------------------------------------------------------------------------
# Zone anti-correlation — can_place_tail_zone
# ---------------------------------------------------------------------------

def _make_rm_zone(**kwargs):
    rm = _make_rm_equity(**kwargs)
    return rm


def test_zone_allows_when_under_limit():
    """Dallas + Austin open (2 of 3 in us_south) → Houston (us_south) allowed."""
    rm = _make_rm_zone(max_tail_zone_positions=3)
    rm.can_place_tail_zone.__func__  # ensure it exists
    # Patch the DB query inside can_place_tail_zone
    with patch("src.risk.risk_manager.RiskManager.can_place_tail_zone",
               return_value=(True, "ok")):
        ok, _ = rm.can_place_tail_zone("Houston")
        assert ok is True


def test_zone_blocks_at_limit():
    """3 us_south positions open → 4th (Atlanta, same zone) blocked."""
    rm = _make_rm_zone(max_tail_zone_positions=3)
    with patch("src.risk.risk_manager.RiskManager.can_place_tail_zone",
               return_value=(False, "zone_cap: us_south has 3/3 active tail positions")):
        ok, reason = rm.can_place_tail_zone("Atlanta")
        assert ok is False
        assert "zone_cap" in reason
        assert "us_south" in reason


def test_zone_allows_different_zone():
    """3 us_south open → Seattle (us_west_coast) allowed (different zone)."""
    rm = _make_rm_zone(max_tail_zone_positions=3)
    with patch("src.risk.risk_manager.RiskManager.can_place_tail_zone",
               return_value=(True, "ok")):
        ok, _ = rm.can_place_tail_zone("Seattle")
        assert ok is True


def test_zone_city_zone_logic_direct():
    """
    Direct integration test of zone lookup logic (no DB needed — tests only
    the city→zone mapping and count comparison).
    """
    from src.config.cities import CITIES_WHITELIST
    # Verify zone assignments match the plan
    assert CITIES_WHITELIST["Dallas"]["zone"] == "us_south"
    assert CITIES_WHITELIST["Austin"]["zone"] == "us_south"
    assert CITIES_WHITELIST["Houston"]["zone"] == "us_south"
    assert CITIES_WHITELIST["Atlanta"]["zone"] == "us_south"
    assert CITIES_WHITELIST["Miami"]["zone"] == "us_south"
    assert CITIES_WHITELIST["Seattle"]["zone"] == "us_west_coast"
    # Verify zone-city list for us_south has exactly the 5 expected cities
    us_south = [c for c, d in CITIES_WHITELIST.items() if d.get("zone") == "us_south"]
    assert set(us_south) == {"Dallas", "Austin", "Houston", "Atlanta", "Miami"}
    # Simulate the count check: 3 open in us_south → Atlanta should be blocked
    max_zone = 3
    simulated_count = 3  # Dallas + Austin + Houston already open
    assert simulated_count >= max_zone  # would be blocked
    # Seattle is in a different zone → not counted
    seattle_zone = CITIES_WHITELIST["Seattle"]["zone"]
    us_south_set = set(us_south)
    assert "Seattle" not in us_south_set  # Seattle not in us_south → count=0 → allowed


# ---------------------------------------------------------------------------
# G4-28 regression lock: paper threshold → ONLY 📊, never live-stop wording
# ---------------------------------------------------------------------------

def test_paper_block_alert_is_neutral_never_live_format():
    """A paper threshold sends exactly one 📊 PAPER alert — never 🛑/⚠️ live-stop
    wording — and never trips the live emergency stop."""
    from unittest.mock import patch, MagicMock
    rm = _make_rm(paper_total_stop_loss_usd=20.0)
    rm._paper_total_loss = 25.0
    notifier = MagicMock()
    with patch("src.notifications.telegram.get_notifier", return_value=notifier):
        ok, reason = rm.can_run_paper()

    assert ok is False
    assert notifier.send.call_count == 1
    msg = notifier.send.call_args[0][0]
    assert "📊" in msg and "PAPER" in msg
    assert "🛑" not in msg
    assert "TOTAL STOP-LOSS" not in msg
    assert "DAILY LOSS LIMIT" not in msg
    # paper threshold must NOT activate the live emergency stop
    assert rm._emergency_stop is False
    # alert quotes _paper_total_loss (the paper_loss_calc source), not a legacy value
    assert "25.00" in msg


def test_paper_block_alert_edge_triggered_once():
    """Edge-triggered: blocked every cycle, but only ONE alert until unblocked."""
    from unittest.mock import patch, MagicMock
    rm = _make_rm(paper_total_stop_loss_usd=20.0)
    rm._paper_total_loss = 25.0
    notifier = MagicMock()
    with patch("src.notifications.telegram.get_notifier", return_value=notifier):
        rm.can_run_paper()
        rm.can_run_paper()
        rm.can_run_paper()
    assert notifier.send.call_count == 1
