import json


# ---------------------------------------------------------------------------
# New functions — honest EV semantics
# ---------------------------------------------------------------------------

def basket_gross_return(sum_ask: float) -> float:
    """
    Gross return IF the basket contains the winner.

    With equal share counts q on every bin:
        cost          = q * sum_ask
        payout_if_hit = q * $1
        gross_return  = (1 - sum_ask) / sum_ask

    IMPORTANT: this is NOT expected value. It assumes P(winner in basket) = 1.0
    which is never true. The honest EV is basket_expected_value() below.
    This function is kept only because checklist.py uses it as a pre-filter.
    """
    if not isinstance(sum_ask, (int, float)):
        return 0.0
    if not (0.0 < sum_ask < 1.0):
        return 0.0
    return (1.0 - sum_ask) / sum_ask


def basket_expected_value(p_win_in_basket: float, sum_ask: float) -> float:
    """
    Honest EV per $1 of equal-share notional.

    EV = P(winner in basket) - sum(ask prices)

    This is the value that must be POSITIVE to enter a trade.
    p_win_in_basket should be estimated independently (Gaussian aggregate,
    normalised market prices) — never assumed to be 1.0.
    """
    return p_win_in_basket - sum_ask


# ---------------------------------------------------------------------------
# Backwards-compatible wrapper — DO NOT REMOVE
# ---------------------------------------------------------------------------

def calculate_edge(basket_prices: list[float]) -> float:
    """
    Deprecated wrapper kept for checklist.py compatibility.

    checklist.py calls  calculate_edge([price1, price2, ...])  and expects
    the function to sum the list internally. Replacing this with a direct
    alias to basket_gross_return (which expects a pre-summed float) broke
    that call site with 'float < list' — this wrapper restores the contract.

    Does NOT compute true EV. Use basket_expected_value() for that.
    """
    total = sum(p for p in basket_prices if p is not None)
    return basket_gross_return(total)


# ---------------------------------------------------------------------------
# Basket serialisation / resolution  (unchanged from original)
# ---------------------------------------------------------------------------

def build_basket_json(basket_items: list) -> str:
    """Serialise basket to JSON for storage in paper_trades."""
    rows = []
    for item in basket_items:
        rows.append({
            "market_id": item.market.market_id,
            "bin_label": item.market.bin_label,
            "bin_min": item.market.bin_min,
            "bin_max": item.market.bin_max,
            "price_yes": item.price_yes,
            "stake_usd": round(item.stake_usd, 4),
            "shares": round(item.shares, 4) if item.shares else None,
        })
    return json.dumps(rows)


def find_winning_market(basket_json: str, actual_temp: float) -> str | None:
    """Return market_id of the bin that contains actual_temp, or None."""
    basket = json.loads(basket_json)
    for item in basket:
        lo = item.get("bin_min")
        hi = item.get("bin_max")
        if lo is None:
            continue
        hi_eff = hi if hi is not None else lo + 1
        if lo <= actual_temp <= hi_eff:
            return item["market_id"]
    return None


def calculate_pnl(
    basket_json: str,
    winning_market_id: str | None,
    virtual_stake_usd: float,
) -> tuple[float, str]:
    """
    Returns (pnl_usd, status).

    With equal shares, every bin in the basket has the same share count q,
    so the winning bin always pays exactly q * $1 = payout_if_hit regardless
    of which specific bin wins. This is correct for the equal-share allocation
    produced by basket_decision.py.

    status:
      'resolved_win'     – pnl > 0
      'resolved_partial' – basket bin won but payout < total stake
      'resolved_loss'    – winning bin not in basket (or no winner)
    """
    if winning_market_id is None:
        return -virtual_stake_usd, "resolved_loss"

    basket = json.loads(basket_json)
    winner = next((b for b in basket if b["market_id"] == winning_market_id), None)

    if winner is None:
        return -virtual_stake_usd, "resolved_loss"

    shares = winner.get("shares")
    if not shares:
        return -virtual_stake_usd, "resolved_loss"

    payout = shares * 1.0   # each share pays $1 on win
    pnl = payout - virtual_stake_usd

    if pnl > 0:
        status = "resolved_win"
    elif payout > 0:
        status = "resolved_partial"
    else:
        status = "resolved_loss"

    return round(pnl, 4), status