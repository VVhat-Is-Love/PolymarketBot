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
# Equity — cash + MTM of open positions; base for portfolio caps
# ---------------------------------------------------------------------------

# (value, timestamp, is_fallback)
_equity_cache: Optional[tuple[float, datetime, bool]] = None
_EQUITY_TTL = timedelta(minutes=5)
_EQUITY_FAIL_TTL = timedelta(minutes=1)   # retry sooner after API failure


def _get_equity() -> float:
    """
    Total equity = USDC cash in exchange + MTM value of open positions.
    Cache: 5 min on success, 1 min on API failure (retries sooner on outage).

    Fallback note: if portfolio API is unavailable, returns wallet cash only.
    Cap will shrink while cash is used as base — safe default (under-deploy
    beats over-deploy), recovers automatically within 1 scheduler cycle.
    """
    global _equity_cache
    now = datetime.utcnow()
    if _equity_cache is not None:
        val, ts, is_fallback = _equity_cache
        ttl = _EQUITY_FAIL_TTL if is_fallback else _EQUITY_TTL
        if now - ts < ttl:
            return val
    try:
        from src.telegram.stats import get_portfolio_breakdown
        breakdown = get_portfolio_breakdown()
        equity = breakdown.get("total", 0.0)
        if equity > 0.01:
            logger.info(f"[live_trader] Equity (cash + MTM): ${equity:.2f}")
            _equity_cache = (equity, now, False)
            return equity
    except Exception as exc:
        logger.warning(f"[live_trader] Equity fetch failed, falling back to cash: {exc}")
    equity = _get_wallet_balance()
    _equity_cache = (equity, now, True)
    return equity


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

def _verify_order_market(expected_city: str, market: "Market | None") -> bool:
    """
    G4-14 safety gate: confirm the order's market really belongs to expected_city.

    Re-parses the city from the market's actual Polymarket question and requires
    it to equal the group's city. Prevents the Milan→Los Angeles class of bug
    where 'LA' matched inside 'miLAn' and orders were routed to the wrong market.

    Logs [order] city={X} bin={Y} market_id={Z} polymarket_question="..." for
    every order so mismatches are visible. Returns False → caller MUST NOT place.
    """
    from src.config.cities import CITIES_WHITELIST
    from src.market.parsers import parse_city_from_title, canonical_city

    question = (market.question if market else "") or ""
    market_id = market.market_id if market else "?"
    bin_label = market.bin_label if market else "?"

    parsed = parse_city_from_title(question, CITIES_WHITELIST)
    parsed_canon = canonical_city(parsed) if parsed else None
    expected_canon = canonical_city(expected_city)

    match = parsed_canon is not None and parsed_canon == expected_canon
    verdict = "OK" if match else "MISMATCH"
    logger.info(
        f"[order] city={expected_city} bin={bin_label} market_id={market_id} "
        f'polymarket_question="{question}" parsed_city={parsed_canon} verify={verdict}'
    )
    if not match:
        logger.error(
            f"[order] BLOCKED — market {market_id} question is for "
            f"{parsed_canon!r}, not {expected_city!r}. Refusing to place order."
        )
    return match


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


