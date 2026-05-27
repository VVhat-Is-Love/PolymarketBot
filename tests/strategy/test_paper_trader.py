from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from src.strategy.paper_trader import _get_latest_prices, _get_latest_snapshots, _get_forecasts_for_group
from src.db.models import MarketGroup, Market, MarketSnapshot, WeatherSnapshot, PaperTrade


def _make_group(city="New York", metric="high_temp", resolution_date=None) -> MarketGroup:
    if resolution_date is None:
        resolution_date = date.today() + timedelta(days=10)
    g = MarketGroup()
    g.group_id = "test-group"
    g.city = city
    g.metric = metric
    g.resolution_date = resolution_date
    g.weather_station = "JFK"
    g.unit = "F"
    g.is_active = True
    return g


# ---------------------------------------------------------------------------
# _get_latest_prices
# ---------------------------------------------------------------------------

def test_get_latest_prices_returns_price_yes():
    snap = MagicMock(spec=MarketSnapshot)
    snap.price_yes = 0.42

    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = snap

    prices = _get_latest_prices(session, ["market-1"])
    assert prices == {"market-1": 0.42}


def test_get_latest_prices_returns_none_when_no_snapshot():
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = None

    prices = _get_latest_prices(session, ["market-x"])
    assert prices == {"market-x": None}


def test_get_latest_prices_handles_multiple_markets():
    def _make_snap(price):
        s = MagicMock(spec=MarketSnapshot)
        s.price_yes = price
        return s

    snaps = [_make_snap(0.30), _make_snap(0.50)]
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        result = MagicMock()
        result.scalars.return_value.first.return_value = snaps[call_count]
        call_count += 1
        return result

    session = MagicMock()
    session.execute.side_effect = side_effect

    prices = _get_latest_prices(session, ["m1", "m2"])
    assert prices["m1"] == 0.30
    assert prices["m2"] == 0.50


# ---------------------------------------------------------------------------
# _get_forecasts_for_group
# ---------------------------------------------------------------------------

def _make_weather_snap(source: str, temp_max: float) -> WeatherSnapshot:
    ws = WeatherSnapshot()
    ws.city = "New York"
    ws.source = source
    ws.temp_forecast_max = temp_max
    ws.temp_forecast_min = temp_max - 5
    ws.forecast_date = date(2026, 5, 10)
    ws.snapshot_time = datetime(2026, 5, 3, 12, 0, 0)
    return ws


def test_get_forecasts_deduplicates_by_source():
    # Two rows with the same source → only the first (most recent) is kept
    ws1 = _make_weather_snap("open_meteo:ecmwf_ifs025", 20.0)
    ws2 = _make_weather_snap("open_meteo:ecmwf_ifs025", 19.0)  # older duplicate
    ws3 = _make_weather_snap("open_meteo:gfs_global", 21.0)

    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [ws1, ws2, ws3]

    group = _make_group()
    result = _get_forecasts_for_group(session, group)

    sources = [r.source for r in result]
    assert len(result) == 2
    assert sources.count("open_meteo:ecmwf_ifs025") == 1


def test_get_forecasts_returns_empty_when_no_rows():
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = []

    group = _make_group()
    result = _get_forecasts_for_group(session, group)
    assert result == []


# ---------------------------------------------------------------------------
# _get_latest_snapshots
# ---------------------------------------------------------------------------

def test_get_latest_snapshots_returns_price_and_bid():
    snap = MagicMock(spec=MarketSnapshot)
    snap.price_yes = 0.35
    snap.best_bid = 0.33

    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = snap

    result = _get_latest_snapshots(session, ["m1"])
    assert result["m1"]["price_yes"] == 0.35
    assert result["m1"]["best_bid"] == 0.33


def test_get_latest_snapshots_returns_none_when_no_snapshot():
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = None

    result = _get_latest_snapshots(session, ["m-missing"])
    assert result["m-missing"]["price_yes"] is None
    assert result["m-missing"]["best_bid"] is None


# ---------------------------------------------------------------------------
# run_paper_trade_engine (high-level smoke tests with full mocking)
# ---------------------------------------------------------------------------

def test_run_paper_trade_engine_no_groups():
    """Engine exits cleanly when no groups are in the time horizon."""
    from src.strategy.paper_trader import run_paper_trade_engine

    with patch("src.strategy.paper_trader.get_session") as mock_get_session:
        session = MagicMock()
        mock_get_session.return_value = session
        # No groups returned
        session.execute.return_value.scalars.return_value.all.return_value = []

        run_paper_trade_engine()

        session.close.assert_called_once()


def test_run_paper_trade_engine_skips_no_price_data():
    """Engine skips groups where all price_yes=None (no snapshot data at all)."""
    from src.strategy.paper_trader import run_paper_trade_engine

    group = _make_group()
    market = MagicMock(spec=Market)
    market.market_id = "m1"
    market.is_active = True
    market.bin_min = 68.0
    market.bin_label = "68-69F"

    # No price data at all (e.g. brand-new market, no snapshot yet)
    no_price_snapshots = {"m1": {"price_yes": None, "best_bid": None}}

    call_count = [0]

    with patch("src.strategy.paper_trader.get_session") as mock_get_session, \
         patch("src.strategy.paper_trader._get_latest_snapshots", return_value=no_price_snapshots), \
         patch("src.strategy.paper_trader._get_forecasts_for_group", return_value=[]):

        session = MagicMock()
        mock_get_session.return_value = session

        def execute_side_effect(stmt):
            result = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                result.scalars.return_value.all.return_value = [group]
            else:
                result.scalars.return_value.all.return_value = [market]
            return result

        session.execute.side_effect = execute_side_effect

        run_paper_trade_engine()

        # Should NOT add any ChecklistEvaluation (skipped before checklist)
        session.add.assert_not_called()
        session.close.assert_called_once()


