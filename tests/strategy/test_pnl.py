import json
import pytest
from src.strategy.pnl_calculator import calculate_edge, find_winning_market, calculate_pnl


def _make_basket_json(items: list[dict]) -> str:
    return json.dumps(items)


BASKET = _make_basket_json([
    {"market_id": "m1", "bin_label": "60-65", "bin_min": 60.0, "bin_max": 65.0, "price_yes": 0.20, "stake_usd": 3.33, "shares": 16.65},
    {"market_id": "m2", "bin_label": "65-70", "bin_min": 65.0, "bin_max": 70.0, "price_yes": 0.25, "stake_usd": 3.33, "shares": 13.32},
    {"market_id": "m3", "bin_label": "70-75", "bin_min": 70.0, "bin_max": 75.0, "price_yes": 0.20, "stake_usd": 3.34, "shares": 16.70},
])


class TestCalculateEdge:
    def test_positive_edge(self):
        edge = calculate_edge([0.20, 0.25, 0.20])
        total = 0.65
        assert edge == pytest.approx((1.0 - total) / total, rel=1e-6)

    def test_no_edge_when_sum_is_one(self):
        assert calculate_edge([0.5, 0.5]) == 0.0

    def test_no_edge_when_sum_exceeds_one(self):
        assert calculate_edge([0.6, 0.5]) == 0.0


class TestFindWinningMarket:
    def test_finds_correct_bin(self):
        winner = find_winning_market(BASKET, 67.0)
        assert winner == "m2"

    def test_returns_none_outside_all_bins(self):
        winner = find_winning_market(BASKET, 90.0)
        assert winner is None


class TestCalculatePnl:
    def test_win(self):
        pnl, status = calculate_pnl(BASKET, "m1", 10.0)
        assert status == "resolved_win"
        assert pnl == pytest.approx(16.65 - 10.0, rel=1e-3)

    def test_loss_no_winner_in_basket(self):
        pnl, status = calculate_pnl(BASKET, "other_market", 10.0)
        assert status == "resolved_loss"
        assert pnl == -10.0

    def test_loss_no_winning_market_id(self):
        pnl, status = calculate_pnl(BASKET, None, 10.0)
        assert status == "resolved_loss"
        assert pnl == -10.0
