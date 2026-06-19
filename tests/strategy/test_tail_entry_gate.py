"""
Integration tests: k·σ gate in run_tail_engine must prevent place_limit_order on SKIP.

What these tests catch that unit tests of _tail_ksigma_ok miss:
  - SKIP verdict must `continue` past the placement call — not just log.
  - consensus=None must fail-closed (SKIP), not bypass the gate.

Invariant: if SKIP, place_limit_order is never called, not even once.
"""
import pytest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _group(city="Denver", unit="F"):
    g = MagicMock()
    g.city = city
    g.group_id = f"grp-{city.lower()}"
    g.unit = unit
    g.resolution_date = date(2026, 6, 17)
    g.metric = "high_temp"
    g.utc_offset_seconds = -21600
    return g


def _market(bin_lo=94.0, bin_hi=95.0, bin_label="94-95°F", city="Denver"):
    m = MagicMock()
    m.market_id = f"mkt-{city.lower()}"
    m.bin_label = bin_label
    m.bin_min = bin_lo
    m.bin_max = bin_hi
    m.token_id_no = f"tok-{city.lower()}-no"
    m.token_id_yes = f"tok-{city.lower()}-yes"
    m.condition_id = None
    m.volume = 1000.0
    m.is_active = True
    return m


def _order(market_id, bin_label):
    o = MagicMock()
    o.market_id = market_id
    o.bin_label = bin_label
    o.stake_usd = 5.0
    o.shares = 50.0
    o.no_ask = 0.12
    o.p_model_no = 0.85
    o.ev_usd = 1.50
    o.edge = 0.15
    return o


def _session(group, market):
    """
    Mock SQLAlchemy session for run_tail_engine.
    Handles the three execute() calls in order:
      1. no_in_use scalar (LiveTrade sum)
      2. MarketGroup query
      3. Market query (inside group loop)
    """
    s = MagicMock()

    r_sum = MagicMock()
    r_sum.scalar.return_value = 0.0

    r_groups = MagicMock()
    r_groups.scalars.return_value.all.return_value = [group]

    r_markets = MagicMock()
    r_markets.scalars.return_value.all.return_value = [market]

    seq = [r_sum, r_groups, r_markets]
    idx = [0]

    def _execute(stmt, *a, **kw):
        i = idx[0]; idx[0] += 1
        if i < len(seq):
            return seq[i]
        r = MagicMock()
        r.scalar.return_value = None
        r.scalars.return_value.all.return_value = []
        r.all.return_value = []
        return r

    s.execute.side_effect = _execute
    return s


