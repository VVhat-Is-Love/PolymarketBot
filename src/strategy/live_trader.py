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
    """Batch query: one SQL instead of N queries (P2-12 fix)."""
    if not market_ids:
        return {}

    # Subquery: max snapshot_time per market_id
    latest_sub = (
        select(
            MarketSnapshot.market_id,
            sa_func.max(MarketSnapshot.snapshot_time).label("ts"),
        )
        .where(MarketSnapshot.market_id.in_(market_ids))
        .group_by(MarketSnapshot.market_id)
        .subquery()
    )
    rows = session.execute(
        select(MarketSnapshot).join(
            latest_sub,
            (MarketSnapshot.market_id == latest_sub.c.market_id)
            & (MarketSnapshot.snapshot_time == latest_sub.c.ts),
        )
    ).scalars().all()

    snap_map = {row.market_id: row for row in rows}
    result: dict[str, dict] = {}
    for mid in market_ids:
        row = snap_map.get(mid)
        result[mid] = {
            "price_yes": row.price_yes if row else None,
            "best_bid": row.best_bid if row else None,
        }
    return result


def _get_forecasts(session: Session, group: MarketGroup) -> list[WeatherSnapshot]:
    """
    Return one snapshot per model for group's city + resolution_date (exact UTC match).
    With timezone='UTC' in Open-Meteo fetches, forecast_date == resolution_date always.
    """
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

