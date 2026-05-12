"""
Live trading engine — mirrors paper_trader logic but places real CLOB orders.
Runs in parallel with paper_trader (paper trades continue for comparison).

Kelly criterion determines total stake; 30/25/25/10/10 splits it across bins.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import MarketGroup, Market, MarketSnapshot, WeatherSnapshot, LiveTrade
from src.db.session import get_session
from src.strategy.checklist import evaluate_checklist
from src.strategy.config import strategy_settings as ss

if TYPE_CHECKING:
    from src.market.gamma_client import GammaClient

# Basket role → weight
_BASKET_WEIGHTS: dict[str, float] = {
    "center": 0.30,
    "minus1": 0.25,
    "plus1":  0.25,
    "minus2": 0.10,
    "plus2":  0.10,
}

# Role ordering for assigning to basket items sorted by bin_min
_ROLE_ORDER = ["minus2", "minus1", "center", "plus1", "plus2"]


def _assign_roles(basket_items: list) -> list[tuple[str, object]]:
    """Pair each basket item (sorted by bin_min) with a basket role."""
    sorted_items = sorted(basket_items, key=lambda b: b.market.bin_min or 0)
    n = len(sorted_items)
    # Use the middle n roles from _ROLE_ORDER
    offset = (len(_ROLE_ORDER) - n) // 2
    roles = _ROLE_ORDER[offset: offset + n]
    return list(zip(roles, sorted_items))


def _kelly_total_stake(
    edge_fraction: float,    # e.g. 0.163 for 16.3%
    sum_prices: float,       # e.g. 0.86
    wallet_balance: float,   # USDC balance
    kelly_fraction: float,   # fractional Kelly, e.g. 0.25
    max_bet_usd: float,
) -> float:
    """
    Kelly stake = wallet_balance × kelly_fraction × kelly_full.
    kelly_full = edge / ((1 / (1 - sum_prices)) - 1)
    Capped at max_bet_usd.
    """
    denominator = (1.0 / (1.0 - sum_prices)) - 1.0
    if denominator <= 0 or sum_prices >= 1.0:
        return 0.0
    kelly_full = edge_fraction / denominator
    stake = wallet_balance * kelly_fraction * kelly_full
    return min(max(stake, 0.0), max_bet_usd)


_balance_cache: Optional[tuple[float, datetime]] = None
_BALANCE_TTL = timedelta(hours=1)


def _fetch_balance_blockchain() -> Optional[float]:
    """Read USDC balance directly from Polygon blockchain (native USDC contract)."""
    try:
        from src.config.settings import settings
        from src.blockchain.balance import get_usdc_balance
        wallet = settings.proxy_wallet_address
        if not wallet:
            return None
        bal = get_usdc_balance(wallet)
        return bal if bal > 0 else None
    except Exception as exc:
        logger.warning(f"[live_trader] Blockchain balance failed: {exc}")
    return None


def _fetch_balance_from_api() -> Optional[float]:
    """Query USDC collateral balance via CLOB API. Requires Level 2 credentials."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        from src.blockchain.polymarket_auth import get_clob_client
        client = get_clob_client()
        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if isinstance(resp, dict):
            raw = resp.get("balance") or resp.get("allowance")
            if raw is not None:
                # CLOB API returns raw USDC units (6 decimals); convert to USD
                bal = float(raw) / 1e6
                if bal > 0:
                    return bal
    except Exception as exc:
        logger.warning(f"[live_trader] CLOB balance API failed: {type(exc).__name__}: {exc}")
    return None


def _get_wallet_balance() -> float:
    """
    Return USDC balance, using a 1-hour cache to avoid hammering the API.
    Priority: cached → blockchain (web3) → CLOB API (Level 2) → LIVE_WALLET_BALANCE_USD (.env).
    """
    global _balance_cache
    from src.config.settings import settings

    now = datetime.utcnow()

    if _balance_cache is not None:
        cached_val, cached_at = _balance_cache
        if now - cached_at < _BALANCE_TTL:
            logger.debug(f"[live_trader] Wallet balance (cached): ${cached_val:.2f}")
            return cached_val

    # 1. Blockchain — reads native USDC directly, no API credentials needed
    bal = _fetch_balance_blockchain()
    if bal is not None:
        logger.info(f"[live_trader] Wallet balance from blockchain: ${bal:.2f}")
        _balance_cache = (bal, now)
        return bal

    # 2. CLOB API — Level 2 credentials required
    bal = _fetch_balance_from_api()
    if bal is not None:
        logger.info(f"[live_trader] Wallet balance from CLOB API: ${bal:.2f}")
        _balance_cache = (bal, now)
        return bal

    # 3. Manual fallback
    fallback = settings.live_wallet_balance_usd
    if fallback > 0:
        logger.info(f"[live_trader] Wallet balance from .env: ${fallback:.2f}")
        _balance_cache = (fallback, now)
        return fallback

    logger.warning(
        "[live_trader] Wallet balance unknown. "
        "Set POLYGON_RPC_URL in .env for automatic blockchain reading."
    )
    return 0.0