def _run_engine(consensus_temp, *, bin_lo=94.0, bin_hi=95.0,
                bin_label="94-95°F", city="Denver"):
    """
    Run run_tail_engine with one market and a controlled consensus.
    Returns list of kwargs dicts captured from place_limit_order calls.
    """
    from src.strategy.live_trader import run_tail_engine

    grp = _group(city)
    mkt = _market(bin_lo, bin_hi, bin_label, city)
    sess = _session(grp, mkt)

    scan = MagicMock()
    scan.orders = [_order(mkt.market_id, bin_label)]
    scan.skipped = []

    placed = []

    mock_rm = MagicMock()
    mock_rm.is_emergency_stopped.return_value = False
    mock_rm.can_place_tail_zone.return_value = (True, "")
    mock_rm.can_place_tail.return_value = (True, "")
    mock_rm.can_place_tail_token.return_value = (True, "")

    with (
        patch("src.telegram.strategy_flags.is_enabled", return_value=True),
        patch("src.strategy.live_trader._get_equity", return_value=50.0),
        patch("src.strategy.live_trader.get_session", return_value=sess),
        patch("src.strategy.live_trader._get_latest_snapshots",
              return_value={mkt.market_id: {"price_yes": 0.12}}),
        patch("src.strategy.live_trader._get_forecasts", return_value=[]),
        patch("src.strategy.live_trader._quick_consensus", return_value=consensus_temp),
        patch("src.market.clob_client.get_no_ask_prices", return_value={}),
        patch("src.strategy.tail_seller.scan_tails", return_value=scan),
        patch("src.strategy.live_trader._log_no_depth"),
        patch("src.market.order_executor.place_limit_order",
              side_effect=lambda **kw: placed.append(kw) or "oid-123"),
        patch("src.risk.risk_manager.risk_manager", mock_rm),
        patch("src.notifications.telegram.get_notifier", return_value=MagicMock()),
        patch("src.strategy.live_trader._verify_order_market", return_value=True),
    ):
        run_tail_engine()

    return placed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestKSigmaGateBlocksOrder:
    """SKIP verdict must prevent place_limit_order — catches 'SKIP but order sent' bug."""

    def test_denver_above_skip_no_order(self):
        """
        Denver 94-95°F, consensus=91.04: dist=2.96, dir=ABOVE, threshold=7.0°F.
        2.96 < 7.0 → SKIP → place_limit_order must NOT be called.
        """
        placed = _run_engine(91.04, bin_lo=94.0, bin_hi=95.0,
                             bin_label="94-95°F", city="Denver")
        assert placed == [], (
            f"SKIP should block order; place_limit_order called {len(placed)}x: {placed}"
        )

    def test_austin_above_skip_no_order(self):
        """
        Austin 90-91°F, consensus=85.64: dist=4.36, dir=ABOVE, threshold=7.0°F.
        4.36 < 7.0 → SKIP → no order.
        """
        placed = _run_engine(85.64, bin_lo=90.0, bin_hi=91.0,
                             bin_label="90-91°F", city="Austin")
        assert placed == [], (
            f"SKIP should block order; placed {len(placed)}x: {placed}"
        )

    def test_chicago_below_pass_allows_order(self):
        """
        Chicago 70-71°F, consensus=75.5: dist=4.5, dir=BELOW, threshold=3.5°F.
        4.5 >= 3.5 → PASS → place_limit_order called once.
        Note: consensus=75.5 is illustrative; confirm actual dist from server logs.
        """
        placed = _run_engine(75.5, bin_lo=70.0, bin_hi=71.0,
                             bin_label="70-71°F", city="Chicago")
        assert len(placed) == 1, (
            f"PASS should allow 1 order; got {len(placed)}: {placed}"
        )


class TestFailClosed:
    """consensus=None must SKIP (fail-closed), not silently bypass the gate."""

    def test_consensus_none_blocks_order(self):
        """
        consensus=None → SKIP (fail-closed) → 0 orders placed.
        Previously this was BYPASS (order would proceed without gate check).
        """
        placed = _run_engine(None, bin_lo=94.0, bin_hi=95.0,
                             bin_label="94-95°F", city="Denver")
        assert placed == [], (
            f"consensus=None must be fail-closed (0 orders); got {len(placed)}: {placed}"
        )

    def test_just_above_threshold_passes(self):
        """
        Bin at exactly threshold distance: dist == 7.0°F → PASS (>= is inclusive).
        consensus = 94 - 7.0 = 87.0 → dist = 7.0.
        """
        placed = _run_engine(87.0, bin_lo=94.0, bin_hi=95.0,
                             bin_label="94-95°F", city="Denver")
        assert len(placed) == 1, (
            f"dist==threshold should PASS (>=); got {len(placed)} orders"
        )

    def test_just_below_threshold_skips(self):
        """
        dist = 6.99 < 7.0 → SKIP → 0 orders.
        consensus = 94 - 6.99 = 87.01.
        """
        placed = _run_engine(87.01, bin_lo=94.0, bin_hi=95.0,
                             bin_label="94-95°F", city="Denver")
        assert placed == [], (
            f"dist<threshold should SKIP; got {len(placed)} orders"
        )
