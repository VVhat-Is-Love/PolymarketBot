"""
edge_filter.py — model probability vs market price.

IMPORTANT CHANGE IN INTENT
--------------------------
The per-bin edge (model_prob - market_prob) is now DIAGNOSTIC ONLY.
It must NEVER be used to add or remove bins from a basket.

Reason: a symmetric Gaussian centred on a forecast consensus almost always
assigns the central favourite LESS probability than a market that is
concentrated on it. Filtering bins by per-bin edge therefore deletes the
single most likely outcome and keeps the longshot tails — it inverts the
strategy. Bin selection belongs to basket_builder; the trade gate belongs
to basket_decision (honest EV).

A per-bin Gaussian is also statistically fragile: small errors in sigma or
centre move probability mass between adjacent bins by tens of percent.
Integrated over a WHOLE basket span, those errors largely cancel — which is
why `basket_span_probability` (an aggregate) is trustworthy enough to use as
one of two cross-checks, while per-bin edge is not.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db.models import Market


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _norm_cdf(z: float) -> float:
    """Standard normal CDF using math.erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _bin_prob(bin_min: float, bin_max: float, mean: float, sigma: float) -> float:
    """P(bin_min <= X <= bin_max) under N(mean, sigma^2)."""
    if sigma <= 0:
        return 0.0
    lo = (bin_min - mean) / sigma
    hi = (bin_max - mean) / sigma
    return max(0.0, _norm_cdf(hi) - _norm_cdf(lo))


def _sigma_in_unit(sigma_c: float, unit: str) -> float:
    return sigma_c * (9.0 / 5.0) if unit.upper() == "F" else sigma_c


# ---------------------------------------------------------------------------
# Aggregate probability — TRUSTWORTHY, use as a cross-check
# ---------------------------------------------------------------------------

def basket_span_probability(
    span_lo: float,
    span_hi: float,
    consensus_temp: float,
    unit: str = "C",
    sigma_c: float = 1.5,
) -> float:
    """
    P(consensus model puts the outcome inside the contiguous basket span).

    This is an AGGREGATE over the whole basket — robust enough to gate trades
    (unlike per-bin probabilities). Returns a value in [0, 1].
    """
    sigma = _sigma_in_unit(sigma_c, unit)
    return _bin_prob(span_lo, span_hi, consensus_temp, sigma)


# ---------------------------------------------------------------------------
# Per-bin edge — DIAGNOSTIC ONLY
# ---------------------------------------------------------------------------

@dataclass
class BinEdge:
    market_id: str
    bin_label: str
    model_prob: float   # Gaussian P(temp in this bin), normalised across all bins
    market_prob: float  # CLOB price (market's implied probability)
    edge: float         # model_prob - market_prob  (DIAGNOSTIC — do not gate on this)


def compute_bin_edges(
    markets: list["Market"],
    consensus_temp: float,
    market_prices: dict[str, float | None],
    unit: str = "C",
    sigma_c: float = 1.5,
) -> dict[str, BinEdge]:
    """
    Per-bin model probability vs market price.

    FOR LOGGING / ANALYTICS ONLY. Do not use the returned `edge` to select,
    add, or remove basket bins — see the module docstring.
    """
    sigma = _sigma_in_unit(sigma_c, unit)

    raw_probs: dict[str, float] = {}
    valid_markets: list[Market] = []
    for m in markets:
        if m.bin_min is None or not m.is_active:
            continue
        bin_max = m.bin_max if m.bin_max is not None else m.bin_min + 1.0
        raw_probs[m.market_id] = _bin_prob(m.bin_min, bin_max, consensus_temp, sigma)
        valid_markets.append(m)

    total_prob = sum(raw_probs.values()) or 1.0

    result: dict[str, BinEdge] = {}
    for m in valid_markets:
        model_prob = raw_probs[m.market_id] / total_prob
        market_prob = float(market_prices.get(m.market_id) or 0.0)
        result[m.market_id] = BinEdge(
            market_id=m.market_id,
            bin_label=m.bin_label or str(m.bin_min),
            model_prob=model_prob,
            market_prob=market_prob,
            edge=model_prob - market_prob,
        )
    return result