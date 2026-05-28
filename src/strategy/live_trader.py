"""
Live trading engine — three parallel strategies, each tracked by strategy_name.

  A  basket_wide   — favourite + neighbours, up to basket_max_bins=5
  B  basket_narrow — favourite ± 1 neighbour, fallback when A is rejected
  C  tail_no       — BUY NO on longshot bins close to resolution (run_tail_engine)

Strategy A → B form a fallback chain within run_live_trade_engine() for each group.
Strategy C runs independently in run_tail_engine(), called separately by the scheduler.

Sizing uses basket_decision.py (equal-share, honest-EV gate) — not Kelly + trim.
"""
from __future__ import annotations

import statistics
import time
import uuid
from datetime import datetime, timedelta, time as dtime
from typing import TYPE_CHECKING, Optional

from loguru import logger
from sqlalchemy import select, func as sa_func
from sqlalchemy.orm import Session

from src.db.models import MarketGroup, Market, MarketSnapshot, WeatherSnapshot, LiveTrade
from src.db.session import get_session
from src.strategy.checklist import evaluate_checklist
from src.strategy.config import strategy_settings as ss

if TYPE_CHECKING:
    from src.market.gamma_client import GammaClient


# ---------------------------------------------------------------------------
# Wallet balance — cached, multi-source
# ---------------------------------------------------------------------------

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
    """Query USDC collateral balance via CLOB API (requires Level 2 credentials)."""
    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        from src.blockchain.polymarket_auth import get_clob_client
        client = get_clob_client()
        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if isinstance(resp, dict):
            raw = resp.get("balance") or resp.get("allowance")
            if raw is not None:
                bal = float(raw) / 1e6  # raw units have 6 decimals
                if bal > 0:
                    return bal
    except Exception as exc:
        logger.warning(f"[live_trader] CLOB balance API failed: {type(exc).__name__}: {exc}")
    return None


def _get_wallet_balance() -> float:
    """
    Return USDC balance, using a 1-hour cache.
    Priority: cached → blockchain (web3) → CLOB API → LIVE_WALLET_BALANCE_USD (.env).
    """
    global _balance_cache
    from src.config.settings import settings

    now = datetime.utcnow()
    if _balance_cache is not None:
        cached_val, cached_at = _balance_cache
        if now - cached_at < _BALANCE_TTL:
            logger.debug(f"[live_trader] Wallet balance (cached): ${cached_val:.2f}")
            return cached_val

    bal = _fetch_balance_blockchain()
    if bal is not None:
        logger.info(f"[live_trader] Wallet balance from blockchain: ${bal:.2f}")
        _balance_cache = (bal, now)
        return bal

    bal = _fetch_balance_from_api()
    if bal is not None:
        logger.info(f"[live_trader] Wallet balance from CLOB API: ${bal:.2f}")
        _balance_cache = (bal, now)
        return bal

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


# ---------------------------------------------------------------------------
# Session helpers — shared by basket and tail engines
# ---------------------------------------------------------------------------

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
    deduped: list[WeatherSnapshot] = []
    for r in rows:
        if r.source not in seen:
            seen.add(r.source)
            deduped.append(r)
    return deduped


def _quick_consensus(group: MarketGroup, forecasts: list[WeatherSnapshot]) -> float | None:
    """Mean forecast temp (°C→unit conversion as needed). Used by tail engine."""
    if group.metric == "high_temp":
        raw = [f.temp_forecast_max for f in forecasts if f.temp_forecast_max is not None]
    elif group.metric == "low_temp":
        raw = [f.temp_forecast_min for f in forecasts if f.temp_forecast_min is not None]
    else:
        return None
    if not raw:
        return None
    mean_c = statistics.mean(raw)
    return mean_c * 9 / 5 + 32 if (group.unit or "F").upper() == "F" else mean_c


def _poll_until_filled_or_timeout(
    order_id: str,
    timeout_minutes: int,
    poll_interval_sec: int = 30,
) -> str:
    """Poll order status until filled/cancelled or timeout. Returns final status string."""
    from src.market.order_executor import get_order_status
    deadline = datetime.utcnow() + timedelta(minutes=timeout_minutes)
    while datetime.utcnow() < deadline:
        status = get_order_status(order_id)
        if status in ("filled", "cancelled"):
            return status
        time.sleep(poll_interval_sec)
    return "expired"


