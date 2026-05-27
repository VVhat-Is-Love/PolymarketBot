"""
Tests for basket_builder.build_basket().

The builder's job: given markets and a price dict, return a contiguous list of
BasketItems anchored on the MARKET FAVOURITE (highest ask price), expanded to
neighbouring bins up to basket_max_bins.

KEY DESIGN DECISIONS (as of the current basket_builder):
  * target_temp is accepted but IGNORED — selection is price-based.
  * Stakes and shares are NOT set here (always 0.0 / None).
    Sizing is the job of basket_decision._size_basket.
  * Returns [] when no bins have valid prices.
"""
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


# ---------------------------------------------------------------------------
# Basic behaviour
# ---------------------------------------------------------------------------

def test_favourite_is_in_basket():
    """The bin with the highest ask price is always in the basket."""
    prices = {m.market_id: 0.10 for m in MARKETS}
    prices["m3"] = 0.55    # m3 is the clear favourite
    basket = build_basket(MARKETS, 72.0, prices)
    ids = [item.market.market_id for item in basket]
    assert "m3" in ids


def test_no_priced_bins_returns_empty():
    """build_basket returns [] when no bins have valid prices."""
    basket = build_basket(MARKETS, 72.0, {})
    assert basket == []


def test_inactive_bins_excluded():
    """Bins with is_active=False are not included."""
    inactive_markets = list(MARKETS)
    dead = _make_market("m99", 85.0, 90.0)
    dead.is_active = False
    inactive_markets.append(dead)
    basket = build_basket(inactive_markets, 72.0, {**PRICES, "m99": 0.50})
    ids = [item.market.market_id for item in basket]
    assert "m99" not in ids


# ---------------------------------------------------------------------------
# basket_max_bins cap
# ---------------------------------------------------------------------------

def test_respects_basket_max_bins():
    """build_basket never returns more than basket_max_bins bins."""
    with patch("src.strategy.basket_builder.ss") as mock_ss:
        mock_ss.basket_max_bins = 3
        basket = build_basket(MARKETS, 72.0, PRICES)
    assert len(basket) <= 3


def test_single_bin_when_max_bins_is_1():
    """With basket_max_bins=1, exactly the favourite is returned."""
    prices = {m.market_id: 0.10 for m in MARKETS}
    prices["m4"] = 0.60    # m4 is the favourite
    with patch("src.strategy.basket_builder.ss") as mock_ss:
        mock_ss.basket_max_bins = 1
        basket = build_basket(MARKETS, 72.0, prices)
    assert len(basket) == 1
    assert basket[0].market.market_id == "m4"


# ---------------------------------------------------------------------------
# Sizing is NOT the builder's responsibility
# ---------------------------------------------------------------------------

def test_stake_not_set_by_builder():
    """stake_usd is always 0.0 out of build_basket — set later by basket_decision."""
    with patch("src.strategy.basket_builder.ss") as mock_ss:
        mock_ss.basket_max_bins = 5
        basket = build_basket(MARKETS, 72.0, PRICES)
    for item in basket:
        assert item.stake_usd == 0.0


def test_shares_not_set_by_builder():
    """shares is always None out of build_basket — set later by basket_decision."""
    with patch("src.strategy.basket_builder.ss") as mock_ss:
        mock_ss.basket_max_bins = 5
        basket = build_basket(MARKETS, 72.0, PRICES)
    for item in basket:
        assert item.shares is None


# ---------------------------------------------------------------------------
# Expansion direction
# ---------------------------------------------------------------------------

def test_expands_to_both_sides_when_possible():
    """When the favourite is in the middle, expansion reaches both neighbours."""
    prices = {m.market_id: 0.10 for m in MARKETS}
    prices["m3"] = 0.50    # m3 (middle bin) is the favourite
    with patch("src.strategy.basket_builder.ss") as mock_ss:
        mock_ss.basket_max_bins = 3
        basket = build_basket(MARKETS, 72.0, prices)
    ids = [item.market.market_id for item in basket]
    assert "m3" in ids
    # Both adjacent bins are included (m2 and m4 are symmetric and have equal price)
    assert len(basket) == 3


def test_skips_unpriced_bins():
    """Bins missing from the price dict are skipped during expansion."""
    prices = {m.market_id: 0.20 for m in MARKETS}
    del prices["m2"]    # gap: m1, [no m2], m3, m4, m5
    with patch("src.strategy.basket_builder.ss") as mock_ss:
        mock_ss.basket_max_bins = 5
        basket = build_basket(MARKETS, 72.0, prices)
    ids = [item.market.market_id for item in basket]
    assert "m2" not in ids
