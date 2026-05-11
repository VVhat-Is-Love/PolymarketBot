import pytest
from unittest.mock import patch
from src.db.models import Market
from src.strategy.basket_builder import build_basket, BasketItem


def _make_market(market_id: str, bin_min: float, bin_max: float) -> Market:
    m = Market()
    m.market_id = market_id
    m.question = f"bin {bin_min}-{bin_max}"
    m.bin_min = bin_min
    m.bin_max = bin_max
    m.bin_label = f"{bin_min}-{bin_max}"
    m.is_active = True
    return m


MARKETS = [
    _make_market("m1", 60.0, 65.0),
    _make_market("m2", 65.0, 70.0),
    _make_market("m3", 70.0, 75.0),
    _make_market("m4", 75.0, 80.0),
    _make_market("m5", 80.0, 85.0),
]

PRICES = {m.market_id: 0.20 for m in MARKETS}


def test_center_bin_found():
    basket = build_basket(MARKETS, 72.0, PRICES)
    ids = [item.market.market_id for item in basket]
    assert "m3" in ids  # center bin 70-75 contains 72


def test_outside_range_returns_empty():
    basket = build_basket(MARKETS, 50.0, PRICES)
    assert basket == []


def test_stake_allocated_equally():
    with patch("src.strategy.basket_builder.ss") as mock_ss:
        mock_ss.strategy_basket_neighbors = 1
        mock_ss.strategy_basket_include_upside = False
        mock_ss.strategy_virtual_stake_usd = 10.0
        basket = build_basket(MARKETS, 72.0, PRICES)
    # neighbors=1 → center + 1 each side = 3 bins
    assert len(basket) == 3
    for item in basket:
        assert abs(item.stake_usd - 10.0 / 3) < 0.01


def test_shares_calculated_from_price():
    with patch("src.strategy.basket_builder.ss") as mock_ss:
        mock_ss.strategy_basket_neighbors = 0
        mock_ss.strategy_basket_include_upside = False
        mock_ss.strategy_virtual_stake_usd = 10.0
        basket = build_basket(MARKETS, 72.0, PRICES)
    assert len(basket) == 1
    item = basket[0]
    assert item.shares == pytest.approx(10.0 / 0.20, rel=1e-4)
