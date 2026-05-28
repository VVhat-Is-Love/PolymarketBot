import json
from datetime import datetime, timedelta, timezone, date, time as dtime
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy import select

from src.db.models import (
    MarketGroup, Market, MarketSnapshot, PaperTrade, ChecklistEvaluation,
    WeatherSnapshot, RejectedMarket,
)
from src.db.session import get_session
from src.strategy.config import strategy_settings as ss
from src.strategy.checklist import evaluate_checklist
from src.strategy.pnl_calculator import build_basket_json, find_winning_market, calculate_pnl
if TYPE_CHECKING:
    from src.market.gamma_client import GammaClient

# Rejection reasons that warrant a rejected_markets DB entry
_QUALITY_REJECTION_PREFIXES = (
    "cold_start_price",
    "low_volume",
    "market_too_young",
    "edge_too_high",
)

# Gamma price threshold: above this the market is considered resolved
_RESOLVED_PRICE_THRESHOLD = 0.95

# Fallback to weather API after this many days past resolution date
_WEATHER_FALLBACK_DAYS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_latest_prices(session: Session, market_ids: list[str]) -> dict[str, float | None]:
    """Return most recent price_yes per market_id."""
    return {mid: s["price_yes"] for mid, s in _get_latest_snapshots(session, market_ids).items()}


def _get_latest_snapshots(
    session: Session, market_ids: list[str]
) -> dict[str, dict]:
    """Return most recent {price_yes, best_bid} per market_id."""
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


def _get_forecasts_for_group(session: Session, group: MarketGroup) -> list[WeatherSnapshot]:
    """
    Latest multi-model Open-Meteo forecasts for group's city + resolution_date.
    Falls back to ±1 day for European cities where UTC conversion shifts the date.
    """
    from datetime import timedelta

    def _deduped_query(target_date) -> list[WeatherSnapshot]:
        rows = (
            session.execute(
                select(WeatherSnapshot).where(
                    WeatherSnapshot.city == group.city,
                    WeatherSnapshot.forecast_date == target_date,
                    WeatherSnapshot.source.startswith("open_meteo:"),  # type: ignore[union-attr]
                )
                .order_by(WeatherSnapshot.snapshot_time.desc())
            )
            .scalars()
            .all()
        )
        seen: set[str] = set()
        unique: list[WeatherSnapshot] = []
        for row in rows:
            if row.source not in seen:
                seen.add(row.source)
                unique.append(row)
        return unique

    forecasts = _deduped_query(group.resolution_date)
    if forecasts:
        return forecasts

    # G2-6: TZ-shift fallback for European cities (UTC+1/+2)
    for delta in (-1, 1):
        alt_date = group.resolution_date + timedelta(days=delta)
        fallback = _deduped_query(alt_date)
        if fallback:
            logger.debug(
                f"[paper_trader] {group.city}: no forecast for {group.resolution_date}, "
                f"using {alt_date} (TZ-shift fallback)"
            )
            return fallback

    return []


def _find_winner_from_gamma(
    gamma: "GammaClient", group_id: str
) -> tuple[str | None, bool]:
    """
    Query Gamma API for the settled outcome of a market group.

    Returns (winning_market_id | None, is_resolved).
    is_resolved=False means prices are still live — don't close the trade yet.
    """
    try:
        prices = gamma.get_prices_for_group(group_id)
    except Exception as e:
        logger.warning(f"Gamma resolution query failed for group {group_id}: {e}")
        return None, False

    if not prices:
        return None, False

    max_price = max(prices.values(), default=0.0)
    if max_price < _RESOLVED_PRICE_THRESHOLD:
        return None, False  # market still live

    winner_id = max(prices.keys(), key=lambda mid: prices[mid])
    return winner_id, True


def _approx_temp_from_market(session: Session, market_id: str) -> float | None:
    """Return midpoint of winning bin as approximate actual temperature."""
    m = session.get(Market, market_id)
    if m is None or m.bin_min is None:
        return None
    hi = m.bin_max if m.bin_max is not None else m.bin_min + 1
    return (m.bin_min + hi) / 2


# ---------------------------------------------------------------------------
# Main jobs
# ---------------------------------------------------------------------------

