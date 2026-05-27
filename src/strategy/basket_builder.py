from dataclasses import dataclass, field

from src.db.models import Market
from src.strategy.config import strategy_settings as ss


@dataclass
class BasketItem:
    """
    One bin in a basket.

    Field layout is kept identical to the original version so existing callers
    (checklist, paper_trader, build_basket_json) keep working without changes.
      price_yes  : the bin's current price / ask (cost to BUY one YES share)
      stake_usd  : filled by basket_decision sizing layer, NOT here (left at 0)
      shares     : same — left at None here
    """
    market: Market
    price_yes: float | None
    stake_usd: float = 0.0
    shares: float | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_float(val) -> float | None:
    """
    Safely coerce a price value to float.

    Guards against unexpected DB return types (Decimal, string, list, None).
    Any value that cannot be cleanly represented as a positive float returns None.
    """
    if val is None:
        return None
    if isinstance(val, list):
        # Shouldn't happen, but prevents 'float < list' crash in comparisons.
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_basket(
    markets: list[Market],
    target_temp: float | None,          # kept for signature compat — not used for selection
    prices: dict[str, float | None],    # market_id -> price_yes (ask for BUY)
    unit: str = "F",
) -> list[BasketItem]:
    """
    Build a contiguous basket of bins anchored on the MARKET FAVOURITE.

    SELECTION RULE: start from the bin with the highest ask price (the market
    favourite = most likely outcome), then expand to contiguous neighbours by
    picking the higher-priced adjacent bin first, up to ss.basket_max_bins.

    Why: the old code centred the basket on a Gaussian consensus and a downstream
    edge-filter then dropped the favourite (highest-priced bin) because the
    symmetric Gaussian always underprices it relative to the market. That
    systematically left the bot holding only longshot tails. Anchoring on the
    favourite guarantees the basket always contains the most probable outcome.

    `target_temp` is accepted but ignored; kept so all callers compile without
    changes.

    Returns [] when no active, priced bins exist.
    """
    active = [m for m in markets if m.bin_min is not None and m.is_active]
    if not active:
        return []

    sorted_markets = sorted(active, key=lambda m: float(m.bin_min))  # bin_min always numeric

    # Resolve each bin's price safely; skip bins with bad/missing prices.
    safe_prices: dict[str, float] = {}
    for m in sorted_markets:
        f = _to_float(prices.get(m.market_id))
        if f is not None:
            safe_prices[m.market_id] = f

    priced_idx = [
        i for i, m in enumerate(sorted_markets)
        if m.market_id in safe_prices
    ]
    if not priced_idx:
        return []

    # Favourite = highest ask among priced bins.
    # BUG FIX: max() already returns the element from priced_idx (an int).
    # Previously had [0] here which tried to subscript an int → crash.
    fav_idx: int = max(priced_idx, key=lambda i: safe_prices[sorted_markets[i].market_id])

    lo = hi = fav_idx
    max_bins = max(1, ss.basket_max_bins)

    def _ask(i: int) -> float | None:
        """Return the safe price for index i, or None if out of range / no price."""
        if i < 0 or i >= len(sorted_markets):
            return None
        return safe_prices.get(sorted_markets[i].market_id)

    # Expand contiguously: always pick the higher-priced adjacent side first.
    while (hi - lo + 1) < max_bins:
        left = _ask(lo - 1)
        right = _ask(hi + 1)
        if left is None and right is None:
            break
        # Both sides exist: pick higher price first.
        # Comparisons are safe because _ask() only returns float or None.
        if right is None or (left is not None and left >= right):
            lo -= 1
        else:
            hi += 1

    window = sorted_markets[lo : hi + 1]
    return [
        BasketItem(
            market=m,
            price_yes=safe_prices.get(m.market_id),  # None for unpriced bins in window
            stake_usd=0.0,
            shares=None,
        )
        for m in window
    ]