def _log_no_depth(city: str, bin_label: str, token_no: str) -> None:
    """
    Log NO-ask book depth for tail capacity analysis.
    Emits one [tail_depth] INFO line per candidate, never blocks placement.
    """
    from src.market.clob_client import get_no_book_depth
    try:
        depth = get_no_book_depth(token_no)
        if not depth:
            logger.info(
                f"[tail_depth] {city} {bin_label}: book unavailable "
                f"(token={token_no[:12]}…)"
            )
            return
        levels: list[tuple[float, float]] = depth.get("ask_levels") or []
        depth_to_90 = sum(s for p, s in levels if p <= 0.90)
        spread = depth.get("spread")
        level_strs = [f"{p:.3f}×{s:.1f}" for p, s in levels[:6]]
        logger.info(
            f"[tail_depth] {city} {bin_label} | "
            f"no_ask_levels=[{', '.join(level_strs)}] | "
            f"depth_to_0.90={depth_to_90:.1f}sh | "
            f"spread={f'{spread:.3f}' if spread is not None else 'N/A'}"
        )
    except Exception as exc:
        logger.debug(f"[tail_depth] {city} {bin_label}: depth log failed: {exc}")


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

            # G4-14: verify market's Polymarket question matches the group city
            if not _verify_order_market(group.city, mkt):
                continue  # wrong-market guard — skip this leg

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

        wallet_balance = _get_equity()
        logger.info(f"[live_trader] Equity (cash + MTM): ${wallet_balance:.4f}")
        if wallet_balance < 0.10:
            logger.warning(
                f"[live_trader] Equity ${wallet_balance:.4f} < $0.10 — skipping ALL groups"
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
                from src.telegram.strategy_flags import is_hard_disabled as _hard
                _why = "HARD-DISABLED (no basket orders ever)" if _hard("basket_wide") else "disabled via Telegram bot"
                logger.info(f"[basket_wide] {group.city}: SKIP — strategy disabled: {_why}")

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
                from src.telegram.strategy_flags import is_hard_disabled as _hard
                _why = "HARD-DISABLED (no basket orders ever)" if _hard("basket_narrow") else "disabled via Telegram bot"
                logger.info(f"[basket_narrow] {group.city}: SKIP — strategy disabled: {_why}")

            # ── Strategy C (inline): tail_no after basket rejection ──────────
            # Runs only when basket_wide AND basket_narrow are both skipped or
            # rejected (no `continue` was hit above). Evaluates NO positions on
            # longshot bins using real CLOB NO ask prices and the same Gaussian
            # model that powers the standalone run_tail_engine.
            if _strat_enabled("tail_no"):
                from src.strategy.tail_seller import scan_tails as _scan_tails
                from src.market.clob_client import get_no_ask_prices as _get_no_asks
                from src.market.order_executor import place_limit_order as _place_lo

                # Hours until resolution (resolution_date = end-of-day UTC 23:59)
                _closes_at = datetime.combine(group.resolution_date, dtime(23, 59))
                _h2c = (_closes_at - datetime.utcnow()).total_seconds() / 3600

                # Capital already locked in open NO positions (across all groups)
                _no_in_use: float = float(
                    session.execute(
                        select(sa_func.coalesce(sa_func.sum(LiveTrade.stake_usd), 0.0))
                        .where(
                            LiveTrade.strategy_name == "tail_no",
                            LiveTrade.status.in_(["pending", "open", "filled"]),
                        )
                    ).scalar()
                    or 0.0
                )

                # Fetch real CLOB NO ask prices for this group's markets
                _no_token_ids = [m.token_id_no for m in markets if m.token_id_no]
                _no_asks_map: dict[str, float | None] | None = None
                if _no_token_ids:
                    try:
                        _raw = _get_no_asks(_no_token_ids)
                        _no_asks_map = {
                            m.market_id: _raw.get(m.token_id_no)
                            for m in markets if m.token_id_no
                        }
                    except Exception as _exc:
                        logger.warning(
                            f"[tail] {group.city}: NO ask fetch failed: {_exc}"
                        )

                _scan = _scan_tails(
                    markets=markets,
                    prices=prices,
                    consensus_temp=result.consensus_temp,
                    unit=unit,
                    hours_to_close=_h2c,
                    available_capital=wallet_balance,   # equity already (see above)
                    no_capital_in_use=_no_in_use,
                    ss=ss,
                    no_asks=_no_asks_map,
                )

                # Log skip reasons at INFO so they're visible without DEBUG
                for _skip in _scan.skipped:
                    logger.info(f"[tail] {group.city}: SKIP {_skip}")

                for _o in _scan.orders:
                    logger.info(
                        f"[tail] {group.city}: NO {_o.bin_label} "
                        f"model_no={_o.p_model_no:.0%} "
                        f"ask={_o.no_ask:.3f} "
                        f"edge={_o.edge:+.3f}"
                    )
                    # Additional model-confidence gate (P_model_no must be strong)
                    if _o.p_model_no < ss.tail_min_model_no:
                        logger.info(
                            f"[tail] {group.city}: NO {_o.bin_label} "
                            f"SKIP model_no={_o.p_model_no:.0%} < "
                            f"{ss.tail_min_model_no:.0%} threshold"
                        )
                        continue

                    # Avoid placing twice if standalone tail engine already placed
                    _existing_tail = session.execute(
                        select(LiveTrade).where(
                            LiveTrade.group_id == group.group_id,
                            LiveTrade.strategy_name == "tail_no",
                            LiveTrade.status.in_(["pending", "open"]),
                        )
                    ).scalars().first()
                    if _existing_tail:
                        logger.info(
                            f"[tail] {group.city}: NO {_o.bin_label} "
                            "SKIP already has open tail_no trade"
                        )
                        break

                    # Depth log: NO book capacity before risk gates (pure monitoring)
                    _mkt_d = next((m for m in markets if m.market_id == _o.market_id), None)
                    _tok_d = (_mkt_d.token_id_no if _mkt_d else None)
                    if _tok_d:
                        _log_no_depth(group.city, _o.bin_label, _tok_d)

                    # Zone anti-correlation check (entry guard only, not an exit/stop)
                    _z_ok, _z_why = risk_manager.can_place_tail_zone(group.city)
                    if not _z_ok:
                        logger.warning(f"[tail_no] Zone block {group.city}: {_z_why}")
                        continue

                    # Portfolio + loss cap (equity-based)
                    _can, _why = risk_manager.can_place_tail(_o.stake_usd, wallet_balance)
                    if not _can:
                        logger.warning(
                            f"[tail_no] Risk block {group.city} {_o.bin_label}: {_why}"
                        )
                        continue

                    _mkt = next((m for m in markets if m.market_id == _o.market_id), None)
                    _tok_no = (_mkt.token_id_no if _mkt else None) or ""
                    if not _tok_no:
                        logger.warning(
                            f"[tail_no] No token_id_no for {_o.market_id} "
                            f"({group.city} {_o.bin_label}) — skip"
                        )
                        continue

                    # G4-14: verify market's Polymarket question matches the group city
                    if not _verify_order_market(group.city, _mkt):
                        continue  # wrong-market guard — skip this tail order

                    # G4-20: aggregate per-token cap (reserves notional this run)
                    _tcan, _twhy = risk_manager.can_place_tail_token(_tok_no, _o.stake_usd)
                    if not _tcan:
                        logger.warning(f"[tail_no] {group.city} {_o.bin_label}: {_twhy}")
                        continue

                    _tail_trade = LiveTrade(
                        id=str(uuid.uuid4()),
                        group_id=group.group_id,
                        market_id=_o.market_id,
                        bin_label=_o.bin_label,
                        target_price=_o.no_ask,
                        size_shares=_o.shares,
                        stake_usd=_o.stake_usd,
                        basket_role="tail_no",
                        strategy_name="tail_no",
                        city=group.city,
                        status="pending",
                        side="buy",
                        placed_at=datetime.utcnow(),
                        condition_id=(_mkt.condition_id if _mkt else None),
                        token_id=_tok_no,
                    )
                    session.add(_tail_trade)
                    session.commit()

                    notifier.send(
                        f"📤 [tail_no] NO-хвост (inline): {group.city} {_o.bin_label}\n"
                        f"${_o.stake_usd:.2f} @ {_o.no_ask:.3f} | "
                        f"P(NO)={_o.p_model_no:.0%} edge={_o.edge:+.1%} "
                        f"EV=${_o.ev_usd:+.2f}"
                    )

                    _oid = _place_lo(
                        market_id=_o.market_id,
                        token_id=_tok_no,
                        side="BUY",
                        price=_o.no_ask,
                        size=_o.shares,
                    )
                    if _oid:
                        _tail_trade.order_id = _oid
                        _tail_trade.status = "open"
                        session.commit()
                        risk_manager.record_bet_placed(
                            group.group_id, _o.stake_usd, strategy="tail_no"
                        )
                        trades_placed += 1
                        logger.info(
                            f"[tail_no] ORDER PLACED (inline): "
                            f"{group.city} {_o.bin_label} "
                            f"order_id={_oid} stake=${_o.stake_usd:.2f} "
                            f"P(NO)={_o.p_model_no:.0%} edge={_o.edge:+.1%}"
                        )
                        notifier.send(
                            f"✅ Размещён [tail_no]: {group.city} {_o.bin_label} | "
                            f"order_id={_oid}"
                        )
                        break  # one tail_no per group per cycle
                    else:
                        _tail_trade.status = "cancelled"
                        _tail_trade.cancelled_at = datetime.utcnow()
                        session.commit()
                        risk_manager.release_tail_token_reservation(_tok_no, _o.stake_usd)
                        logger.warning(
                            f"[tail_no] Order placement failed: "
                            f"{group.city} {_o.bin_label}"
                        )

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

def _tail_ksigma_ok(
    bin_lo: float,
    bin_hi: float | None,
    consensus_temp: float,
    unit: str,
    ss,
) -> tuple[bool, float, float, float, float]:
    """
    k·σ distance gate: is the near bin boundary far enough from consensus?

    Returns (ok, near_bound, distance, threshold, k_eff).
    ok=True  → bin qualifies (distance ≥ k·σ), may proceed to risk gates.
    ok=False → bin is too close to consensus, caller should SKIP.

    near_bound: whichever bin edge is closest to consensus.
    k_eff: the k actually used (tail_k_sigma_above if bin is above consensus
           and tail_k_sigma_above > 0, else tail_k_sigma).
    """
    sigma_gate = (
        ss.tail_distance_sigma_f if unit.upper() == "F"
        else ss.tail_distance_sigma_f / 1.8
    )
    hi = float(bin_hi) if bin_hi is not None else bin_lo + 1.0
    above = bin_lo >= consensus_temp
    near = bin_lo if above else hi
    dist = abs(near - consensus_temp)
    k_above = getattr(ss, "tail_k_sigma_above", 0.0)
    k_eff = k_above if (above and k_above > 0.0) else ss.tail_k_sigma
    threshold = k_eff * sigma_gate
    return dist >= threshold, near, dist, threshold, k_eff


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
        equity = _get_equity()
        if equity < ss.min_order_notional_usd:
            logger.debug(
                f"[tail] Equity ${equity:.2f} < min_order_notional "
                f"${ss.min_order_notional_usd:.2f} — skip"
            )
            return

        tail_cap_usd = equity * settings.tail_deployed_pct
        logger.info(
            f"[risk] Equity caps: equity=${equity:.2f} | "
            f"total={settings.total_deployed_pct:.0%}=${equity * settings.total_deployed_pct:.2f} "
            f"tail={settings.tail_deployed_pct:.0%}=${tail_cap_usd:.2f} | "
            f"per-token=${ss.tail_max_position_usd:.2f} | "
            f"50/50 — оптимум при одной стратегии и недоказанном edge; "
            f"пересмотр Kelly (winrate) после 30-50 сделок и первых убытков."
        )

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
            logger.info(
                f"[tail] No groups in tail window "
                f"({ss.tail_min_hours_to_close:.0f}–{ss.tail_max_hours_to_close:.0f}h)"
            )
            return

        logger.info(f"[tail] Evaluating {len(groups)} group(s) in tail window")

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
                    available_capital=equity,
                    no_capital_in_use=no_in_use,
                    ss=ss,
                    no_asks=no_asks,
                )

                logger.info(
                    f"[tail] {group.city}: scan → "
                    f"{len(scan.orders)} order(s), {len(scan.skipped)} skipped"
                    + (f" [{scan.skipped[0]}]" if scan.skipped and not scan.orders else "")
                )
                for reason in scan.skipped:
                    logger.debug(f"[tail] {group.city}: {reason}")

                for o in scan.orders:
                    # Depth log: NO book capacity before risk gates (pure monitoring)
                    _mkt_d = next((m for m in markets if m.market_id == o.market_id), None)
                    _tok_d = (_mkt_d.token_id_no if _mkt_d else None)
                    if _tok_d:
                        _log_no_depth(group.city, o.bin_label, _tok_d)

                    # k·σ distance gate: near bin boundary ≥ k·σ from consensus.
                    # Fail-closed: missing market geometry or consensus → SKIP, never bypass.
                    _unit = (group.unit or "F").upper()
                    if _mkt_d is None or _mkt_d.bin_min is None:
                        logger.warning(
                            f"[tail_ksigma] {group.city} {o.bin_label}: "
                            f"SKIP (no market geometry — fail-closed)"
                        )
                        continue
                    if consensus_temp is None:
                        logger.warning(
                            f"[tail_ksigma] {group.city} {o.bin_label}: "
                            f"SKIP (consensus=None — fail-closed, no forecast data)"
                        )
                        continue
                    _ok, _near, _dist, _thr, _k = _tail_ksigma_ok(
                        float(_mkt_d.bin_min),
                        float(_mkt_d.bin_max) if _mkt_d.bin_max is not None else None,
                        consensus_temp,
                        _unit,
                        ss,
                    )
                    _sigma_gate = _thr / _k if _k > 0 else 0.0
                    _dir = "ABOVE" if _near >= consensus_temp else "BELOW"
                    _verdict = "PASS" if _ok else "SKIP"
                    logger.info(
                        f"[tail_ksigma] {group.city} {o.bin_label}: "
                        f"consensus={consensus_temp:.2f}°{_unit} "
                        f"near_edge={_near:.2f}°{_unit} "
                        f"dist={_dist:.2f}°{_unit} "
                        f"dir={_dir} "
                        f"sigma={_sigma_gate:.2f}°{_unit} "
                        f"k={_k:.1f} verdict={_verdict}"
                    )
                    if not _ok:
                        continue

                    # Zone anti-correlation check (entry guard only, not an exit/stop)
                    z_ok, z_why = risk_manager.can_place_tail_zone(group.city)
                    if not z_ok:
                        logger.warning(f"[tail_no] Zone block {group.city}: {z_why}")
                        continue

                    # Portfolio + loss cap (equity-based)
                    can, why = risk_manager.can_place_tail(o.stake_usd, equity)
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

                    # G4-14: verify market's Polymarket question matches the group city
                    if not _verify_order_market(group.city, mkt):
                        continue  # wrong-market guard — skip this tail order

                    # G4-20: aggregate per-token cap (reserves notional this run)
                    _tcan, _twhy = risk_manager.can_place_tail_token(token_no, o.stake_usd)
                    if not _tcan:
                        logger.warning(f"[tail_no] {group.city} {o.bin_label}: {_twhy}")
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
                        risk_manager.release_tail_token_reservation(token_no, o.stake_usd)
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