# ---------------------------------------------------------------------------
# Order placement helper — shared by Strategy A and B
# ---------------------------------------------------------------------------

def _place_basket_orders(
    session: Session,
    group: MarketGroup,
    decision,                   # BasketDecision (accepted=True)
    markets: list[Market],
    strategy_name: str,         # "basket_wide" | "basket_narrow"
    settings,
    risk_manager,
    notifier,
) -> None:
    """
    Place real CLOB limit orders for an accepted basket decision.
    Records each bin as a LiveTrade row, polls for fills, handles timeouts.
    """
    from src.market.order_executor import place_limit_order, cancel_order

    markets_by_id = {m.market_id: m for m in markets}
    risk_recorded = False

    bin_labels = " / ".join(o.bin_label for o in decision.bins)
    notifier.send(
        f"🟢 Новая сделка [{strategy_name}]\n"
        f"{group.city} {group.resolution_date} | {len(decision.bins)} бин(а)\n"
        f"Бины: {bin_labels}\n"
        f"Стоимость: ${decision.total_cost_usd:.2f} | "
        f"Выплата: ${decision.payout_if_hit_usd:.2f} ({decision.gross_return_pct:+.1%})\n"
        f"EV: ${decision.expected_value_usd:+.2f} | "
        f"P(model)={decision.p_model:.0%} P(mkt)={decision.p_market:.0%}"
    )

    for o in decision.bins:
        mkt = markets_by_id.get(o.market_id)
        token_id = (mkt.token_id_yes if mkt else None) or ""

        trade = LiveTrade(
            id=str(uuid.uuid4()),
            group_id=group.group_id,
            market_id=o.market_id,
            bin_label=o.bin_label,
            target_price=o.ask_price,
            size_shares=o.shares,
            stake_usd=o.stake_usd,
            basket_role="basket",
            strategy_name=strategy_name,
            city=group.city,
            status="pending",
            side="buy",
        )
        session.add(trade)
        session.commit()

        if not token_id:
            logger.warning(
                f"[{strategy_name}] No token_id_yes for {o.market_id} "
                f"({group.city} {o.bin_label}) — skip"
            )
            trade.status = "cancelled"
            session.commit()
            continue

        order_id = place_limit_order(
            market_id=o.market_id,
            token_id=token_id,
            side="BUY",
            price=o.ask_price,
            size=o.shares,
        )

        if not order_id:
            trade.status = "cancelled"
            trade.cancelled_at = datetime.utcnow()
            session.commit()
            logger.warning(f"[{strategy_name}] Order placement failed for {o.market_id}")
            continue

        # Record the basket cost in risk manager on the first successful order
        if not risk_recorded:
            risk_manager.record_bet_placed(
                group.group_id, decision.total_cost_usd, strategy=strategy_name
            )
            risk_recorded = True

        trade.order_id = order_id
        trade.status = "open"
        session.commit()

        final_status = _poll_until_filled_or_timeout(
            order_id=order_id,
            timeout_minutes=settings.order_timeout_minutes,
        )

        if final_status == "filled":
            trade.status = "filled"
            trade.filled_price = o.ask_price
            trade.filled_at = datetime.utcnow()
            session.commit()
            logger.info(
                f"[{strategy_name}] ORDER FILLED: {group.city} {o.bin_label} "
                f"@ {o.ask_price:.4f} × {o.shares:.4f} shares"
            )
            notifier.send(
                f"✅ Заполнен [{strategy_name}]: {group.city} {o.bin_label} | "
                f"${o.ask_price:.4f} × {o.shares:.4f}"
            )
        else:
            cancel_order(order_id)
            trade.status = "expired"
            trade.cancelled_at = datetime.utcnow()
            session.commit()
            logger.warning(
                f"[{strategy_name}] ORDER EXPIRED: {group.city} {o.bin_label} — cancelled"
            )
            notifier.send(f"⏰ Истёк [{strategy_name}]: {group.city} {o.bin_label}")


# ---------------------------------------------------------------------------
# Main basket engine — Strategy A (basket_wide) → B (basket_narrow) fallback
# ---------------------------------------------------------------------------

