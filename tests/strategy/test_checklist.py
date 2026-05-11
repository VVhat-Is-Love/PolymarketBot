from datetime import date, datetime
from unittest.mock import patch, MagicMock

import pytest

from src.db.models import MarketGroup, Market, WeatherSnapshot
from src.strategy.checklist import evaluate_checklist


def _make_group(weather_station="JFK", unit="F", metric="high_temp") -> MarketGroup:
    g = MarketGroup()
    g.group_id = "test-group"
    g.city = "New York"
    g.metric = metric
    g.resolution_date = date(2026, 5, 10)
    g.weather_station = weather_station
    g.unit = unit
    g.event_volume = 10000.0
    g.is_active = True
    return g


def _make_market(
    market_id: str,
    bin_min: float,
    bin_max: float,
    price: float = 0.20,
    volume: float = 5000.0,
    created_hours_ago: float = 48.0,
) -> Market:
    m = Market()
    m.market_id = market_id
    m.group_id = "test-group"
    m.question = f"bin {bin_min}-{bin_max}"
    m.bin_min = bin_min
    m.bin_max = bin_max
    m.bin_label = f"{bin_min}-{bin_max}"
    m.is_active = True
    m.volume = volume
    # created_at represents discovery time — set far enough in the past by default
    from datetime import timedelta
    m.created_at = datetime.utcnow() - timedelta(hours=created_hours_ago)
    return m


def _make_forecast(temp_max: float, temp_min: float, source: str) -> WeatherSnapshot:
    ws = WeatherSnapshot()
    ws.city = "New York"
    ws.source = source
    ws.temp_forecast_max = temp_max
    ws.temp_forecast_min = temp_min
    ws.forecast_date = date(2026, 5, 10)
    return ws


MARKETS = [
    _make_market("m1", 55.0, 60.0),
    _make_market("m2", 60.0, 65.0),
    _make_market("m3", 65.0, 70.0),
    _make_market("m4", 70.0, 75.0),
    _make_market("m5", 75.0, 80.0),
]

PRICES = {m.market_id: 0.18 for m in MARKETS}

# Three models agree closely: ~20°C → ~68°F
GOOD_FORECASTS = [
    _make_forecast(20.0, 15.0, "open_meteo:ecmwf_ifs025"),
    _make_forecast(20.5, 15.5, "open_meteo:gfs_global"),
    _make_forecast(20.2, 15.2, "open_meteo:icon_global"),
]


def _mock_ss(
    *,
    min_model_agreement: int = 3,
    max_model_disagreement_c: float = 1.0,
    max_basket_sum: float = 0.96,
    min_edge_percent: float = 10.0,
    min_bin_price: float = 0.0,
    min_bin_volume_usd: float = 0.0,
    min_market_age_hours: float = 0.0,
    max_edge: float = 0.0,
):
    """Return a mock strategy_settings with all required attributes set."""
    m = MagicMock()
    m.strategy_min_model_agreement = min_model_agreement
    m.strategy_max_model_disagreement_c = max_model_disagreement_c
    m.strategy_max_basket_sum = max_basket_sum
    m.strategy_min_edge_percent = min_edge_percent
    m.strategy_min_bin_price = min_bin_price
    m.strategy_min_bin_volume_usd = min_bin_volume_usd
    m.strategy_min_market_age_hours = min_market_age_hours
    m.strategy_max_edge = max_edge
    return m


def _mock_bss(*, neighbors: int = 1, include_upside: bool = False, stake: float = 10.0):
    m = MagicMock()
    m.strategy_basket_neighbors = neighbors
    m.strategy_basket_include_upside = include_upside
    m.strategy_virtual_stake_usd = stake
    return m


# ── Existing tests (updated to use _mock_ss) ──────────────────────────────


def test_passes_when_all_criteria_met():
    with patch("src.strategy.checklist.ss", _mock_ss()), \
         patch("src.strategy.basket_builder.ss", _mock_bss()):
        result = evaluate_checklist(_make_group(), MARKETS, PRICES, GOOD_FORECASTS)

    assert result.passed is True
    assert result.rules_ok is True
    assert result.consensus_ok is True
    assert result.sum_of_prices_ok is True
    assert result.edge_ok is True


def test_fails_no_weather_station():
    result = evaluate_checklist(_make_group(weather_station=None), MARKETS, PRICES, GOOD_FORECASTS)
    assert result.passed is False
    assert result.rules_ok is False
    assert result.rejection_reason == "no_weather_station"


def test_fails_model_disagreement():
    bad_forecasts = [
        _make_forecast(18.0, 13.0, "open_meteo:ecmwf_ifs025"),
        _make_forecast(21.5, 16.5, "open_meteo:gfs_global"),
        _make_forecast(22.0, 17.0, "open_meteo:icon_global"),
    ]
    with patch("src.strategy.checklist.ss", _mock_ss()):
        result = evaluate_checklist(_make_group(), MARKETS, PRICES, bad_forecasts)
    assert result.passed is False
    assert result.consensus_ok is False


def test_fails_high_sum_of_prices():
    high_prices = {m.market_id: 0.34 for m in MARKETS}
    with patch("src.strategy.checklist.ss", _mock_ss()), \
         patch("src.strategy.basket_builder.ss", _mock_bss()):
        result = evaluate_checklist(_make_group(), MARKETS, high_prices, GOOD_FORECASTS)
    assert result.passed is False
    assert result.sum_of_prices_ok is False