# ---------------------------------------------------------------------------
# Tail early exit — SELL a winning NO position to lock profit before REDEEM
# ---------------------------------------------------------------------------

# Adaptive-polling state (in-memory; resets on restart — next cycle re-derives).
_tail_last_eval: dict[str, datetime] = {}
_tail_last_bid: dict[str, float] = {}


def decide_tail_exit(
    best_bid: float, in_window: bool, *,
    take_profit: float, hard_floor: float, time_gate: float,
) -> str:
    """
    FIX-2 (2026-06-18): only take_profit is active.

    take_profit: bid ≥ take_profit → SELL immediately. At bid≥0.99 the position
    has already won; selling frees the capital slot without meaningful residual risk
    (and without waiting for market resolve, which can take hours).

    time_gate / hard_floor REMOVED: sold winning positions on thin-book bid spikes
    that recovered (Ankara: +$0.93 winner sold at −$2.09). Asymmetry: misfiring
    costs ~$3; not cutting a real loser costs ~$6. Net-negative in logged history.

    # HOOK — future running-max-confirmed loss exit:
    #   wire hard_floor / time_gate back here after σ-backtest + bias calibration
    #   on the resolved CalibrationLog sample. Signature unchanged for easy diff.
    """
    if best_bid >= take_profit:
        return "take_profit"
    return "hold"


