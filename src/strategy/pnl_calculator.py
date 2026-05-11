import json


def calculate_edge(basket_prices: list[float]) -> float:
    """
    Simple edge model:
      edge = (1 - sum_prices) / sum_prices
    Positive edge means the basket is underpriced relative to implied probability of 1.
    """
    total = sum(p for p in basket_prices if p is not None)
    if total <= 0 or total >= 1.0:
        return 0.0
    return (1.0 - total) / total


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

    payout = shares * 1.0  # each share pays $1 on win
    pnl = payout - virtual_stake_usd

    if pnl > 0:
        status = "resolved_win"
    elif payout > 0:
        status = "resolved_partial"
    else:
        status = "resolved_loss"

    return round(pnl, 4), status