def _entry_price(ask_price: float, settings) -> float:
    """
    G2-3: Return the order entry price.
    In 'ask' mode (default): best_ask + entry_tick, capped at 0.99.
    This makes the limit order marketable — higher fill probability.
    """
    mode = getattr(settings, "entry_price_mode", "ask")
    if mode == "ask":
        tick = getattr(settings, "entry_tick", 0.01)
        return min(0.99, round(ask_price + tick, 4))
    return ask_price  # 'mid' mode: passive limit


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
    Single basket-level risk check → place order → write DB → poll fill.
    On any exception after partial placement, cancels all placed orders.
    """
    from src.market.order_executor import place_limit_order, cancel_order

    total_notional = decision.total_cost_usd
    n_legs = len(decision.bins)

    # Single basket-level risk gate (P1-7: replaces per-leg can_place_order)
    logger.info(
        f"[{strategy_name}] Risk gate {group.city}: "
        f"notional=${total_notional:.2f} legs={n_legs} — calling can_place_basket"
    )
    ok, reason = risk_manager.can_place_basket(total_notional, n_legs)
    if not ok:
        logger.info(f"[{strategy_name}] Risk gate BLOCKED {group.city}: {reason}")
        return
    logger.info(f"[{strategy_name}] Risk gate PASSED {group.city} — placing {n_legs} order(s)")

    # Shared basket_id links all legs for partial-fill detection (G2-5)
    basket_id = str(uuid.uuid4())
    placed_order_ids: list[str] = []

    try:
        markets_by_id = {m.market_id: m for m in markets}
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
            condition_id = (mkt.condition_id if mkt else None)

            if not token_id:
                logger.warning(
                    f"[{strategy_name}] No token_id_yes for {o.market_id} "
                    f"({group.city} {o.bin_label}) — skip leg"
                )
                continue

            # G2-3: use best_ask price mode (marketable limit = faster fill)
            entry_price = _entry_price(o.ask_price, settings)

            # Place order FIRST — risk already checked at basket level (skip_risk_check=True)
            order_id = place_limit_order(
                market_id=o.market_id,
                token_id=token_id,
                side="BUY",
                price=entry_price,
                size=o.shares,
                skip_risk_check=True,
            )

            if not order_id:
                logger.warning(f"[{strategy_name}] Order failed for {group.city} {o.bin_label}")
                continue

            # Write to DB only after confirmed order placement — status "open" (P1-7/P1-8)
            # Fill detection is handled by _job_reconcile_orders (non-blocking, every 2 min)
            trade = LiveTrade(
                id=str(uuid.uuid4()),
                group_id=group.group_id,
                market_id=o.market_id,
                bin_label=o.bin_label,
                target_price=entry_price,
                size_shares=o.shares,
                stake_usd=o.stake_usd,
                basket_role="basket",
                strategy_name=strategy_name,
                city=group.city,
                status="open",
                order_id=order_id,
                side="buy",
                placed_at=datetime.utcnow(),
                condition_id=condition_id,
                token_id=token_id,
                basket_id=basket_id,
            )
            session.add(trade)
            session.commit()
            placed_order_ids.append(order_id)
            logger.info(
                f"[{strategy_name}] ORDER PLACED: {group.city} {o.bin_label} "
                f"price={entry_price:.4f} order_id={order_id[:16]}…"
            )

        if placed_order_ids:
            risk_manager.record_bet_placed(group.group_id, total_notional, strategy=strategy_name)

    except Exception as exc:
        logger.error(f"[{strategy_name}] Basket error {group.city}: {exc}")
        for oid in placed_order_ids:
            try:
                cancel_order(oid)
            except Exception:
                pass
        raise
    finally:
        risk_manager.release_basket_reservation(total_notional, n_legs)


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

        logger.info(
            f"[live_trader] Engine start: now={now.strftime('%H:%M UTC')} "
            f"window=[{min_close.date()} → {horizon.date()}] "
            f"({ss.strategy_min_hours_to_close}h–{ss.strategy_time_horizon_hours}h)"
        )

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
            logger.info(
                f"[live_trader] 0 active groups in date window "
                f"[{min_close.date()} → {horizon.date()}] — nothing to trade"
            )
            return

        logger.info(
            f"[live_trader] {len(groups)} group(s) in window: "
            + ", ".join(f"{g.city}/{g.resolution_date}" for g in groups[:10])
            + ("…" if len(groups) > 10 else "")
        )

        wallet_balance = _get_wallet_balance()
        logger.info(f"[live_trader] Wallet balance: ${wallet_balance:.4f}")
        if wallet_balance < 0.10:
            logger.warning(
                f"[live_trader] Wallet ${wallet_balance:.4f} < $0.10 — skipping ALL groups"
            )
            return

        # Risk-manager snapshot for diagnostics
        rm_status = risk_manager.get_status()
        logger.info(
            f"[live_trader] Risk state: total_loss=${rm_status.total_loss:.2f} "
            f"daily_loss=${rm_status.daily_loss:.2f} "
            f"deployed=${rm_status.total_deployed_usd:.2f}/{rm_status.total_deployed_cap_usd:.2f} "
            f"open_bets={rm_status.concurrent_open_bets} "
            f"emergency_stop={rm_status.emergency_stop}"
        )

        rejection_counts: dict[str, int] = {}
        trades_placed = 0

        for group in groups:
            logger.info(f"[live_trader] ── Evaluating: {group.city} {group.resolution_date} ──")

            # Skip if this group already has an open/pending live trade
            existing = session.execute(
                select(LiveTrade).where(
                    LiveTrade.group_id == group.group_id,
                    LiveTrade.status.in_(["pending", "open"]),
                )
            ).scalars().first()
            if existing:
                logger.info(
                    f"[live_trader] SKIP {group.city}: existing {existing.strategy_name} "
                    f"trade id={existing.id[:8]} status={existing.status}"
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
                logger.info(f"[live_trader] SKIP {group.city}: no active markets in DB")
                continue

            market_ids = [m.market_id for m in markets]
            snapshots = _get_latest_snapshots(session, market_ids)
            prices = {mid: snapshots[mid]["price_yes"] for mid in market_ids}

            if all(p is None for p in prices.values()):
                logger.info(
                    f"[live_trader] SKIP {group.city}: all {len(market_ids)} market prices=None "
                    "(no snapshots — run _job_snapshot_markets)"
                )
                continue

            price_summary = ", ".join(
                f"{snapshots[mid].get('best_ask','?'):.3f}" if snapshots[mid].get('best_ask') else "None"
                for mid in market_ids[:5]
            )
            logger.info(
                f"[live_trader] {group.city}: {len(market_ids)} markets, "
                f"best_ask sample=[{price_summary}]"
            )

            forecasts = _get_forecasts(session, group)
            if not forecasts:
                logger.info(
                    f"[live_trader] SKIP {group.city}: no weather forecasts for "
                    f"{group.resolution_date} (city='{group.city}')"
                )
                continue
            logger.info(
                f"[live_trader] {group.city}: {len(forecasts)} weather model(s) "
                f"for {group.resolution_date}"
            )

            result = evaluate_checklist(group, markets, prices, forecasts)

            if not result.passed:
                reason_key = (result.rejection_reason or "unknown").split(":")[0]
                rejection_counts[reason_key] = rejection_counts.get(reason_key, 0) + 1
                logger.info(
                    f"[live_trader] SKIP {group.city}: checklist FAILED — "
                    f"{result.rejection_reason}"
                )
                continue

            logger.info(
                f"[live_trader] {group.city}: checklist PASSED — "
                f"consensus={result.consensus_temp} "
                f"edge={result.estimated_edge:.1%} "
                f"sum_prices={result.sum_of_prices:.3f}"
            )

            basket_items = result.basket
            if not basket_items:
                logger.info(f"[live_trader] SKIP {group.city}: no basket items after checklist")
                continue

            unit = group.unit or "F"

            # ── Fetch real CLOB YES ask prices (not Gamma mid/last) ────────
            # basket_decision needs real order-book ask to compute honest EV.
            # Gamma outcomePrices[0] is mid/last price — using it causes EV
            # to be computed on a surrogate and the EV gate to fire incorrectly.
            from src.market.clob_client import get_yes_ask_prices as _get_yes_asks
            token_yes_map: dict[str, str] = {
                m.token_id_yes: m.market_id
                for m in markets
                if m.token_id_yes
            }
            # Log raw CLOB response for the first YES token of this group (diagnostics)
            log_raw_token = next(iter(token_yes_map), None)
            yes_asks_by_token = _get_yes_asks(
                list(token_yes_map.keys()),
                log_raw_for=log_raw_token,
            )
            # Build market_id → ask_price; fall back to Gamma price_yes if CLOB returns None
            clob_ask_prices: dict[str, float] = {}
            for tok, mid in token_yes_map.items():
                clob_ask = yes_asks_by_token.get(tok)
                gamma_mid = prices.get(mid)
                chosen = clob_ask if clob_ask is not None else gamma_mid
                source = "CLOB" if clob_ask is not None else "Gamma_fallback"
                logger.info(
                    f"[live_trader] {group.city} market={mid} "
                    f"clob_ask={clob_ask} gamma_mid={gamma_mid} using={source}"
                )
                if chosen is not None:
                    clob_ask_prices[mid] = chosen

            if not clob_ask_prices:
                logger.info(
                    f"[live_trader] SKIP {group.city}: "
                    "no ask prices from CLOB or Gamma — cannot size basket"
                )
                continue

            all_bin_prices = clob_ask_prices

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
                    trades_placed += 1
                    continue  # one strategy per group per cycle

                logger.info(
                    f"[basket_wide] {group.city}: REJECTED — {decision_a.reason} "
                    f"→ trying basket_narrow"
                )
            else:
                logger.info(f"[basket_wide] {group.city}: SKIP — disabled via Telegram bot")

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
                    trades_placed += 1
                else:
                    logger.info(
                        f"[basket_narrow] {group.city}: REJECTED — {decision_b.reason}"
                    )
            else:
                logger.info(f"[basket_narrow] {group.city}: SKIP — disabled via Telegram bot")

        rejection_summary = ", ".join(
            f"{k}={v}" for k, v in sorted(rejection_counts.items(), key=lambda x: -x[1])
        )
        logger.info(
            f"[live_trader] Run complete: {len(groups)} groups evaluated | "
            f"placed={trades_placed} | rejections: {rejection_summary or 'none'}"
        )

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
                        condition_id=(mkt.condition_id if mkt else None),
                        token_id=token_no,
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
