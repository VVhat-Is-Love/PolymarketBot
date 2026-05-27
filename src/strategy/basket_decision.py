"""
basket_decision.py — coherent entry gate + sizing for the weather-basket strategy.

Replaces the per-bin edge filter + trim-to-extremes logic that used to live
inside live_trader. That old logic concentrated the whole stake on the single
longshot bin. This module never does that.

CORE PRINCIPLE
--------------
With EQUAL share counts `q` on every basket bin:

    cost          = q * sum(ask)
    payout_if_hit = q * $1            (only one bin in the basket can win)
    EV            = q * ( P(winner in basket) - sum(ask) )

So a YES basket is +EV only when  P(winner in basket) > sum(ask).
This module gates on exactly that, using the LOWER of two independent
probability estimates (Gaussian forecast model, normalised market prices)
for a conservative margin.

SIZING (small bankroll)
-----------------------
  budget        = min(wallet * fraction, max_basket_cost_usd)
  equal shares  q so that the CHEAPEST bin still gets >= min_order_notional
When the basket cannot be funded within budget, it is SHRUNK by dropping the
CHEAPEST (least likely) bin. The market favourite is never dropped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class BinOrder:
    market_id: str
    bin_label: str
    ask_price: float      # price to BUY one YES share
    shares: float         # equal across all bins in the basket
    stake_usd: float      # ask_price * shares  (>= min_order_notional_usd)


@dataclass
class BasketDecision:
    accepted: bool
    reason: str
    bins: list[BinOrder] = field(default_factory=list)
    sum_ask: float = 0.0
    total_cost_usd: float = 0.0
    payout_if_hit_usd: float = 0.0     # = shares * $1
    gross_return_pct: float = 0.0      # (payout - cost) / cost, IF the basket hits
    p_model: float = 0.0               # Gaussian P(winner in basket span)
    p_market: float = 0.0              # normalised market P(winner in basket)
    expected_value_usd: float = 0.0    # honest EV of the whole basket


# ---------------------------------------------------------------------------
# Probability estimates
# ---------------------------------------------------------------------------

def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _gaussian_span_prob(
    span_lo: float, span_hi: float, consensus_temp: float, sigma: float
) -> float:
    if sigma <= 0 or span_hi <= span_lo:
        return 0.0
    lo = (span_lo - consensus_temp) / sigma
    hi = (span_hi - consensus_temp) / sigma
    return max(0.0, min(1.0, _norm_cdf(hi) - _norm_cdf(lo)))


def _market_span_prob(basket_ids: set[str], all_prices: dict[str, float]) -> float:
    """
    Market-implied P(winner in basket), with the overround removed by
    normalising over every bin of the event. Polymarket YES prices across a
    full event typically sum to >1 (~1.05-1.10); dividing by that total gives
    a fair-ish probability instead of an inflated one.
    """
    total = sum(p for p in all_prices.values() if p and p > 0)
    if total <= 0:
        return 0.0
    inside = sum(p for mid, p in all_prices.items() if mid in basket_ids and p and p > 0)
    return min(1.0, inside / total)


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def _size_basket(
    asks: list[tuple[str, str, float]],   # (market_id, bin_label, ask_price)
    wallet_balance: float,
    ss,
) -> list[BinOrder] | None:
    """
    Equal-share sizing. Returns BinOrder list, or None if the basket cannot be
    funded within budget while keeping every order >= min_order_notional_usd.
    """
    if not asks:
        return None

    sum_ask = sum(a for _, _, a in asks)
    min_ask = min(a for _, _, a in asks)
    if sum_ask <= 0 or min_ask <= 0:
        return None

    budget = min(
        wallet_balance * ss.bankroll_fraction_per_basket,
        ss.max_basket_cost_usd,
    )
    if budget < ss.min_order_notional_usd:
        return None  # bankroll too small to place even one compliant order

    # q from budget; q_floor guarantees the cheapest bin reaches the min notional
    q_budget = budget / sum_ask
    q_floor = ss.min_order_notional_usd / min_ask
    q = max(q_budget, q_floor)

    total_cost = q * sum_ask
    # If meeting the min notional pushes us past the ceiling/wallet, this basket
    # is too wide for the bankroll — caller will shrink it.
    if total_cost > ss.max_basket_cost_usd + 1e-9 or total_cost > wallet_balance + 1e-9:
        return None

    q = round(q, 4)
    orders: list[BinOrder] = []
    for market_id, bin_label, ask in asks:
        stake = round(q * ask, 4)
        if stake < ss.min_order_notional_usd - 1e-6:
            return None  # safety: should not happen given q_floor
        orders.append(
            BinOrder(market_id=market_id, bin_label=bin_label,
                     ask_price=ask, shares=q, stake_usd=stake)
        )
    return orders


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def decide_basket(
    basket_items: list,                    # BasketItem list from build_basket (favourite-anchored)
    all_bin_prices: dict[str, float],      # market_id -> price for EVERY bin of the event
    consensus_temp: float | None,          # forecast consensus in the market's native unit
    unit: str,                             # "C" or "F"
    wallet_balance: float,
    ss,                                    # strategy_settings
) -> BasketDecision:
    """
    Decide whether to enter the basket and, if so, how to size it.

    Never drops the favourite. When the basket is too wide for the bankroll it
    is shrunk from the CHEAPEST (least likely) end. Gates on honest EV:
        min(P_model, P_market) - sum(ask) >= basket_required_edge
    """
    # Keep only priced bins, sorted by bin_min
    work = [
        b for b in sorted(basket_items, key=lambda x: (x.market.bin_min or 0))
        if b.price_yes is not None and b.price_yes > 0
    ]
    if len(work) < ss.basket_min_bins:
        return BasketDecision(False, f"too_few_priced_bins:{len(work)}")

    favourite = max(work, key=lambda b: b.price_yes)
    sigma = ss.edge_sigma_c * (9.0 / 5.0) if unit.upper() == "F" else ss.edge_sigma_c

    last_reason = "no_viable_subset"

    while len(work) >= ss.basket_min_bins:
        asks_tuples = [
            (b.market.market_id, b.market.bin_label or str(b.market.bin_min), b.price_yes)
            for b in work
        ]
        sum_ask = sum(a for _, _, a in asks_tuples)

        # A basket whose cost already >= 1.0 cannot be +EV at all.
        if sum_ask >= 1.0:
            last_reason = f"sum_ask>={sum_ask:.3f}"
            work = _drop_cheapest(work, favourite, ss.basket_min_bins)
            if work is None:
                break
            continue

        span_lo = min(b.market.bin_min for b in work)
        span_hi = max(
            (b.market.bin_max if b.market.bin_max is not None else b.market.bin_min + 1)
            for b in work
        )
        basket_ids = {b.market.market_id for b in work}

        p_market = _market_span_prob(basket_ids, all_bin_prices)
        p_model = (
            _gaussian_span_prob(span_lo, span_hi, consensus_temp, sigma)
            if consensus_temp is not None else p_market
        )
        p_used = min(p_model, p_market)            # conservative
        ev_per_unit = p_used - sum_ask             # EV per $1 of equal-share notional

        cost_ok = sum_ask <= ss.basket_max_cost
        edge_ok = ev_per_unit >= ss.basket_required_edge
        mkt_ok = p_market >= ss.basket_min_market_prob

        if cost_ok and edge_ok and mkt_ok:
            orders = _size_basket(asks_tuples, wallet_balance, ss)
            if orders is not None:
                total_cost = sum(o.stake_usd for o in orders)
                payout = orders[0].shares * 1.0   # equal shares -> any winning bin pays this
                gross = (payout - total_cost) / total_cost if total_cost > 0 else 0.0
                return BasketDecision(
                    accepted=True,
                    reason="ok",
                    bins=orders,
                    sum_ask=sum_ask,
                    total_cost_usd=round(total_cost, 4),
                    payout_if_hit_usd=round(payout, 4),
                    gross_return_pct=gross,
                    p_model=p_model,
                    p_market=p_market,
                    expected_value_usd=round(ev_per_unit * orders[0].shares, 4),
                )
            # Accepted on EV but cannot be funded -> shrink and retry
            last_reason = f"unfundable:sum_ask={sum_ask:.3f},bins={len(work)}"
        else:
            bits = []
            if not cost_ok:
                bits.append(f"sum_ask={sum_ask:.3f}>{ss.basket_max_cost}")
            if not edge_ok:
                bits.append(f"ev={ev_per_unit:+.3f}<{ss.basket_required_edge}")
            if not mkt_ok:
                bits.append(f"p_market={p_market:.3f}<{ss.basket_min_market_prob}")
            last_reason = ";".join(bits)

        work = _drop_cheapest(work, favourite, ss.basket_min_bins)
        if work is None:
            break

    # Optional single-favourite fallback (a value bet on the MOST likely bin —
    # never on a longshot). Off unless ss.allow_single_favorite is True.
    if ss.allow_single_favorite:
        single = _size_basket(
            [(favourite.market.market_id,
              favourite.market.bin_label or str(favourite.market.bin_min),
              favourite.price_yes)],
            wallet_balance, ss,
        )
        if single is not None:
            o = single[0]
            return BasketDecision(
                accepted=True,
                reason="single_favorite",
                bins=single,
                sum_ask=favourite.price_yes,
                total_cost_usd=o.stake_usd,
                payout_if_hit_usd=round(o.shares, 4),
                gross_return_pct=(o.shares - o.stake_usd) / o.stake_usd if o.stake_usd else 0.0,
                p_model=favourite.price_yes,
                p_market=favourite.price_yes,
                expected_value_usd=0.0,   # break-even by construction at the ask
            )

    return BasketDecision(False, f"rejected:{last_reason}")


def _drop_cheapest(work: list, favourite, min_bins: int) -> list | None:
    """Remove the lowest-ask bin (never the favourite). None if can't shrink further."""
    if len(work) <= min_bins:
        return None
    candidates = [b for b in work if b is not favourite]
    if not candidates:
        return None
    cheapest = min(candidates, key=lambda b: b.price_yes)
    return [b for b in work if b is not cheapest]