def test_run_paper_trade_engine_rejected_group():
    """Engine records a failed checklist evaluation and does not open a trade."""
    from src.strategy.paper_trader import run_paper_trade_engine
    from src.strategy.checklist import ChecklistResult

    group = _make_group()
    market = MagicMock(spec=Market)
    market.market_id = "m1"
    market.is_active = True
    market.bin_min = 68.0
    market.bin_label = "68-69F"

    failed_result = ChecklistResult(
        passed=False, rules_ok=False, consensus_ok=False,
        sum_of_prices_ok=False, stability_ok=False, edge_ok=False,
        rejection_reason="no_weather_station",
    )

    call_count = [0]

    mock_snapshots = {"m1": {"price_yes": 0.20, "best_bid": 0.18}}

    with patch("src.strategy.paper_trader.get_session") as mock_get_session, \
         patch("src.strategy.paper_trader.evaluate_checklist", return_value=failed_result), \
         patch("src.strategy.paper_trader._get_latest_snapshots", return_value=mock_snapshots), \
         patch("src.strategy.paper_trader._get_forecasts_for_group", return_value=[]):

        session = MagicMock()
        mock_get_session.return_value = session

        def execute_side_effect(stmt):
            result = MagicMock()
            # First call: groups query → return [group]
            # Second call: markets query → return [market]
            call_count[0] += 1
            if call_count[0] == 1:
                result.scalars.return_value.all.return_value = [group]
            else:
                result.scalars.return_value.all.return_value = [market]
            return result

        session.execute.side_effect = execute_side_effect

        run_paper_trade_engine()

        # Should add ChecklistEvaluation but no PaperTrade
        session.add.assert_called_once()
        session.close.assert_called_once()


def test_run_resolve_paper_trades_no_open_trades():
    """Resolve job exits cleanly when there are no open trades."""
    from src.strategy.paper_trader import run_resolve_paper_trades

    with patch("src.strategy.paper_trader.get_session") as mock_get_session, \
         patch("src.strategy.paper_trader.get_notifier") as mock_notifier:
        mock_notifier.return_value.enabled = False
        mock_notifier.return_value.send = MagicMock(return_value=False)
        session = MagicMock()
        mock_get_session.return_value = session
        session.execute.return_value.scalars.return_value.all.return_value = []

        run_resolve_paper_trades()

        session.close.assert_called_once()


def test_run_resolve_paper_trades_basket_miss():
    """Gamma API reports a winner outside the basket → resolved_basket_miss."""
    from src.strategy.paper_trader import run_resolve_paper_trades
    import json as _json
    from datetime import date as _date

    # Basket only covers m1 and m2
    basket_json = _json.dumps([
        {"market_id": "m1", "bin_label": "60-65", "bin_min": 60.0, "bin_max": 65.0,
         "price_yes": 0.25, "stake_usd": 5.0, "shares": 20.0},
        {"market_id": "m2", "bin_label": "65-70", "bin_min": 65.0, "bin_max": 70.0,
         "price_yes": 0.20, "stake_usd": 5.0, "shares": 25.0},
    ])

    trade = MagicMock(spec=PaperTrade)
    trade.id = 1
    trade.group_id = "g1"
    trade.basket_json = basket_json
    trade.virtual_stake_usd = 10.0
    trade.status = "open"

    group = _make_group(resolution_date=_date(2026, 5, 1))  # past date
    group.group_id = "g1"
    group.unit = "F"
    group.metric = "high_temp"

    # Gamma API says m3 won (outside basket)
    gamma_mock = MagicMock()
    gamma_mock.get_prices_for_group.return_value = {
        "m1": 0.0, "m2": 0.0, "m3": 0.99, "m4": 0.0
    }

    market_m3 = MagicMock(spec=Market)
    market_m3.bin_min = 70.0
    market_m3.bin_max = 75.0

    with patch("src.strategy.paper_trader.get_session") as mock_gs, \
         patch("src.strategy.paper_trader.get_notifier") as mock_notifier, \
         patch("src.data.open_meteo.fetch_actual_temperature", return_value=None), \
         patch("src.config.cities.CITIES_WHITELIST",
               {"New York": {"lat": 40.7, "lon": -74.0, "station": "JFK", "unit": "F"}}):
        mock_notifier.return_value.enabled = False
        mock_notifier.return_value.send = MagicMock(return_value=False)

        session = MagicMock()
        mock_gs.return_value = session

        # open_trades query
        session.execute.return_value.scalars.return_value.all.return_value = [trade]
        # session.get for group
        session.get.side_effect = lambda model, pk: (
            group if model.__name__ == "MarketGroup" else market_m3
        )

        run_resolve_paper_trades(gamma=gamma_mock)

    assert trade.status == "resolved_basket_miss"
    assert trade.basket_miss is True
    assert trade.pnl_usd == -10.0
    assert trade.resolved_via == "polymarket_api"
