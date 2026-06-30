"""Tests for ask_levels in _parse_book_response and get_no_book_depth."""
from unittest.mock import patch

import pytest

from src.market.clob_client import _parse_book_response, get_no_book_depth, BookUnavailable


def _book(asks, bids=None):
    return {"asks": asks or [], "bids": bids or []}


# ---------------------------------------------------------------------------
# ask_levels in _parse_book_response
# ---------------------------------------------------------------------------

class TestAskLevels:
    def test_present_and_sorted_cheapest_first(self):
        raw = _book([
            {"price": "0.91", "size": "45.0"},
            {"price": "0.88", "size": "120.5"},
            {"price": "0.90", "size": "60.0"},
        ])
        result = _parse_book_response(raw)
        levels = result["ask_levels"]
        assert len(levels) == 3
        prices = [p for p, _ in levels]
        assert prices == sorted(prices)

    def test_empty_when_no_real_asks(self):
        result = _parse_book_response(_book([]))
        assert result["ask_levels"] == []

    def test_stub_asks_excluded(self):
        raw = _book([
            {"price": "0.88", "size": "100.0"},
            {"price": "0.97", "size": "5000.0"},   # stub threshold — excluded
            {"price": "0.99", "size": "9999.0"},   # stub — excluded
        ])
        result = _parse_book_response(raw)
        assert len(result["ask_levels"]) == 1
        assert result["ask_levels"][0][0] == pytest.approx(0.88)

    def test_zero_size_excluded(self):
        raw = _book([
            {"price": "0.87", "size": "0"},        # zero size — excluded
            {"price": "0.89", "size": "50.0"},
        ])
        result = _parse_book_response(raw)
        assert len(result["ask_levels"]) == 1

    def test_depth_to_90_computable(self):
        raw = _book([
            {"price": "0.88", "size": "60.0"},
            {"price": "0.90", "size": "40.0"},
            {"price": "0.91", "size": "25.0"},    # above 0.90 — not counted
        ])
        result = _parse_book_response(raw)
        depth_to_90 = sum(s for p, s in result["ask_levels"] if p <= 0.90)
        assert depth_to_90 == pytest.approx(100.0)

    def test_levels_prices_and_sizes_correct(self):
        raw = _book([
            {"price": "0.89", "size": "120.5"},
            {"price": "0.92", "size": "30.0"},
        ])
        result = _parse_book_response(raw)
        levels = result["ask_levels"]
        assert levels[0] == pytest.approx((0.89, 120.5))
        assert levels[1] == pytest.approx((0.92, 30.0))


# ---------------------------------------------------------------------------
# get_no_book_depth
# ---------------------------------------------------------------------------

class TestGetNoBookDepth:
    def test_returns_none_on_book_unavailable(self):
        with patch("src.market.clob_client._get_single_book",
                   side_effect=BookUnavailable("tok")):
            assert get_no_book_depth("tok") is None

    def test_returns_none_on_generic_exception(self):
        with patch("src.market.clob_client._get_single_book",
                   side_effect=RuntimeError("network error")):
            assert get_no_book_depth("tok") is None

    def test_returns_parsed_dict_with_levels(self):
        fake_snap = {
            "best_ask": 0.89,
            "best_bid": 0.86,
            "ask_levels": [(0.89, 100.0), (0.91, 50.0)],
            "spread": 0.03,
            "no_liquidity": False,
            "asset_id": "tok",
        }
        with patch("src.market.clob_client._get_single_book", return_value=fake_snap):
            result = get_no_book_depth("tok")
        assert result is not None
        assert result["best_ask"] == pytest.approx(0.89)
        assert len(result["ask_levels"]) == 2

    def test_returns_none_when_single_book_returns_none(self):
        with patch("src.market.clob_client._get_single_book", return_value=None):
            assert get_no_book_depth("tok") is None
