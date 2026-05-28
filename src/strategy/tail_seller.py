"""
tail_seller.py — NO time-decay tail selling, honest-EV edition.

IDEA
----
Longshot YES bins ("the high will be 29C-or-higher") are systematically
OVERpriced — the favourite-longshot bias. Selling such a bin = BUYing its NO
token. With one NO share:

    cost          = no_ask
    payout_if_win = $1            (NO wins  <=>  the bin does NOT happen)
    EV per share  = P(bin does NOT win) - no_ask

So buying NO is +EV only when the TRUE probability the bin does not happen
exceeds the NO ask. "Time decay" = enter close to resolution, when the
forecast is reliable and the longshot is almost certainly dead, while the
price still carries a premium that has not fully decayed to zero.

RISK — READ BEFORE TUNING
-------------------------
NO selling has an ASYMMETRIC payoff: you risk ~0.95 to make ~0.05. A single
tail that hits erases ~19 winning trades. With a small bankroll the failure
mode is RUIN, not a small loss. Therefore this module:
  * sizes with FRACTIONAL Kelly (never full Kelly),
  * hard-caps every position (tail_max_position_pct / tail_max_position_usd),
  * caps total capital locked across all NO positions (tail_max_portfolio_pct),
  * refuses sub-min-notional orders.
A $36 NO on a $50 account is 72% of the account on one bet. This module will
not produce a position remotely that large — that is the point.

The per-bin Gaussian is fragile between ADJACENT middle bins but acceptable
for FAR tails (it just needs to say "this is very unlikely"). Keep tail_sigma_c
conservative (rather too large than too small): underestimating sigma makes the
tail look deader than it is and causes overbetting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db.models import Market


_BIG = 1.0e6  # stand-in for +/- infinity on open-ended tail bins


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class TailOrder:
    market_id: str
    bin_label: str
    no_ask: float            # price to BUY one NO share
    shares: float
    stake_usd: float
    p_model_no: float        # model P(bin does NOT win)
    edge: float              # p_model_no - no_ask
    gross_return_pct: float  # (payout - cost) / cost, IF NO wins
    ev_usd: float            # honest EV of this position


@dataclass
class TailScanResult:
    orders: list[TailOrder] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # human-readable reasons, for logs
    total_stake_usd: float = 0.0


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _gauss_mass(lo: float, hi: float, mean: float, sigma: float) -> float:
    if sigma <= 0 or hi <= lo:
        return 0.0
    return max(0.0, _norm_cdf((hi - mean) / sigma) - _norm_cdf((lo - mean) / sigma))


def _bin_yes_probs(
    markets: list["Market"], consensus_temp: float, sigma: float
) -> dict[str, float]:
    """
    Normalised model P(YES) for every active bin. The lowest/highest bins are
    treated as open-ended tails (extended to -/+ infinity) so an outcome that
    overshoots the listed range is still counted.
    """
    active = sorted(
        (m for m in markets if m.bin_min is not None and m.is_active),
        key=lambda m: m.bin_min,
    )
    if not active:
        return {}

    bottom_id = active[0].market_id
    top_id = active[-1].market_id

    raw: dict[str, float] = {}
    for m in active:
        lo = -_BIG if m.market_id == bottom_id else m.bin_min
        if m.market_id == top_id:
            hi = _BIG
        else:
            hi = m.bin_max if m.bin_max is not None else m.bin_min + 1.0
        raw[m.market_id] = _gauss_mass(lo, hi, consensus_temp, sigma)

    total = sum(raw.values()) or 1.0
    return {mid: v / total for mid, v in raw.items()}


def kelly_no_stake(
    p_no: float, no_ask: float, bankroll: float, ss
) -> float:
    """
    Fractional-Kelly stake for a NO bet, hard-capped.

    Full Kelly:  f* = p_no - (1 - p_no) / b ,   b = (1 - no_ask) / no_ask
    Returned stake = bankroll * f* * tail_kelly_fraction, then capped by
    min(tail_max_position_usd, bankroll * tail_max_position_pct).
    """
    if not (0.0 < no_ask < 1.0) or p_no <= no_ask:
        return 0.0
    b = (1.0 - no_ask) / no_ask
    q = 1.0 - p_no
    f_star = p_no - q / b
    if f_star <= 0.0:
        return 0.0
    stake = bankroll * f_star * ss.tail_kelly_fraction
    cap = min(ss.tail_max_position_usd, bankroll * ss.tail_max_position_pct)
    return max(0.0, min(stake, cap))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_tails(
    markets: list["Market"],
    prices: dict[str, float | None],          # market_id -> price_yes (probability proxy)
    consensus_temp: float | None,
    unit: str,                                # "C" or "F"
    hours_to_close: float,
    available_capital: float,                 # wallet minus capital already deployed
    no_capital_in_use: float,                 # USD already locked in open NO positions
    ss,                                       # strategy_settings
    no_asks: dict[str, float] | None = None,  # real NO ask per market_id, if available
) -> TailScanResult:
    """
    Find +EV NO tail positions for one market group.

    no_asks: pass the real NO-token best ask when you have it. If omitted, the
    NO ask is approximated as (1 - price_yes), which is slightly OPTIMISTIC
    (it ignores the bid/ask spread). For production, read the NO token's order
    book and pass real asks here.
    """
    result = TailScanResult()

    if consensus_temp is None:
        result.skipped.append("no_consensus_temp")
        return result
    if hours_to_close > ss.tail_max_hours_to_close:
        result.skipped.append(f"too_early:{hours_to_close:.1f}h>{ss.tail_max_hours_to_close}h")
        return result
    if hours_to_close < ss.tail_min_hours_to_close:
        result.skipped.append(f"too_late:{hours_to_close:.1f}h<{ss.tail_min_hours_to_close}h")
        return result

    sigma = ss.tail_sigma_c * (9.0 / 5.0) if unit.upper() == "F" else ss.tail_sigma_c
    yes_probs = _bin_yes_probs(markets, consensus_temp, sigma)

    portfolio_cap = available_capital * ss.tail_max_portfolio_pct
    budget_left = max(0.0, portfolio_cap - no_capital_in_use)
    if budget_left < ss.min_order_notional_usd:
        result.skipped.append(
            f"portfolio_cap_reached:in_use=${no_capital_in_use:.2f}/{portfolio_cap:.2f}"
        )
        return result

    candidates: list[TailOrder] = []
    for m in markets:
        if m.bin_min is None or not m.is_active:
            continue
        yes_price = prices.get(m.market_id)
        if yes_price is None or yes_price <= 0:
            continue
        label = m.bin_label or str(m.bin_min)

        # Only longshot tails — and skip dust where there is no premium to collect
        if yes_price > ss.tail_max_yes_price:
            continue
        if yes_price < ss.tail_min_yes_price:
            result.skipped.append(f"{label}:dust_yes={yes_price:.3f}")
            continue

        if no_asks is not None:
            no_ask = no_asks.get(m.market_id)
            if no_ask is None:
                result.skipped.append(f"{label}:no_order_book")
                continue
        else:
            # Approximation: add slippage buffer to account for spread
            raw_approx = 1.0 - yes_price
            no_ask = min(0.99, raw_approx * (1.0 + ss.tail_slippage_buffer))
            logger.debug(f"[tail] {label}: approx no_ask={no_ask:.4f} (raw={raw_approx:.4f})")

        if not (0.0 < no_ask < 1.0):
            continue

        # Price band guard: only trade within the viable NO ask range
        if hasattr(ss, 'tail_min_no_ask') and (
            no_ask < ss.tail_min_no_ask or no_ask > ss.tail_max_no_ask
        ):
            result.skipped.append(
                f"{label}:out_of_band_no_ask={no_ask:.3f} "
                f"[{ss.tail_min_no_ask:.2f},{ss.tail_max_no_ask:.2f}]"
            )
            continue

        p_model_no = 1.0 - yes_probs.get(m.market_id, 0.0)
        edge = p_model_no - no_ask
        if edge < ss.tail_required_edge:
            result.skipped.append(
                f"{label}:edge={edge:+.3f}<{ss.tail_required_edge} "
                f"(p_no={p_model_no:.3f} no_ask={no_ask:.3f})"
            )
            continue

        stake = kelly_no_stake(p_model_no, no_ask, available_capital, ss)
        if stake < ss.min_order_notional_usd:
            result.skipped.append(f"{label}:kelly_stake=${stake:.2f}<min_notional")
            continue

        shares = round(stake / no_ask, 4)
        actual_stake = round(shares * no_ask, 4)
        payout = shares * 1.0
        candidates.append(
            TailOrder(
                market_id=m.market_id,
                bin_label=label,
                no_ask=no_ask,
                shares=shares,
                stake_usd=actual_stake,
                p_model_no=p_model_no,
                edge=edge,
                gross_return_pct=(payout - actual_stake) / actual_stake if actual_stake else 0.0,
                ev_usd=round(edge * shares, 4),
            )
        )

    # Best edge first, then fill until the portfolio budget is exhausted
    candidates.sort(key=lambda o: o.edge, reverse=True)
    for o in candidates:
        if o.stake_usd > budget_left + 1e-9:
            result.skipped.append(f"{o.bin_label}:no_portfolio_budget_left")
            continue
        result.orders.append(o)
        budget_left -= o.stake_usd
        result.total_stake_usd += o.stake_usd

    return result