# ── New tests: cold-start / quality filters ────────────────────────────────


def test_rejects_cold_start_price():
    """Trade #22 scenario: bin prices near zero (0.0005) before market makers arrive."""
    cold_prices = {m.market_id: 0.0005 for m in MARKETS}
    with patch("src.strategy.checklist.ss", _mock_ss(min_bin_price=0.01)), \
         patch("src.strategy.basket_builder.ss", _mock_bss()):
        result = evaluate_checklist(_make_group(), MARKETS, cold_prices, GOOD_FORECASTS)

    assert result.passed is False
    assert result.rejection_reason is not None
    assert result.rejection_reason.startswith("cold_start_price")
    assert result.bin_volumes_snapshot is not None


def test_rejects_edge_too_high():
    """Trade #22 scenario: cold-start prices produce absurd edge (>200%)."""
    cold_prices = {m.market_id: 0.0005 for m in MARKETS}
    # Disable price check so edge_too_high fires first
    with patch("src.strategy.checklist.ss", _mock_ss(min_bin_price=0.0, max_edge=2.0)), \
         patch("src.strategy.basket_builder.ss", _mock_bss()):
        result = evaluate_checklist(_make_group(), MARKETS, cold_prices, GOOD_FORECASTS)

    assert result.passed is False
    assert result.rejection_reason is not None
    assert result.rejection_reason.startswith("edge_too_high")


def test_rejects_low_volume():
    low_vol_markets = [
        _make_market(m.market_id, m.bin_min, m.bin_max, volume=50.0)
        for m in MARKETS
    ]
    prices = {m.market_id: 0.18 for m in low_vol_markets}
    with patch("src.strategy.checklist.ss", _mock_ss(min_bin_volume_usd=200.0)), \
         patch("src.strategy.basket_builder.ss", _mock_bss()):
        result = evaluate_checklist(_make_group(), low_vol_markets, prices, GOOD_FORECASTS)

    assert result.passed is False
    assert result.rejection_reason is not None
    assert result.rejection_reason.startswith("low_volume")
    assert result.bin_volumes_snapshot is not None


def test_rejects_market_too_young():
    young_markets = [
        _make_market(m.market_id, m.bin_min, m.bin_max, created_hours_ago=0.5)
        for m in MARKETS
    ]
    prices = {m.market_id: 0.18 for m in young_markets}
    with patch("src.strategy.checklist.ss", _mock_ss(min_market_age_hours=2.0)), \
         patch("src.strategy.basket_builder.ss", _mock_bss()):
        result = evaluate_checklist(_make_group(), young_markets, prices, GOOD_FORECASTS)

    assert result.passed is False
    assert result.rejection_reason is not None
    assert result.rejection_reason.startswith("market_too_young")


def test_rejects_consensus_near_edge():
    """Consensus close to the basket boundary (outer 20%) should be rejected."""
    # Consensus ~68°F. With neighbors=0, basket = single bin 65-70.
    # basket range: 65-70, width=5, inner 60%: 66-69.
    # Consensus at ~68.2°F is within inner range, so we need a tighter basket.
    # Use a 1-bin setup where consensus is at 65.5°F (5% from left edge of 65-70).
    # basket_lo=65, basket_hi=70, width=5, inner_lo=66, inner_hi=69.
    # 65.5 < 66 → consensus_near_edge
    edge_forecasts = [
        _make_forecast(18.6, 13.0, "open_meteo:ecmwf_ifs025"),  # 18.6C→65.5F
        _make_forecast(18.7, 13.1, "open_meteo:gfs_global"),
        _make_forecast(18.5, 12.9, "open_meteo:icon_global"),
    ]
    with patch("src.strategy.checklist.ss", _mock_ss(max_model_disagreement_c=1.0)), \
         patch("src.strategy.basket_builder.ss", _mock_bss(neighbors=0, include_upside=False)):
        result = evaluate_checklist(_make_group(), MARKETS, PRICES, edge_forecasts)

    assert result.passed is False
    assert result.rejection_reason is not None
    assert result.rejection_reason.startswith("consensus_near_edge")


def test_consensus_offset_is_stored_on_pass():
    """consensus_offset is computed and returned even when the trade passes."""
    with patch("src.strategy.checklist.ss", _mock_ss()), \
         patch("src.strategy.basket_builder.ss", _mock_bss()):
        result = evaluate_checklist(_make_group(), MARKETS, PRICES, GOOD_FORECASTS)

    assert result.passed is True
    assert result.consensus_offset is not None


def test_quality_filters_disabled_when_thresholds_zero():
    """When all thresholds are 0 (disabled), a cold-start market still passes price / edge."""
    # All thresholds 0 → quality checks skipped
    cold_prices = {m.market_id: 0.0005 for m in MARKETS}
    # sum_prices = 3 * 0.0005 ≈ 0.0015, edge ≈ 666x — but max_edge=0 (disabled)
    # sum_ok: 0.0015 <= 0.96 → True. edge_ok: 666 >= 10% → True. Should pass.
    with patch("src.strategy.checklist.ss", _mock_ss(max_edge=0.0, min_bin_price=0.0)), \
         patch("src.strategy.basket_builder.ss", _mock_bss()):
        result = evaluate_checklist(_make_group(), MARKETS, cold_prices, GOOD_FORECASTS)

    assert result.passed is True
