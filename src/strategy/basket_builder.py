from dataclasses import dataclass

from src.db.models import Market
from src.strategy.config import strategy_settings as ss


@dataclass
class BasketItem:
    market: Market
    price_yes: float | None
    stake_usd: float
    shares: float | None  # units purchased = stake_usd / price_yes


def build_basket(
    markets: list[Market],
    target_temp: float,           # in market's native unit (F or C)
    prices: dict[str, float | None],  # market_id -> latest price_yes
    unit: str = "F",
) -> list[BasketItem]:
    """
    Select bins centered on target_temp, add neighbors and optional upside bins.
    Returns empty list if target_temp is outside all bin ranges.
    """
    sortable = [m for m in markets if m.bin_min is not None and m.is_active]
    sorted_markets = sorted(sortable, key=lambda m: m.bin_min)

    if not sorted_markets:
        return []

    # Find center bin
    center_idx: int | None = None
    for i, m in enumerate(sorted_markets):
        bin_max = m.bin_max if m.bin_max is not None else m.bin_min + 1
        if m.bin_min <= target_temp <= bin_max:
            center_idx = i
            break

    if center_idx is None:
        return []

    n = ss.strategy_basket_neighbors
    lo = max(0, center_idx - n)
    hi = min(len(sorted_markets), center_idx + n + 1)
    basket_markets = list(sorted_markets[lo:hi])

    if ss.strategy_basket_include_upside:
        if lo - 1 >= 0:
            basket_markets.append(sorted_markets[lo - 1])
        if hi < len(sorted_markets):
            basket_markets.append(sorted_markets[hi])

    if not basket_markets:
        return []

    per_stake = ss.strategy_virtual_stake_usd / len(basket_markets)
    items: list[BasketItem] = []
    for m in basket_markets:
        price = prices.get(m.market_id)
        shares = (per_stake / price) if price and price > 0 else None
        items.append(BasketItem(market=m, price_yes=price, stake_usd=per_stake, shares=shares))

    return items