def _get_latest_snapshots(
    session: Session, market_ids: list[str]
) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for mid in market_ids:
        row = (
            session.execute(
                select(MarketSnapshot)
                .where(MarketSnapshot.market_id == mid)
                .order_by(MarketSnapshot.snapshot_time.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        result[mid] = {
            "price_yes": row.price_yes if row else None,
            "best_bid": row.best_bid if row else None,
        }
    return result


def _get_forecasts(session: Session, group: MarketGroup) -> list[WeatherSnapshot]:
    rows = (
        session.execute(
            select(WeatherSnapshot).where(
                WeatherSnapshot.city == group.city,
                WeatherSnapshot.forecast_date == group.resolution_date,
                WeatherSnapshot.source.startswith("open_meteo:"),  # type: ignore[union-attr]
            )
            .order_by(WeatherSnapshot.snapshot_time.desc())
        )
        .scalars()
        .all()
    )
    seen: set[str] = set()
    deduped = []
    for r in rows:
        if r.source not in seen:
            seen.add(r.source)
            deduped.append(r)
    return deduped


def _poll_until_filled_or_timeout(
    order_id: str,
    timeout_minutes: int,
    poll_interval_sec: int = 30,
) -> str:
    """Poll order status until filled/cancelled or timeout. Returns final status."""
    from src.market.order_executor import get_order_status
    deadline = datetime.utcnow() + timedelta(minutes=timeout_minutes)
    while datetime.utcnow() < deadline:
        status = get_order_status(order_id)
        if status in ("filled", "cancelled"):
            return status
        time.sleep(poll_interval_sec)
    return "expired"


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

def run_live_trade_engine(gamma: "GammaClient | None" = None) -> None:
    """
    Evaluate all active market groups through the checklist.
    For groups that pass: compute Kelly stake, distribute across basket,
    place CLOB limit orders, poll for fills, record in live_trades.
    """
    from src.config.settings import settings
    from src.risk.risk_manager import risk_manager
    from src.market.order_executor import place_limit_order, cancel_order
    from src.notifications.telegram import get_notifier

    session = get_session()
    notifier = get_notifier()

    try:
        now = datetime.utcnow()
        horizon = now + timedelta(hours=ss.strategy_time_horizon_hours)
        min_close = now + timedelta(hours=ss.strategy_min_hours_to_close)

        groups: list[MarketGroup] = list(
            session.execute(
                select(MarketGroup).where(
                    MarketGroup.is_active == True,  # noqa: E712
                    MarketGroup.resolution_date <= horizon.date(),
                    MarketGroup.resolution_date >= min_close.date(),
                )
            )
            .scalars()
            .all()
        )

        if not groups:
            logger.debug("[live_trader] No groups in time horizon")
            return

        wallet_balance = _get_wallet_balance()
        if wallet_balance < 0.10:
            logger.warning(f"[live_trader] Wallet balance ${wallet_balance:.2f} too low — skipping")
            return

        for group in groups:
            # Skip if already have an open live trade for this group
            existing = session.execute(
                select(LiveTrade).where(
                    LiveTrade.group_id == group.group_id,
                    LiveTrade.status.in_(["pending", "open"]),
                )
            ).scalars().first()
            if existing:
                logger.debug(f"[live_trader] Already has open trade for {group.group_id}")
                continue

            markets: list[Market] = list(
                session.execute(
                    select(Market).where(
                        Market.group_id == group.group_id,
                        Market.is_active == True,  # noqa: E712
                    )
                )
                .scalars()
                .all()
            )
            if not markets:
                continue

            market_ids = [m.market_id for m in markets]
            snapshots = _get_latest_snapshots(session, market_ids)
            prices = {mid: snapshots[mid]["price_yes"] for mid in market_ids}

            if all(p is None for p in prices.values()):
                continue

            forecasts = _get_forecasts(session, group)
            result = evaluate_checklist(group, markets, prices, forecasts)

            if not result.passed:
                logger.debug(
                    f"[live_trader] {group.city} {group.resolution_date} "
                    f"rejected: {result.rejection_reason}"
                )
                continue

            # ---- Risk check ----
            can_trade, reason = risk_manager.can_place_order(settings.max_single_bet_usd)
            if not can_trade:
                logger.warning(f"[live_trader] Risk blocked: {reason}")
                notifier.send(f"🚨 Risk block: {reason}")
                continue

            # ---- Compute stake via Kelly ----
            basket_items = result.basket  # list[BasketItem]
            if not basket_items:
                continue

            basket_prices_list = [b.price_yes for b in basket_items if b.price_yes]
            sum_prices = sum(basket_prices_list)
            edge = result.estimated_edge or 0.0

            total_stake = _kelly_total_stake(
                edge_fraction=edge,
                sum_prices=sum_prices,
                wallet_balance=wallet_balance,
                kelly_fraction=settings.kelly_fraction,
                max_bet_usd=settings.max_single_bet_usd,
            )
            total_stake = risk_manager.cap_bet_size(total_stake)

            if total_stake < 0.05:
                logger.info(
                    f"[live_trader] {group.city}: Kelly stake ${total_stake:.4f} too small — skip"
                )
                continue

            # ---- Assign roles + distribute stake ----
            role_stakes: list[tuple[str, Market, float, float]] = []  # role, market, price, stake

            sorted_basket = sorted(basket_items, key=lambda b: (b.market.bin_min or 0))
            n = len(sorted_basket)
            offset = (len(_ROLE_ORDER) - n) // 2
            roles_to_use = _ROLE_ORDER[offset: offset + n]

            for role, bin_item in zip(roles_to_use, sorted_basket):
                weight = _BASKET_WEIGHTS.get(role, 0.0)
                bin_stake = total_stake * weight
                price_yes = bin_item.price_yes
                mkt = bin_item.market
                if price_yes and bin_stake > 0.01:
                    role_stakes.append((role, mkt, price_yes, bin_stake))

            logger.info(
                f"[live_trader] {group.city} {group.resolution_date}: "
                f"total_stake=${total_stake:.2f} bins={len(role_stakes)}"
            )

            # ---- Record in risk manager and place orders ----
            risk_manager.record_bet_placed(group.group_id, total_stake, strategy="live")
            notifier.send(
                f"📤 Новая корзина: {group.city} {group.resolution_date}\n"
                f"Stake: ${total_stake:.2f} | Bins: {len(role_stakes)}"
            )

            for role, mkt, price, bin_stake in role_stakes:
                size_shares = round(bin_stake / price, 6) if price > 0 else 0.0
                token_id = mkt.token_id_yes or ""

                trade = LiveTrade(
                    id=str(uuid.uuid4()),
                    group_id=group.group_id,
                    market_id=mkt.market_id,
                    bin_label=mkt.bin_label or "",
                    target_price=price,
                    size_shares=size_shares,
                    stake_usd=bin_stake,
                    basket_role=role,
                    city=group.city,
                    status="pending",
                )
                session.add(trade)
                session.commit()

                notifier.send(
                    f"📤 Ордер: {group.city} {mkt.bin_label} | "
                    f"${bin_stake:.2f} | лимит ${price:.4f}"
                )

                if not token_id:
                    logger.warning(f"[live_trader] No token_id_yes for {mkt.market_id} — skipping order")
                    trade.status = "cancelled"
                    session.commit()
                    continue

                order_id = place_limit_order(
                    market_id=mkt.market_id,
                    token_id=token_id,
                    side="BUY",
                    price=price,
                    size=size_shares,
                )

                if not order_id:
                    trade.status = "cancelled"
                    trade.cancelled_at = datetime.utcnow()
                    session.commit()
                    logger.warning(f"[live_trader] Order placement failed for {mkt.market_id}")
                    continue

                trade.order_id = order_id
                trade.status = "open"
                session.commit()

                # Poll for fill in background-friendly way
                final_status = _poll_until_filled_or_timeout(
                    order_id=order_id,
                    timeout_minutes=settings.order_timeout_minutes,
                )

                if final_status == "filled":
                    trade.status = "filled"
                    trade.filled_price = price  # approximate; real fill price from CLOB if available
                    trade.filled_at = datetime.utcnow()
                    session.commit()
                    logger.info(
                        f"ORDER FILLED: {group.city} {mkt.bin_label} "
                        f"filled at {price} size={size_shares:.4f}"
                    )
                    notifier.send(
                        f"✅ Заполнен: {group.city} {mkt.bin_label} | "
                        f"${price:.4f} × {size_shares:.4f} shares"
                    )
                else:
                    # Timeout — cancel
                    cancel_order(order_id)
                    trade.status = "expired"
                    trade.cancelled_at = datetime.utcnow()
                    session.commit()
                    logger.warning(
                        f"ORDER EXPIRED: {group.city} {mkt.bin_label} — cancelled"
                    )
                    notifier.send(
                        f"⏰ Истёк: {group.city} {mkt.bin_label} | отменён"
                    )

    except Exception as exc:
        logger.error(f"[live_trader] Engine error: {exc}")
    finally:
        session.close()