def run_tail_early_exit(gamma: "GammaClient | None" = None) -> None:
    """
    Manage open NO positions — FIX-2: take_profit only, hold everything else.

    Each tick:
      - market RESOLVED   → status=pending_redeem → auto_redeem handles on-chain.
      - bid ≥ take_profit → SELL @ min(0.99, bid), free the capital slot.
      - otherwise         → hold; log bid for observability.

    time_gate / hard_floor removed (sold winners on thin-book noise, see FIX-2).
    Adaptive polling kept (post-peak or low-bid → hot=5 min, else normal=10 min).
    """
    from src.config.settings import settings
    from src.risk.risk_manager import risk_manager
    from src.market.order_executor import place_limit_order
    from src.market.clob_client import get_book_snapshot
    from src.notifications.telegram import get_notifier
    from src.strategy.local_time import local_hour

    if settings.trading_mode.lower() != "live":
        return

    take_profit = getattr(ss, "tail_take_profit_price", 0.99)
    peak_hour = int(getattr(ss, "tail_local_peak_hour", 16))
    hot_thr = getattr(ss, "tail_hot_bid_threshold", 0.65)
    hot_min = int(getattr(ss, "tail_poll_hot_minutes", 5))
    normal_min = int(getattr(ss, "tail_poll_normal_minutes", 10))
    session = get_session()
    notifier = get_notifier()
    _resolved_cache: dict[str, bool] = {}

    def _market_resolved(group_id: str) -> bool:
        """True if the market is closed/resolved (shares no longer tradeable)."""
        if not group_id:
            return False
        if group_id in _resolved_cache:
            return _resolved_cache[group_id]
        # Local signal first: _job_deactivate_resolved_groups sets is_active=False
        grp = session.get(MarketGroup, group_id)
        resolved = bool(grp is not None and grp.is_active is False)
        # Confirm with Gamma when available (covers lag before deactivation job runs)
        if not resolved and gamma is not None:
            try:
                resolved = gamma.is_resolved_event(group_id)
            except Exception:
                pass
        _resolved_cache[group_id] = resolved
        return resolved

    try:
        positions: list[LiveTrade] = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.strategy_name == "tail_no",
                    LiveTrade.status.in_(["open", "filled", "pending_redeem"]),
                    LiveTrade.token_id.isnot(None),
                    LiveTrade.order_id.isnot(None),
                )
            ).scalars().all()
        )
        if not positions:
            logger.debug("[tail_exit] No open tail_no positions to evaluate")
            return

        logger.info(f"[tail_exit] Evaluating {len(positions)} open tail_no position(s)")

        for t in positions:
            grp = session.get(MarketGroup, t.group_id) if t.group_id else None

            # Market resolved → hand off to auto_redeem
            if _market_resolved(t.group_id):
                if t.status != "pending_redeem":
                    t.status = "pending_redeem"
                    session.commit()
                logger.info(
                    f"[tail_exit] {t.city} {t.market_id}: RESOLVED → auto_redeem"
                )
                continue

            # Adaptive polling
            lhour = local_hour(grp)
            in_window = lhour is not None and lhour >= peak_hour
            last_bid = _tail_last_bid.get(t.id)
            hot = in_window or (last_bid is not None and last_bid < hot_thr)
            cadence = hot_min if hot else normal_min
            now = datetime.utcnow()
            last_eval = _tail_last_eval.get(t.id)
            if last_eval is not None and \
                    (now - last_eval) < timedelta(minutes=cadence) - timedelta(seconds=30):
                continue
            _tail_last_eval[t.id] = now

            try:
                snap = get_book_snapshot(t.token_id)
            except Exception as exc:
                logger.warning(f"[tail_exit] book fetch failed {t.market_id}: {exc}")
                continue
            if not snap:
                continue

            best_bid = snap.get("best_bid")
            if best_bid is None:
                logger.debug(f"[tail_exit] {t.city} {t.market_id}: no bid — skip")
                continue
            _tail_last_bid[t.id] = best_bid

            action = decide_tail_exit(
                best_bid, in_window,
                take_profit=take_profit, hard_floor=0.0, time_gate=0.0,
            )
            logger.info(
                f"[tail_exit] {t.city} {t.market_id}: bid={best_bid:.3f} "
                f"local_hour={lhour if lhour is not None else '?'} in_window={in_window} "
                f"poll={'hot' if hot else 'normal'} → {action}"
            )

            if action == "hold":
                continue

            # take_profit: SELL at bid, clamped to CLOB max 0.99 (T2)
            shares = t.size_shares or 0.0
            stake = t.stake_usd or 0.0
            exec_price = round(min(0.99, best_bid), 4)
            provisional = round(shares * exec_price - stake, 4)
            logger.info(
                f"[tail_exit] TAKE_PROFIT {t.city} {t.market_id} sell @ "
                f"{exec_price:.4f} est_pnl=${provisional:+.4f} "
                f"(shares={shares:.2f} stake=${stake:.2f})"
            )

            sell_oid = place_limit_order(
                market_id=t.market_id,
                token_id=t.token_id,
                side="SELL",
                price=exec_price,
                size=shares,
                skip_risk_check=True,
            )

            if sell_oid:
                t.status = "sold"
                t.pnl_usd = provisional
                t.resolved_at = t.resolved_at or datetime.utcnow()
                session.commit()
                _tail_last_eval.pop(t.id, None)
                _tail_last_bid.pop(t.id, None)
                try:
                    risk_manager.record_bet_result(t.id, t.group_id or "", provisional)
                except Exception:
                    pass
                notifier.send(
                    f"💰 [tail_exit/take_profit] {t.city} {t.bin_label} — продано @ "
                    f"{exec_price:.3f} | est PnL ${provisional:+.2f} (sell_order={sell_oid})"
                )
            else:
                logger.warning(
                    f"[tail_exit] SELL order failed {t.city} {t.market_id} "
                    f"take_profit @ {exec_price:.4f}"
                )

    except Exception:
        logger.exception("[tail_exit] Engine error in run_tail_early_exit")
    finally:
        session.close()