def run_paper_trade_engine() -> None:
    """Evaluate active groups within time horizon; open paper trades if checklist passes."""
    now = datetime.now(timezone.utc)
    horizon = timedelta(hours=ss.strategy_time_horizon_hours)
    cutoff_date = (now + horizon).date()

    session = get_session()
    try:
        groups: list[MarketGroup] = list(
            session.execute(
                select(MarketGroup).where(
                    MarketGroup.is_active == True,  # noqa: E712
                    MarketGroup.resolution_date >= now.date(),
                    MarketGroup.resolution_date <= cutoff_date,
                )
            )
            .scalars()
            .all()
        )

        # Deduplicate: keep highest-volume group per (city, metric, date)
        best: dict[tuple, MarketGroup] = {}
        for g in groups:
            key = (g.city, g.metric, g.resolution_date)
            if key not in best or (g.event_volume or 0) > (best[key].event_volume or 0):
                best[key] = g
        groups = list(best.values())

        # Filter out groups closing too soon
        min_close_dt = now + timedelta(hours=ss.strategy_min_hours_to_close)
        filtered = []
        for g in groups:
            closes_at = datetime.combine(g.resolution_date, dtime(23, 59), tzinfo=timezone.utc)
            if closes_at >= min_close_dt:
                filtered.append(g)
            else:
                logger.debug(
                    f"SKIP {g.city} {g.metric} {g.resolution_date} "
                    f"— closes in <{ss.strategy_min_hours_to_close}h"
                )
        groups = filtered

        dates = sorted(set(g.resolution_date for g in groups))
        logger.info(
            f"Paper-trade engine: evaluating {len(groups)} groups in horizon "
            f"(dates: {dates})"
        )

        for group in groups:
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

            snapshots = _get_latest_snapshots(session, [m.market_id for m in markets])
            prices = {mid: s["price_yes"] for mid, s in snapshots.items()}
            forecasts = _get_forecasts_for_group(session, group)

            # Early-skip: no price data at all
            all_prices = [s["price_yes"] for s in snapshots.values() if s["price_yes"] is not None]
            if not all_prices:
                logger.info(
                    f"SKIP: {group.city} {group.metric} {group.resolution_date} "
                    f"— no_price_data"
                )
                continue

            # Settled detection: one bin dominates with a wide spread
            max_p = max(all_prices)
            spread = max_p - min(all_prices)
            if max_p > 0.90 and spread > 0.70:
                logger.info(
                    f"SKIP: {group.city} {group.metric} {group.resolution_date} "
                    f"— settled (max={max_p:.3f} spread={spread:.3f})"
                )
                continue

            result = evaluate_checklist(group, markets, prices, forecasts)

            unit_label = group.unit or "F"
            temp_str = (
                f"consensus={result.consensus_temp:.1f}{unit_label}"
                if result.consensus_temp is not None else "consensus=N/A"
            )
            bin_mins = sorted(m.bin_min for m in markets if m.bin_min is not None)
            bin_range_str = (
                f"bins=[{bin_mins[0]:.0f}..{bin_mins[-1]:.0f}]" if bin_mins else "bins=[]"
            )

            evaluation = ChecklistEvaluation(
                group_id=group.group_id,
                rules_ok=result.rules_ok,
                consensus_ok=result.consensus_ok,
                sum_of_prices_ok=result.sum_of_prices_ok,
                stability_ok=result.stability_ok,
                edge_ok=result.edge_ok,
                consensus_temp=result.consensus_temp,
                sum_of_prices=result.sum_of_prices,
                estimated_edge=result.estimated_edge,
                passed=result.passed,
                triggered_paper_trade=False,
                rejection_reason=result.rejection_reason,
            )

            if result.passed:
                existing = session.execute(
                    select(PaperTrade).where(
                        PaperTrade.group_id == group.group_id,
                        PaperTrade.status == "open",
                    )
                ).scalars().first()

                if not existing:
                    # G2-7: realistic sizing mirroring live execution
                    # Use best_ask + entry_tick as simulated entry price
                    # Size by wallet balance × bankroll_fraction, capped at max_basket_cost_usd
                    try:
                        from src.strategy.live_trader import _get_wallet_balance
                        paper_wallet = _get_wallet_balance()
                    except Exception:
                        paper_wallet = ss.strategy_virtual_stake_usd * 3  # fallback

                    n_bins = len(result.basket)
                    entry_tick = getattr(ss, "entry_tick", 0.01)
                    raw_stake = paper_wallet * ss.bankroll_fraction_per_basket
                    total_stake = max(
                        ss.min_basket_stake_usd,
                        min(ss.max_basket_cost_usd, raw_stake),
                    )
                    per_bin_stake = total_stake / n_bins if n_bins else total_stake

                    # Build basket_json with realistic entry prices and shares
                    import json as _json
                    realistic_rows = []
                    for item in result.basket:
                        # Use market ask price + tick (or price_yes if no ask)
                        snap = snapshots.get(item.market.market_id, {})
                        ask = snap.get("best_ask") or item.price_yes or 0.01
                        entry_price = min(0.99, ask + entry_tick)
                        shares = round(per_bin_stake / entry_price, 4) if entry_price > 0 else 0.0
                        actual_stake = round(shares * entry_price, 4)
                        realistic_rows.append({
                            "market_id": item.market.market_id,
                            "bin_label": item.market.bin_label,
                            "bin_min": item.market.bin_min,
                            "bin_max": item.market.bin_max,
                            "price_yes": item.price_yes,
                            "entry_price": entry_price,
                            "stake_usd": actual_stake,
                            "shares": shares,
                        })
                    basket_json = _json.dumps(realistic_rows)
                    total_paper_stake = sum(r["stake_usd"] for r in realistic_rows)

                    # Snapshot volumes at entry time
                    bin_volumes = {
                        item.market.market_id: item.market.volume
                        for item in result.basket
                    }

                    # Basket range for consensus_offset
                    basket_lo = min(
                        (item.market.bin_min for item in result.basket if item.market.bin_min is not None),
                        default=None,
                    )
                    basket_hi = max(
                        (
                            item.market.bin_max if item.market.bin_max is not None else item.market.bin_min + 1
                            for item in result.basket
                            if item.market.bin_min is not None
                        ),
                        default=None,
                    )
                    consensus_offset = None
                    if basket_lo is not None and basket_hi is not None and result.consensus_temp is not None:
                        basket_center = (basket_lo + basket_hi) / 2
                        consensus_offset = result.consensus_temp - basket_center

                    trade = PaperTrade(
                        group_id=group.group_id,
                        decision_time=datetime.utcnow(),
                        consensus_temp=result.consensus_temp,
                        sum_of_prices=result.sum_of_prices,
                        estimated_edge=result.estimated_edge,
                        basket_json=basket_json,
                        virtual_stake_usd=round(total_paper_stake, 4),
                        status="open",
                        bin_volumes_json=json.dumps(bin_volumes),
                        consensus_offset=consensus_offset,
                        city=group.city,
                        market_type=group.metric,
                    )
                    session.add(trade)
                    evaluation.triggered_paper_trade = True
                    basket_labels = [b.market.bin_label for b in result.basket]
                    logger.info(
                        f"PAPER TRADE: {group.city} {group.metric} {group.resolution_date} "
                        f"{temp_str} unit={unit_label} "
                        f"sum={result.sum_of_prices:.2f} edge={result.estimated_edge:.1%} "
                        f"basket={basket_labels} — OPENED"
                    )
                else:
                    logger.debug(f"Already open trade for {group.group_id}")
            else:
                sum_str = f" sum={result.sum_of_prices:.2f}" if result.sum_of_prices is not None else ""
                logger.info(
                    f"REJECTED: {group.city} {group.metric} {group.resolution_date} "
                    f"— {result.rejection_reason}"
                    f" | {temp_str} unit={unit_label}{sum_str} {bin_range_str}"
                )

                # Save quality rejections to rejected_markets for analytics
                reason_key = (result.rejection_reason or "").split(":")[0]
                if reason_key in _QUALITY_REJECTION_PREFIXES:
                    details = {
                        "prices": {mid: p for mid, p in prices.items() if p is not None},
                        "volumes": result.bin_volumes_snapshot or {},
                        "consensus_temp": result.consensus_temp,
                        "reason_full": result.rejection_reason,
                    }
                    for market in markets:
                        rm = RejectedMarket(
                            market_id=market.market_id,
                            group_id=group.group_id,
                            reason=reason_key,
                            details_json=json.dumps(details),
                        )
                        session.add(rm)

            session.add(evaluation)
            session.commit()

    finally:
        session.close()