# ---------------------------------------------------------------------------
# Strategy B — basket_narrow (favourite ± 1 neighbour, looser EV gate)
# ---------------------------------------------------------------------------

def decide_basket_narrow(
    basket_items: list,                    # full BasketItem list from build_basket (sorted by bin_min)
    all_bin_prices: dict[str, float],      # market_id -> price for EVERY bin of the event
    consensus_temp: float | None,
    unit: str,
    wallet_balance: float,
    ss,
) -> BasketDecision:
    """
    Strategy B: evaluate the market favourite ± 1 neighbour as a narrow 3-bin basket.

    Never modifies decide_basket() logic. Uses looser EV thresholds
    (ss.basket_narrow_required_edge, ss.basket_narrow_min_market_prob) so that
    concentrated markets where a single bin dominates can still pass when the
    wide basket fails because it includes too many marginal bins.

    On flat markets (where even 3 bins sum to Σask > P_winner) this will also
    be rejected — which is correct; the bot must not trade negative-EV positions.
    """
    # Keep only priced bins, sorted by bin_min
    work = [
        b for b in sorted(basket_items, key=lambda x: (x.market.bin_min or 0))
        if b.price_yes is not None and b.price_yes > 0
    ]
    if not work:
        return BasketDecision(False, "basket_narrow:no_priced_bins")

    # Find the market favourite (highest ask = most probable outcome)
    favourite = max(work, key=lambda b: b.price_yes)
    fav_idx = work.index(favourite)

    # Narrow window: favourite ± 1 neighbour (2 or 3 bins depending on edge position)
    lo = max(0, fav_idx - 1)
    hi = min(len(work) - 1, fav_idx + 1)
    narrow = work[lo : hi + 1]

    if not narrow:
        return BasketDecision(False, "basket_narrow:empty_window")

    asks_tuples = [
        (b.market.market_id, b.market.bin_label or str(b.market.bin_min), b.price_yes)
        for b in narrow
    ]
    sum_ask = sum(a for _, _, a in asks_tuples)

    # A basket costing ≥ $1 to win $1 cannot be +EV regardless of probability
    if sum_ask >= 1.0:
        return BasketDecision(False, f"basket_narrow:sum_ask={sum_ask:.3f}>=1.0")

    sigma = ss.edge_sigma_c * (9.0 / 5.0) if unit.upper() == "F" else ss.edge_sigma_c

    span_lo = min(b.market.bin_min for b in narrow)
    span_hi = max(
        (b.market.bin_max if b.market.bin_max is not None else b.market.bin_min + 1)
        for b in narrow
    )
    basket_ids = {b.market.market_id for b in narrow}

    p_market = _market_span_prob(basket_ids, all_bin_prices)
    p_model = (
        _gaussian_span_prob(span_lo, span_hi, consensus_temp, sigma)
        if consensus_temp is not None else p_market
    )
    p_used = min(p_model, p_market)        # conservative: take the lower estimate
    ev_per_unit = p_used - sum_ask         # honest EV per $1 of equal-share notional

    edge_ok = ev_per_unit >= ss.basket_narrow_required_edge
    mkt_ok = p_market >= ss.basket_narrow_min_market_prob

    if not edge_ok or not mkt_ok:
        bits: list[str] = []
        if not edge_ok:
            bits.append(f"ev={ev_per_unit:+.3f}<{ss.basket_narrow_required_edge}")
        if not mkt_ok:
            bits.append(f"p_market={p_market:.3f}<{ss.basket_narrow_min_market_prob}")
        return BasketDecision(False, f"basket_narrow:{';'.join(bits)}")

    orders = _size_basket(asks_tuples, wallet_balance, ss)
    if orders is None:
        return BasketDecision(
            False,
            f"basket_narrow:unfundable:bins={len(narrow)},sum_ask={sum_ask:.3f}",
        )

    total_cost = sum(o.stake_usd for o in orders)
    payout = orders[0].shares * 1.0   # equal shares — any winning bin pays this
    gross = (payout - total_cost) / total_cost if total_cost > 0 else 0.0
    return BasketDecision(
        accepted=True,
        reason="basket_narrow:ok",
        bins=orders,
        sum_ask=sum_ask,
        total_cost_usd=round(total_cost, 4),
        payout_if_hit_usd=round(payout, 4),
        gross_return_pct=gross,
        p_model=p_model,
        p_market=p_market,
        expected_value_usd=round(ev_per_unit * orders[0].shares, 4),
    )