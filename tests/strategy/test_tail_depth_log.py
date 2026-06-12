"""Tests for _log_no_depth: NO-ask book depth logging for tail candidates."""
from unittest.mock import patch

import pytest

from src.strategy.live_trader import _log_no_depth


# ---------------------------------------------------------------------------
# _log_no_depth: smoke tests (must never raise)
# ---------------------------------------------------------------------------

class TestLogNoDepth:
    def test_no_crash_on_unavailable_book(self):
        with patch("src.market.clob_client.get_no_book_depth", return_value=None):
            _log_no_depth("Miami", "88-89°F", "tok-abc")  # must not raise

    def test_no_crash_on_empty_levels(self):
        fake = {"ask_levels": [], "spread": None, "no_liquidity": True}
        with patch("src.market.clob_client.get_no_book_depth", return_value=fake):
            _log_no_depth("Chicago", "85-86°F", "tok-xyz")  # must not raise

    def test_no_crash_on_normal_book(self):
        fake = {
            "ask_levels": [(0.88, 60.0), (0.90, 40.0), (0.91, 25.0)],
            "spread": 0.03,
            "no_liquidity": False,
        }
        with patch("src.market.clob_client.get_no_book_depth", return_value=fake):
            _log_no_depth("Dallas", "90-91°F", "tok-123")  # must not raise

    def test_no_crash_on_exception_in_depth_fetch(self):
        with patch("src.market.clob_client.get_no_book_depth",
                   side_effect=RuntimeError("timeout")):
            _log_no_depth("Seattle", "80-81°F", "tok-err")  # must not raise

    def test_no_crash_on_missing_spread_key(self):
        fake = {"ask_levels": [(0.89, 50.0)]}  # spread key absent
        with patch("src.market.clob_client.get_no_book_depth", return_value=fake):
            _log_no_depth("Phoenix", "95-96°F", "tok-nospread")  # must not raise


# ---------------------------------------------------------------------------
# depth_to_0.90 arithmetic (pure, no patching needed)
# ---------------------------------------------------------------------------

class TestDepthArithmetic:
    def test_depth_to_90_includes_boundary(self):
        levels = [(0.88, 60.0), (0.90, 40.0), (0.91, 25.0)]
        depth = sum(s for p, s in levels if p <= 0.90)
        assert depth == pytest.approx(100.0)

    def test_depth_to_90_zero_when_all_above(self):
        levels = [(0.91, 50.0), (0.93, 30.0)]
        depth = sum(s for p, s in levels if p <= 0.90)
        assert depth == pytest.approx(0.0)

    def test_depth_to_90_empty_levels(self):
        depth = sum(s for p, s in [] if p <= 0.90)
        assert depth == pytest.approx(0.0)