def run_resolve_paper_trades(gamma: "GammaClient | None" = None) -> None:
    """
    Resolve open paper trades after their resolution_date.

    Resolution strategy (in priority order):
      1. Polymarket Gamma API — find the bin with settled price ≥ 0.95
      2. Open-Meteo actual temperature (fallback after WEATHER_FALLBACK_DAYS)

    Discrepancies between API and weather outcomes are logged as warnings and
    sent as Telegram alerts when a notifier is configured.
    """
    from src.data.open_meteo import fetch_actual_temperature
    from src.config.cities import CITIES_WHITELIST

    session = get_session()
    try:
        today = date.today()
        open_trades: list[PaperTrade] = list(
            session.execute(
                select(PaperTrade).where(PaperTrade.status == "open")
            )
            .scalars()
            .all()
        )

        to_resolve = []
        for trade in open_trades:
            group = session.get(MarketGroup, trade.group_id)
            if group and group.resolution_date < today:
                to_resolve.append((trade, group))

        if not to_resolve:
            logger.debug("No trades to resolve")
            return

        logger.info(f"Resolving {len(to_resolve)} paper trades...")

        for trade, group in to_resolve:
            try:
                cfg = CITIES_WHITELIST.get(group.city)
                if not cfg:
                    logger.warning(f"City {group.city!r} not in whitelist, skipping resolution")
                    continue

                basket = json.loads(trade.basket_json)
                basket_ids = {item["market_id"] for item in basket}
                days_overdue = (today - group.resolution_date).days

                winner_id: str | None = None
                resolved_via: str | None = None

                # ── Strategy 1: Polymarket Gamma API ──────────────────
                if gamma is not None:
                    api_winner, is_resolved = _find_winner_from_gamma(gamma, group.group_id)
                    if is_resolved:
                        winner_id = api_winner
                        resolved_via = "polymarket_api"
                        approx_temp = _approx_temp_from_market(session, winner_id) if winner_id else None
                    elif days_overdue <= _WEATHER_FALLBACK_DAYS:
                        logger.debug(
                            f"Gamma: {group.city} {group.resolution_date} not yet resolved "
                            f"({days_overdue}d overdue) — will retry"
                        )
                        continue  # wait for Polymarket to settle

                # ── Strategy 2: Open-Meteo weather fallback ───────────
                # Used when: no gamma client, OR gamma client but market not
                # resolved after WEATHER_FALLBACK_DAYS days past resolution.
                actual_temp_c: float | None = None
                if resolved_via is None:
                    actual_temp_c = fetch_actual_temperature(
                        lat=cfg["lat"],
                        lon=cfg["lon"],
                        target_date=group.resolution_date,
                        metric=group.metric,
                    )
                    if actual_temp_c is None:
                        logger.warning(
                            f"Could not fetch actual temp for {group.city} {group.resolution_date}"
                        )
                        continue

                    actual_temp_native = (
                        actual_temp_c * 9 / 5 + 32 if (group.unit or "F") == "F" else actual_temp_c
                    )
                    winner_id = find_winning_market(trade.basket_json, actual_temp_native)
                    resolved_via = "weather_api"
                else:
                    # We have an API winner — also fetch weather for cross-check & actual_temp
                    actual_temp_c = fetch_actual_temperature(
                        lat=cfg["lat"],
                        lon=cfg["lon"],
                        target_date=group.resolution_date,
                        metric=group.metric,
                    )
                    if actual_temp_c is not None:
                        actual_temp_native = (
                            actual_temp_c * 9 / 5 + 32 if (group.unit or "F") == "F" else actual_temp_c
                        )
                        weather_winner = find_winning_market(trade.basket_json, actual_temp_native)
                        if weather_winner != winner_id:
                            logger.warning(
                                f"Resolution mismatch: {group.city} {group.resolution_date} "
                                f"Gamma={winner_id} Weather={weather_winner} (temp={actual_temp_native:.1f}°)"
                            )

                # ── Determine basket_miss and calculate PnL ───────────
                is_basket_miss = winner_id is not None and winner_id not in basket_ids

                if is_basket_miss:
                    pnl = -trade.virtual_stake_usd
                    status = "resolved_basket_miss"
                else:
                    pnl, status = calculate_pnl(trade.basket_json, winner_id, trade.virtual_stake_usd)

                # actual_temp for record: prefer real weather temp, fall back to bin midpoint
                stored_temp: float | None = None
                if actual_temp_c is not None:
                    stored_temp = (
                        actual_temp_c * 9 / 5 + 32 if (group.unit or "F") == "F" else actual_temp_c
                    )
                elif winner_id is not None:
                    stored_temp = _approx_temp_from_market(session, winner_id)

                trade.status = status
                trade.resolved_at = datetime.utcnow()
                trade.actual_temp = stored_temp
                trade.winning_market_id = winner_id
                trade.pnl_usd = pnl
                trade.resolved_via = resolved_via
                trade.basket_miss = is_basket_miss
                session.commit()

                logger.info(
                    f"RESOLVED: {group.city} {group.resolution_date} "
                    f"actual={stored_temp:.1f}° status={status} pnl=${pnl:+.2f} "
                    f"via={resolved_via}"
                    if stored_temp is not None else
                    f"RESOLVED: {group.city} {group.resolution_date} "
                    f"status={status} pnl=${pnl:+.2f} via={resolved_via}"
                )

            except Exception as e:
                logger.error(f"Resolution failed for trade {trade.id}: {e}")
    finally:
        session.close()
