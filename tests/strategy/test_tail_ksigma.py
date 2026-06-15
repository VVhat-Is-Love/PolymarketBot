"""Tests for the k·σ distance gate in tail NO entry (_tail_ksigma_ok).

Gate rule: near bin boundary must be ≥ k·σ from model consensus.
Default params: k=2.0, σ=1.7°F → threshold=3.4°F.
"""
import pytest
from types import SimpleNamespace

from src.strategy.live_trader import _tail_ksigma_ok


def _ss(k: float = 2.0, k_above: float = 0.0, sigma_f: float = 1.7) -> SimpleNamespace:
    return SimpleNamespace(
        tail_k_sigma=k,
        tail_k_sigma_above=k_above,
        tail_distance_sigma_f=sigma_f,
    )


# ---------------------------------------------------------------------------
# Passes (ok=True) — bin is far enough from consensus
# ---------------------------------------------------------------------------

class TestKSigmaGatePasses:
    def test_bin_below_consensus_far(self):
        # Dallas-style: bin 84-85, consensus 93 → near=85, dist=8 > 3.4 → PASS
        ok, near, dist, thr, k = _tail_ksigma_ok(84, 85, 93.0, "F", _ss())
        assert ok
        assert near == pytest.approx(85.0)
        assert dist == pytest.approx(8.0)

    def test_bin_above_consensus_far(self):
        # bin 98-99, consensus 93 → near=98, dist=5 > 3.4 → PASS
        ok, *_ = _tail_ksigma_ok(98, 99, 93.0, "F", _ss())
        assert ok

    def test_exactly_at_threshold_passes(self):
        # near=89.6, consensus=93.0, dist=3.4 = threshold → PASS (>=)
        ok, near, dist, thr, _ = _tail_ksigma_ok(88.6, 89.6, 93.0, "F", _ss())
        assert ok
        assert dist == pytest.approx(3.4)
        assert thr == pytest.approx(3.4)

    def test_distant_above_bin(self):
        # bin 100-101, consensus 93 → dist=7 > 3.4 → PASS
        ok, *_ = _tail_ksigma_ok(100, 101, 93.0, "F", _ss())
        assert ok


# ---------------------------------------------------------------------------
# Rejects (ok=False) — bin too close to consensus
# ---------------------------------------------------------------------------

class TestKSigmaGateRejects:
    def test_austin_94_95_rejects(self):
        # Canonical failure: bin 94-95°F, consensus 93°F → near=94, dist=1 < 3.4
        ok, near, dist, thr, k = _tail_ksigma_ok(94, 95, 93.0, "F", _ss())
        assert not ok
        assert near == pytest.approx(94.0)
        assert dist == pytest.approx(1.0)
        assert thr == pytest.approx(3.4)
        assert k == pytest.approx(2.0)

    def test_bin_below_consensus_too_close(self):
        # bin 90-91, consensus 93 → near=91, dist=2 < 3.4 → FAIL
        ok, *_ = _tail_ksigma_ok(90, 91, 93.0, "F", _ss())
        assert not ok

    def test_just_under_threshold(self):
        # dist=3.3 < 3.4 → FAIL
        ok, _, dist, thr, _ = _tail_ksigma_ok(88.7, 89.7, 93.0, "F", _ss())
        assert not ok
        assert dist < thr


# ---------------------------------------------------------------------------
# Asymmetric k (tail_k_sigma_above > 0 for above-consensus tails)
# ---------------------------------------------------------------------------