def run_live_trade_engine(gamma: "GammaClient | None" = None) -> None:
    """
    For every active group in the trading window:
      1. Evaluate checklist (rules / consensus / quality).
      2. Try Strategy A (basket_wide, up to 5 bins, strict EV gate).
      3. If A rejected: try Strategy B (basket_narrow, centre ± 1, looser gate).
      One group → at most one active strategy at any time.
    """
    from src.config.settings import settings
    from src.risk.risk_manager import risk_manager
    from src.notifications.telegram import get_notifier
    from src.strategy.basket_decision import decide_basket, decide_basket_narrow
    from src.strategy.edge_filter import compute_bin_edges

    session = get_session()
    notifier = get_notifier()

    try:
        now = datetime.utcnow()
        horizon = now + timedelta(hours=ss.strategy_time_horizon_hours)
        min_close = now + timedelta(hours=ss.strategy_min_hours_to_close)

        groups: list[MarketGroup] = list(
            session.execute(
                select(MarketGroup).where(
                    MarketGroup.is_active == True,         # noqa: E712
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
            logger.warning(
                f"[live_trader] Wallet balance ${wallet_balance:.2f} too low — skipping"
            )
            return

        for group in groups:
            # Skip if this group already has an open/pending live trade
            existing = session.execute(
                select(LiveTrade).where(
                    LiveTrade.group_id == group.group_id,
                    LiveTrade.status.in_(["pending", "open"]),
                )
            ).scalars().first()
            if existing:
                logger.debug(
                    f"[live_trader] {group.city}: already has open trade "
                    f"({existing.strategy_name}) — skip"
                )
                continue

            markets: list[Market] = list(
                session.execute(
                    select(Market).where(
                        Market.group_id == group.group_id,
                        Market.is_active == True,          # noqa: E712
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
                    f"[live_trader] {group.city} {group.resolution_date}: "
                    f"checklist rejected — {result.rejection_reason}"
                )
                continue

            # Risk gate: can we place any order right now?
            can_trade, reason = risk_manager.can_place_order(settings.max_single_bet_usd)
            if not can_trade:
                logger.warning(f"[live_trader] Risk blocked for {group.city}: {reason}")
                notifier.send(f"🚨 Risk block: {reason}")
                continue

            basket_items = result.basket
            if not basket_items:
                continue

            unit = group.unit or "F"
            all_bin_prices = {mid: p for mid, p in prices.items() if p is not None}

            # Diagnostic per-bin edge logging (does NOT gate or select bins)
            if result.consensus_temp is not None:
                bin_edges = compute_bin_edges(
                    markets=markets,
                    consensus_temp=result.consensus_temp,
                    market_prices=prices,
                    unit=unit,
                    sigma_c=ss.edge_sigma_c,
                )
                for be in bin_edges.values():
                    logger.debug(
                        f"[diag] {group.city} {be.bin_label}: "
                        f"model={be.model_prob:.0%} market={be.market_prob:.0%} "
                        f"edge={be.edge:+.0%}"
                    )

            # ── Strategy A: basket_wide ──────────────────────────────────────
            from src.telegram.strategy_flags import is_enabled as _strat_enabled

            if _strat_enabled("basket_wide"):
                decision_a = decide_basket(
                    basket_items=basket_items,
                    all_bin_prices=all_bin_prices,
                    consensus_temp=result.consensus_temp,
                    unit=unit,
                    wallet_balance=wallet_balance,
                    ss=ss,
                )

                if decision_a.accepted:
                    logger.info(
                        f"[basket_wide] {group.city} {group.resolution_date}: ACCEPTED "
                        f"bins={len(decision_a.bins)} cost=${decision_a.total_cost_usd:.2f} "
                        f"P_model={decision_a.p_model:.0%} P_market={decision_a.p_market:.0%} "
                        f"EV=${decision_a.expected_value_usd:+.2f}"
                    )
                    _place_basket_orders(
                        session, group, decision_a, markets,
                        "basket_wide", settings, risk_manager, notifier,
                    )
                    continue  # one strategy per group per cycle

                logger.info(
                    f"[basket_wide] {group.city} {group.resolution_date}: "
                    f"rejected ({decision_a.reason}) → trying basket_narrow"
                )
            else:
                logger.debug(f"[basket_wide] disabled via bot — skip {group.city}")

            # ── Strategy B: basket_narrow (fallback) ────────────────────────
            if _strat_enabled("basket_narrow"):
                decision_b = decide_basket_narrow(
                    basket_items=basket_items,
                    all_bin_prices=all_bin_prices,
                    consensus_temp=result.consensus_temp,
                    unit=unit,
                    wallet_balance=wallet_balance,
                    ss=ss,
                )

                if decision_b.accepted:
                    logger.info(
                        f"[basket_narrow] {group.city} {group.resolution_date}: ACCEPTED "
                        f"bins={len(decision_b.bins)} cost=${decision_b.total_cost_usd:.2f} "
                        f"P_model={decision_b.p_model:.0%} P_market={decision_b.p_market:.0%} "
                        f"EV=${decision_b.expected_value_usd:+.2f}"
                    )
                    _place_basket_orders(
                        session, group, decision_b, markets,
                        "basket_narrow", settings, risk_manager, notifier,
                    )
                else:
                    logger.info(
                        f"[basket_narrow] {group.city} {group.resolution_date}: "
                        f"rejected ({decision_b.reason})"
                    )
            else:
                logger.debug(f"[basket_narrow] disabled via bot — skip {group.city}")

    except Exception:
        logger.exception("[live_trader] Engine error in run_live_trade_engine")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Tail engine — Strategy C: BUY NO on longshot bins close to resolution
# ---------------------------------------------------------------------------

def run_tail_engine() -> None:
    """
    Scan all groups in the tail time window for +EV NO positions.

    Completely independent from run_live_trade_engine (separate scheduler job).
    A group can simultaneously have a YES basket trade AND a NO tail trade.

    Capital management:
      - no_in_use  = Σ stake_usd of all open tail_no trades
      - portfolio cap = wallet_balance × tail_max_portfolio_pct
      - per-position cap = min(tail_max_position_usd, wallet × tail_max_position_pct)
    """
    from src.telegram.strategy_flags import is_enabled as _strat_enabled
    if not _strat_enabled("tail_no"):
        logger.info("[tail] tail_no disabled via Telegram bot — skip")
        return

    from src.config.settings import settings
    from src.risk.risk_manager import risk_manager
    from src.market.order_executor import place_limit_order
    from src.notifications.telegram import get_notifier
    from src.strategy.tail_seller import scan_tails
    from src.market.clob_client import get_no_ask_prices

    session = get_session()
    notifier = get_notifier()

    try:
        wallet = _get_wallet_balance()
        if wallet < ss.min_order_notional_usd:
            logger.debug(
                f"[tail] Wallet ${wallet:.2f} < min_order_notional "
                f"${ss.min_order_notional_usd:.2f} — skip"
            )
            return

        # Capital already locked in open NO positions
        no_in_use: float = (
            session.execute(
                select(sa_func.coalesce(sa_func.sum(LiveTrade.stake_usd), 0.0)).where(
                    LiveTrade.strategy_name == "tail_no",
                    LiveTrade.status.in_(["pending", "open", "filled"]),
                )
            ).scalar()
            or 0.0
        )

        # Groups that resolve within the tail time window
        now = datetime.utcnow()
        min_close_dt = now + timedelta(hours=ss.tail_min_hours_to_close)
        max_close_dt = now + timedelta(hours=ss.tail_max_hours_to_close)

        groups: list[MarketGroup] = list(
            session.execute(
                select(MarketGroup).where(
                    MarketGroup.is_active == True,             # noqa: E712
                    MarketGroup.resolution_date >= min_close_dt.date(),
                    MarketGroup.resolution_date <= max_close_dt.date(),
                )
            )
            .scalars()
            .all()
        )

        if not groups:
            logger.debug("[tail] No groups in tail time window")
            return

        for group in groups:
            try:
                markets: list[Market] = list(
                    session.execute(
                        select(Market).where(
                            Market.group_id == group.group_id,
                            Market.is_active == True,          # noqa: E712
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
                consensus_temp = _quick_consensus(group, forecasts)

                # Hours until the market resolves (resolution_date = end-of-day UTC)
                closes_at = datetime.combine(group.resolution_date, dtime(23, 59))
                hours_to_close = (closes_at - now).total_seconds() / 3600

                # Fetch real NO-token asks from CLOB order book (P1-5)
                no_token_ids = [m.token_id_no for m in markets if m.token_id_no]
                no_asks: dict[str, float | None] | None = None
                if no_token_ids:
                    try:
                        book_prices = get_no_ask_prices(no_token_ids)
                        no_asks = {
                            m.market_id: book_prices.get(m.token_id_no)
                            for m in markets
                            if m.token_id_no
                        }
                        logger.debug(
                            f"[tail] {group.city}: fetched {len(no_asks)} NO asks"
                        )
                    except Exception as exc:
                        logger.warning(
                            f"[tail] {group.city}: NO ask fetch failed, using approx: {exc}"
                        )
                        no_asks = None  # fall back to approximation

                scan = scan_tails(
                    markets=markets,
                    prices=prices,
                    consensus_temp=consensus_temp,
                    unit=group.unit or "F",
                    hours_to_close=hours_to_close,
                    available_capital=wallet,
                    no_capital_in_use=no_in_use,
                    ss=ss,
                    no_asks=no_asks,
                )

                for reason in scan.skipped:
                    logger.debug(f"[tail] {group.city}: {reason}")

                for o in scan.orders:
                    can, why = risk_manager.can_place_order(o.stake_usd)
                    if not can:
                        logger.warning(
                            f"[tail_no] Risk block {group.city} {o.bin_label}: {why}"
                        )
                        continue

                    mkt = next((m for m in markets if m.market_id == o.market_id), None)
                    token_no = (mkt.token_id_no if mkt else None) or ""
                    if not token_no:
                        logger.warning(
                            f"[tail_no] No token_id_no for {o.market_id} "
                            f"({group.city} {o.bin_label}) — skip"
                        )
                        continue

                    trade = LiveTrade(
                        id=str(uuid.uuid4()),
                        group_id=group.group_id,
                        market_id=o.market_id,
                        bin_label=o.bin_label,
                        target_price=o.no_ask,
                        size_shares=o.shares,
                        stake_usd=o.stake_usd,
                        basket_role="tail_no",
                        strategy_name="tail_no",
                        city=group.city,
                        status="pending",
                        side="buy",
                    )
                    session.add(trade)
                    session.commit()

                    notifier.send(
                        f"📤 [tail_no] NO-хвост: {group.city} {o.bin_label}\n"
                        f"${o.stake_usd:.2f} @ {o.no_ask:.3f} | "
                        f"P(NO)={o.p_model_no:.0%} edge={o.edge:+.1%} "
                        f"EV=${o.ev_usd:+.2f}"
                    )

                    order_id = place_limit_order(
                        market_id=o.market_id,
                        token_id=token_no,
                        side="BUY",
                        price=o.no_ask,
                        size=o.shares,
                    )

                    if order_id:
                        trade.order_id = order_id
                        trade.status = "open"
                        session.commit()
                        risk_manager.record_bet_placed(
                            group.group_id, o.stake_usd, strategy="tail_no"
                        )
                        no_in_use += o.stake_usd
                        logger.info(
                            f"[tail_no] ORDER PLACED: {group.city} {o.bin_label} "
                            f"order_id={order_id} stake=${o.stake_usd:.2f} "
                            f"P(NO)={o.p_model_no:.0%} edge={o.edge:+.1%}"
                        )
                        notifier.send(
                            f"✅ Размещён [tail_no]: {group.city} {o.bin_label} | "
                            f"order_id={order_id}"
                        )
                    else:
                        trade.status = "cancelled"
                        trade.cancelled_at = datetime.utcnow()
                        session.commit()
                        logger.warning(
                            f"[tail_no] Order placement failed for {o.market_id} "
                            f"({group.city} {o.bin_label})"
                        )

            except Exception:
                logger.exception(
                    f"[tail] Error processing group {group.group_id} ({group.city})"
                )

    except Exception:
        logger.exception("[tail] Engine error in run_tail_engine")
    finally:
        session.close()