class TestKSigmaAsymmetricK:
    def test_above_uses_k_above(self):
        ss = _ss(k=2.0, k_above=3.0, sigma_f=1.7)
        # bin 97-98, consensus 93 → above → k_eff=3.0, threshold=5.1
        # dist=4 < 5.1 → FAIL (would PASS under symmetric k=2.0)
        ok, _, _, thr, k = _tail_ksigma_ok(97, 98, 93.0, "F", ss)
        assert not ok
        assert k == pytest.approx(3.0)
        assert thr == pytest.approx(5.1)

    def test_above_passes_far_bin_with_k_above(self):
        ss = _ss(k=2.0, k_above=3.0, sigma_f=1.7)
        # bin 100-101, consensus 93 → dist=7 > 5.1 → PASS
        ok, *_ = _tail_ksigma_ok(100, 101, 93.0, "F", ss)
        assert ok

    def test_k_above_zero_falls_back_to_k_sigma(self):
        ss = _ss(k=2.0, k_above=0.0, sigma_f=1.7)
        # above-consensus bin with k_above=0 → uses k_sigma=2.0
        _, _, _, _, k = _tail_ksigma_ok(97, 98, 93.0, "F", ss)
        assert k == pytest.approx(2.0)

    def test_below_always_uses_k_sigma(self):
        ss = _ss(k=2.0, k_above=5.0, sigma_f=1.7)
        # below consensus → k_eff=2.0 regardless of k_above
        _, _, _, thr, k = _tail_ksigma_ok(84, 85, 93.0, "F", ss)
        assert k == pytest.approx(2.0)
        assert thr == pytest.approx(3.4)


# ---------------------------------------------------------------------------
# Celsius unit conversion
# ---------------------------------------------------------------------------

class TestKSigmaUnits:
    def test_celsius_threshold_scaled(self):
        # sigma_gate = 1.7 / 1.8 ≈ 0.944°C; k=2 → threshold ≈ 1.889°C
        _, _, _, thr, _ = _tail_ksigma_ok(36, 37, 34.0, "C", _ss())
        assert thr == pytest.approx(1.7 / 1.8 * 2.0, abs=1e-9)

    def test_celsius_rejects_adjacent_bin(self):
        # bin 35-36, consensus 34.5, dist=0.5 < 1.889 → FAIL
        ok, *_ = _tail_ksigma_ok(35, 36, 34.5, "C", _ss())
        assert not ok

    def test_celsius_passes_far_bin(self):
        # bin 30-31, consensus 34.5, near=31, dist=3.5 > 1.889 → PASS
        ok, *_ = _tail_ksigma_ok(30, 31, 34.5, "C", _ss())
        assert ok

    def test_lowercase_unit_accepted(self):
        ok_upper, *_ = _tail_ksigma_ok(84, 85, 93.0, "F", _ss())
        ok_lower, *_ = _tail_ksigma_ok(84, 85, 93.0, "f", _ss())
        assert ok_upper == ok_lower


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestKSigmaEdgeCases:
    def test_open_ended_top_bin_max_none(self):
        # bin_max=None treated as bin_lo+1; bin 94-…, above consensus 93
        # near=94, dist=1 < 3.4 → FAIL
        ok, near, *_ = _tail_ksigma_ok(94, None, 93.0, "F", _ss())
        assert not ok
        assert near == pytest.approx(94.0)

    def test_open_ended_bottom_bin(self):
        # bin 78-80, consensus 93 → near=80, dist=13 > 3.4 → PASS
        ok, near, *_ = _tail_ksigma_ok(78, 80, 93.0, "F", _ss())
        assert ok
        assert near == pytest.approx(80.0)

    def test_returns_five_tuple(self):
        result = _tail_ksigma_ok(84, 85, 93.0, "F", _ss())
        assert len(result) == 5
        ok, near, dist, thr, k = result
        assert isinstance(ok, bool)
        assert all(isinstance(v, float) for v in (near, dist, thr, k))

    def test_correct_geometry_below(self):
        ok, near, dist, thr, k = _tail_ksigma_ok(90, 91, 95.0, "F", _ss())
        # below consensus → near=91, dist=4, threshold=3.4
        assert near == pytest.approx(91.0)
        assert dist == pytest.approx(4.0)
        assert thr == pytest.approx(3.4)
        assert ok

    def test_correct_geometry_above(self):
        ok, near, dist, thr, k = _tail_ksigma_ok(97, 98, 93.0, "F", _ss())
        # above consensus → near=97, dist=4, threshold=3.4
        assert near == pytest.approx(97.0)
        assert dist == pytest.approx(4.0)
        assert ok
