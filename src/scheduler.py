import json
import time
from datetime import datetime, date, timedelta
import schedule
from loguru import logger
from sqlalchemy import select

from src.market.gamma_client import GammaClient
from src.market.markets import discover_and_save_weather_markets
from src.data.open_meteo import fetch_and_save_all_cities
from src.db.session import get_session
from src.db.repository import MarketGroupRepository, MarketSnapshotRepository
from src.db.models import MarketGroup, Market, MarketSnapshot, PaperTrade, DailySummary, LiveTrade
from src.strategy.paper_trader import run_paper_trade_engine, run_resolve_paper_trades
from src.strategy.live_trader import run_live_trade_engine, run_tail_engine
from src.notifications.telegram import get_notifier

_gamma = GammaClient()


_discovery_zero_count: int = 0  # G2-8: consecutive zero-event discovery counter


def _job_discover_markets() -> None:
    global _discovery_zero_count
    logger.info("JOB: market discovery")
    try:
        saved = discover_and_save_weather_markets(_gamma)
        if saved > 0:
            _discovery_zero_count = 0
            logger.info(f"Discovery added {saved} markets — triggering immediate snapshot")
            _job_snapshot_markets()
        else:
            _discovery_zero_count += 1
            logger.warning(
                f"[discovery] 0 new markets (cycle #{_discovery_zero_count})"
            )
            # G2-8: alert owner if discovery returns 0 for two consecutive cycles
            if _discovery_zero_count >= 2:
                _notifier_alert(
                    f"⚠️ Gamma discovery: 0 рынков найдено {_discovery_zero_count} цикла подряд. "
                    f"Gamma API может быть недоступен или изменился формат событий."
                )
                _discovery_zero_count = 0  # reset counter after alert
    except Exception as e:
        logger.error(f"Market discovery job failed: {e}")


def _notifier_alert(msg: str) -> None:
    """Send alert to owner only (not client). Used for operational issues."""
    try:
        notifier = get_notifier()
        notifier.send(msg)
    except Exception as exc:
        logger.warning(f"[notifier_alert] Failed to send alert: {exc}")


def _job_snapshot_markets() -> None:
    """Fetch current prices from Gamma API for all active groups."""
    logger.info("JOB: market snapshots (Gamma API)")
    session = get_session()
    snap_repo = MarketSnapshotRepository(session)

    try:
        groups: list[MarketGroup] = list(
            session.execute(
                select(MarketGroup).where(MarketGroup.is_active == True)  # noqa: E712
            ).scalars().all()
        )
        logger.info(f"Fetching prices for {len(groups)} active groups")

        saved = 0
        for group in groups:
            try:
                prices = _gamma.get_prices_for_group(group.group_id)
                if not prices:
                    continue

                markets: list[Market] = list(
                    session.execute(
                        select(Market).where(
                            Market.group_id == group.group_id,
                            Market.is_active == True,  # noqa: E712
                        )
                    ).scalars().all()
                )

                for market in markets:
                    price_yes = prices.get(market.market_id)
                    if price_yes is None:
                        continue
                    snap = MarketSnapshot(
                        market_id=market.market_id,
                        price_yes=price_yes,
                        price_no=round(1.0 - price_yes, 6),
                        mid=price_yes,
                        source="gamma",
                        snapshot_time=datetime.utcnow(),
                    )
                    snap_repo.insert(snap)
                    saved += 1

                time.sleep(0.2)
            except Exception as e:
                logger.error(f"Snapshot failed for group {group.group_id}: {e}")

        logger.info(f"Market snapshots: {saved} saved via Gamma API")
    finally:
        session.close()


def _job_deactivate_resolved_groups() -> None:
    logger.info("JOB: deactivate resolved groups")
    session = get_session()
    try:
        n = MarketGroupRepository(session).deactivate_resolved()
        if n:
            logger.info(f"Deactivated {n} resolved market groups (and their markets)")
    except Exception as e:
        logger.error(f"Deactivate-resolved job failed: {e}")
    finally:
        session.close()


def _job_fetch_open_meteo() -> None:
    logger.info("JOB: Open-Meteo multi-model forecast fetch")
    try:
        saved = fetch_and_save_all_cities()
        logger.info(f"Open-Meteo saved {saved} snapshots")
    except Exception as e:
        logger.error(f"Open-Meteo fetch job failed: {e}")


def _job_paper_trade_engine() -> None:
    logger.info("JOB: paper trade engine")
    try:
        run_paper_trade_engine()
    # except Exception as e:
    #     logger.error(f"Paper trade engine job failed: {e}")
    except Exception as exc:
        logger.exception("Paper trade engine job failed")



def _job_live_trade_engine() -> None:
    from src.config.settings import settings
    from src.risk.risk_manager import risk_manager
    if settings.trading_mode.lower() != "live":
        return
    if risk_manager.is_emergency_stopped():
        logger.warning("JOB: live trade engine SKIPPED — emergency stop active")
        return
    logger.info("JOB: live trade engine")
    try:
        run_live_trade_engine(gamma=_gamma)   # Strategy A (basket_wide) + B (basket_narrow)
        run_tail_engine()                      # Strategy C (tail_no — NO time-decay)
    except Exception as e:
        logger.error(f"Live trade engine job failed: {e}")


def _job_resolve_paper_trades() -> None:
    logger.info("JOB: resolve paper trades")
    try:
        run_resolve_paper_trades(gamma=_gamma)
    except Exception as e:
        logger.error(f"Resolve paper trades job failed: {e}")


def _job_reconcile_orders() -> None:
    """
    Non-blocking reconciliation: poll CLOB status for all open/pending orders.
    Runs every 2 minutes — replaces the blocking _poll_until_filled_or_timeout loop.
    Orders older than ORDER_TIMEOUT_MINUTES are cancelled and marked expired.
    """
    from datetime import timedelta
    from src.config.settings import settings
    from src.market.order_executor import get_order_status, cancel_order
    from src.notifications.telegram import get_notifier

    notifier = get_notifier()
    session = get_session()
    try:
        timeout = timedelta(minutes=settings.order_timeout_minutes)
        now = datetime.utcnow()

        pending_open = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.status.in_(["open", "pending"]),
                    LiveTrade.order_id.isnot(None),
                )
            ).scalars().all()
        )

        if not pending_open:
            return

        logger.debug(f"[reconcile] Checking {len(pending_open)} open/pending orders")

        for t in pending_open:
            try:
                clob_status = get_order_status(t.order_id)
            except Exception as exc:
                logger.warning(f"[reconcile] Status check failed for {t.order_id[:12]}…: {exc}")
                continue

            placed_at = t.placed_at or now

            if clob_status == "filled":
                t.status = "filled"
                t.filled_price = t.target_price
                t.filled_at = now
                session.commit()
                city = t.city or t.group_id or "?"
                logger.info(f"[reconcile] FILLED: {city} {t.bin_label} order={t.order_id[:12]}…")
                notifier.send(
                    f"✅ Исполнен [{t.strategy_name}]: {city} {t.bin_label} | "
                    f"${t.target_price:.4f} × {t.size_shares:.4f}"
                )

            elif clob_status == "cancelled":
                t.status = "cancelled"
                t.cancelled_at = now
                session.commit()
                logger.info(
                    f"[reconcile] CANCELLED by exchange: {t.city or '?'} {t.bin_label}"
                )

            elif (now - placed_at) > timeout:
                # Order timed out — cancel it
                try:
                    cancel_order(t.order_id)
                except Exception:
                    pass
                t.status = "expired"
                t.cancelled_at = now
                session.commit()
                city = t.city or t.group_id or "?"
                logger.warning(
                    f"[reconcile] EXPIRED (>{settings.order_timeout_minutes}m): "
                    f"{city} {t.bin_label} order={t.order_id[:12]}…"
                )
                notifier.send(f"⏰ Истёк [{t.strategy_name}]: {city} {t.bin_label}")

        # G2-5: After processing individual legs, detect partial basket fills
        _detect_partial_baskets(session, notifier)

    except Exception as exc:
        logger.error(f"[reconcile] Job failed: {exc}")
    finally:
        session.close()


def _detect_partial_baskets(session, notifier) -> None:
    """
    G2-5: Detect baskets where some legs filled and some expired.
    For each such basket: attempt one re-fill of expired legs at current ask.
    If re-fill also fails: mark basket_incomplete, alert owner.
    """
    from src.config.settings import settings as _settings
    from src.market.order_executor import place_limit_order, cancel_order
    from sqlalchemy import func as sa_func

    # Find basket_ids that have both filled and expired/cancelled legs
    baskets_with_mixed = session.execute(
        select(LiveTrade.basket_id, LiveTrade.status)
        .where(
            LiveTrade.basket_id.isnot(None),
            LiveTrade.status.in_(["filled", "expired", "cancelled"]),
        )
    ).all()

    if not baskets_with_mixed:
        return

    # Group by basket_id
    basket_status_map: dict[str, set[str]] = {}
    for bid, status in baskets_with_mixed:
        if bid:
            basket_status_map.setdefault(bid, set()).add(status)

    for basket_id, statuses in basket_status_map.items():
        has_filled = "filled" in statuses
        has_missing = bool(statuses & {"expired", "cancelled"})
        if not (has_filled and has_missing):
            continue

        all_legs: list[LiveTrade] = list(
            session.execute(
                select(LiveTrade).where(LiveTrade.basket_id == basket_id)
            ).scalars().all()
        )

        filled_legs = [l for l in all_legs if l.status == "filled"]
        missing_legs = [l for l in all_legs if l.status in ("expired", "cancelled")]

        if not missing_legs:
            continue

        city = (all_legs[0].city or basket_id[:8]) if all_legs else basket_id[:8]
        logger.warning(
            f"[reconcile/G2-5] Partial basket {basket_id[:8]}: "
            f"{len(filled_legs)} filled, {len(missing_legs)} missing — attempting re-fill"
        )

        # One retry: place new order for each missing leg at current ask
        refilled = 0
        for leg in missing_legs:
            from src.db.session import get_session as _gs
            from src.db.models import Market
            mkt = session.execute(
                select(Market).where(Market.market_id == leg.market_id)
            ).scalars().first()
            token_id = (mkt.token_id_yes if mkt else None) or leg.token_id or ""
            if not token_id or not leg.target_price or not leg.size_shares:
                continue

            order_id = place_limit_order(
                market_id=leg.market_id,
                token_id=token_id,
                side="BUY",
                price=min(0.99, leg.target_price + _settings.entry_tick),
                size=leg.size_shares,
                skip_risk_check=True,
            )
            if order_id:
                import uuid as _uuid
                from src.db.models import LiveTrade as _LT
                retry_trade = _LT(
                    id=str(_uuid.uuid4()),
                    group_id=leg.group_id,
                    market_id=leg.market_id,
                    bin_label=leg.bin_label,
                    target_price=leg.target_price,
                    size_shares=leg.size_shares,
                    stake_usd=leg.stake_usd,
                    basket_role=leg.basket_role,
                    strategy_name=leg.strategy_name,
                    city=leg.city,
                    status="open",
                    order_id=order_id,
                    side=leg.side,
                    placed_at=datetime.utcnow(),
                    condition_id=leg.condition_id,
                    token_id=leg.token_id,
                    basket_id=basket_id,
                )
                session.add(retry_trade)
                # Mark the old expired leg as basket_refill_attempted so we don't retry again
                leg.basket_role = "expired_refilled"
                session.commit()
                refilled += 1
                logger.info(f"[reconcile/G2-5] Re-placed leg {leg.bin_label} order={order_id[:12]}…")

        if refilled < len(missing_legs):
            still_missing = len(missing_legs) - refilled
            notifier.send(
                f"⚠️ Частичная корзина [{all_legs[0].strategy_name if all_legs else '?'}]: "
                f"{city} — {len(filled_legs)}/{len(all_legs)} бинов исполнено, "
                f"{still_missing} не удалось перезалить\n"
                f"basket_id={basket_id[:8]}"
            )
            logger.warning(
                f"[reconcile/G2-5] Basket {basket_id[:8]} incomplete: "
                f"{still_missing} legs still missing after retry"
            )
        elif refilled > 0:
            logger.info(
                f"[reconcile/G2-5] Basket {basket_id[:8]}: re-filled {refilled} missing leg(s)"
            )


def _job_expire_stale_pending() -> None:
    """Mark pending limit orders older than 2 hours as expired (FIX 5)."""
    from datetime import timedelta
    logger.debug("JOB: expire stale pending orders")
    session = get_session()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=2)
        stale = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.status == "pending",
                    LiveTrade.placed_at < cutoff,
                )
            ).scalars().all()
        )
        if stale:
            for t in stale:
                t.status = "expired"
            session.commit()
            logger.info(f"[expire_pending] Expired {len(stale)} stale pending orders (>2h)")
    except Exception as e:
        logger.error(f"Expire stale pending job failed: {e}")
    finally:
        session.close()


def _job_reconcile_pnl() -> None:
    """
    G3-2: Reconcile live trade PnL against Polymarket Data API.

    Resolution gating: Gamma is_resolved_event(group_id) — not cash-flow heuristics.
    PnL source: realized_pnl_by_token keyed by token_id.
    Dedup: one primary row per token_id gets the real PnL; duplicates (retries) get 0.
    PnL=$0 on a resolved losing position IS valid and IS written.
    """
    from collections import defaultdict as _dd
    from src.config.settings import settings
    from src.market.polymarket_data import get_activity, realized_pnl_by_token
    from src.notifications.telegram import get_notifier

    if not settings.proxy_wallet_address:
        logger.debug("[reconcile_pnl] No proxy_wallet_address — skip")
        return

    notifier = get_notifier()
    session = get_session()
    try:
        unreconciled: list[LiveTrade] = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.token_id.isnot(None),
                    LiveTrade.group_id.isnot(None),
                    LiveTrade.reconciled_with_polymarket == False,  # noqa: E712
                    LiveTrade.status.in_(["filled", "open", "expired", "cancelled"]),
                )
            ).scalars().all()
        )

        if not unreconciled:
            logger.debug("[reconcile_pnl] No unreconciled trades with token_id")
            return

        logger.info(f"[reconcile_pnl] Checking {len(unreconciled)} trades against Data API")

        activity = get_activity(settings.proxy_wallet_address)
        pnl_map = realized_pnl_by_token(activity)

        # Cache Gamma resolution per group_id to avoid duplicate API calls
        resolved_cache: dict[str, bool] = {}

        def _is_resolved(group_id: str) -> bool:
            if group_id not in resolved_cache:
                resolved_cache[group_id] = _gamma.is_resolved_event(group_id)
            return resolved_cache[group_id]

        # Group by token_id for dedup; then by group_id for resolution check
        by_token: dict[str, list[LiveTrade]] = _dd(list)
        for t in unreconciled:
            if t.token_id:
                by_token[t.token_id].append(t)

        for token_id, trades in by_token.items():
            group_id = trades[0].group_id
            if not group_id or not _is_resolved(group_id):
                continue  # market still live — skip

            # PnL from Polymarket cash flows (0.0 for a loss = valid)
            realized = pnl_map.get(token_id, 0.0)

            # Primary = filled > open > most-recently-placed (handles retries)
            primary = max(
                trades,
                key=lambda t: (
                    t.status == "filled",
                    t.status == "open",
                    t.placed_at or datetime.min,
                ),
            )
            primary.pnl_usd = round(realized, 4)
            primary.reconciled_with_polymarket = True
            primary.resolved_at = primary.resolved_at or datetime.utcnow()
            primary.status = "resolved"

            # Dedup: retries with same token get pnl=0 (the real PnL is on primary)
            for t in trades:
                if t is not primary and not t.reconciled_with_polymarket:
                    t.pnl_usd = 0.0
                    t.reconciled_with_polymarket = True
                    t.status = "resolved"

            session.commit()

            pnl_sign = "+" if realized >= 0 else ""
            city = primary.city or group_id or "?"
            logger.info(
                f"[reconcile_pnl] ✅ {city} {primary.bin_label} "
                f"PnL={pnl_sign}${realized:.2f} token={token_id[:12]}…"
                + (f" ({len(trades)-1} deduped)" if len(trades) > 1 else "")
            )
            outcome_em = "✅" if realized > 0 else "❌"
            notifier.send(
                f"{outcome_em} [{primary.strategy_name}] {city} {primary.bin_label} — "
                f"закрыта {pnl_sign}${abs(realized):.2f} (сверено Polymarket)"
            )

    except Exception as exc:
        logger.error(f"[reconcile_pnl] Job failed: {exc}")
    finally:
        session.close()


def _job_resolve_live_trades() -> None:
    """
    LEGACY: weather-based resolution for trades WITHOUT condition_id.
    For trades that were placed before G2-1 (no Polymarket matching keys).
    New trades use _job_reconcile_pnl instead.
    """
    session = get_session()
    notifier = get_notifier()
    try:
        unresolved: list[LiveTrade] = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.status == "filled",
                    LiveTrade.resolved_at.is_(None),
                )
            ).scalars().all()
        )

        if not unresolved:
            logger.debug("[resolve_live] No unresolved filled trades")
            return

        logger.info(f"[resolve_live] Checking {len(unresolved)} unresolved live trades")

        # Batch by group_id to minimise Gamma API calls
        by_group: dict[str, list[LiveTrade]] = {}
        for t in unresolved:
            by_group.setdefault(t.group_id or t.market_id, []).append(t)

        for group_id, trades in by_group.items():
            try:
                prices = _gamma.get_prices_for_group(group_id)
                if not prices:
                    continue

                for t in trades:
                    price_yes = prices.get(t.market_id)
                    if price_yes is None:
                        continue

                    if price_yes > 0.99:
                        yes_won = True
                    elif price_yes < 0.01:
                        yes_won = False
                    else:
                        continue  # market still live

                    shares = t.size_shares or 0.0
                    stake = t.stake_usd or 0.0
                    bought_yes = (t.strategy_name != "tail_no")

                    if bought_yes:
                        pnl = (shares - stake) if yes_won else -stake
                    else:  # bought NO shares
                        pnl = (shares - stake) if (not yes_won) else -stake

                    t.pnl_usd = round(pnl, 4)
                    t.resolved_at = datetime.utcnow()
                    session.commit()

                    outcome_em = "✅" if pnl > 0 else "❌"
                    city = t.city or group_id[:8]
                    logger.info(
                        f"[resolve_live] {city} {t.bin_label} "
                        f"({'YES' if bought_yes else 'NO'} "
                        f"{'ВЫИГРАл' if pnl > 0 else 'ПРОИГРАл'}) "
                        f"PnL=${pnl:+.2f}"
                    )
                    side_tag = "YES" if bought_yes else "NO"
                    pnl_sign = "+" if pnl >= 0 else "−"
                    notifier.send(
                        f"{outcome_em} {city} {t.bin_label} {side_tag} — "
                        f"закрыта {pnl_sign}${abs(pnl):.2f}"
                    )

            except Exception as e:
                logger.error(f"[resolve_live] Error for group {group_id}: {e}")

    except Exception as e:
        logger.error(f"[resolve_live] Job failed: {e}")
    finally:
        session.close()


def _job_paper_trade_summary() -> None:
    session = get_session()
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())

        open_trades = list(
            session.execute(select(PaperTrade).where(PaperTrade.status == "open"))
            .scalars().all()
        )
        resolved_today = list(
            session.execute(
                select(PaperTrade).where(
                    PaperTrade.resolved_at >= today_start,
                    PaperTrade.status != "open",
                )
            ).scalars().all()
        )

        wins = sum(1 for t in resolved_today if t.status == "resolved_win")
        basket_misses = sum(1 for t in resolved_today if t.status == "resolved_basket_miss")
        losses = len(resolved_today) - wins - basket_misses
        pnl = sum(t.pnl_usd or 0.0 for t in resolved_today)
        rate = f"{wins/len(resolved_today)*100:.0f}%" if resolved_today else "N/A"

        logger.info(
            f"SUMMARY: Open={len(open_trades)} | "
            f"Today: {len(resolved_today)} ({wins}W/{losses}L/{basket_misses}BM) "
            f"PnL=${pnl:+.2f} winrate={rate}"
        )
    except Exception as e:
        logger.error(f"Summary job failed: {e}")
    finally:
        session.close()


def _job_settle_paper() -> None:
    """
    G3-3: Settle open paper trades by Gamma outcome — NOT weather, NOT Data API.

    For each open PaperTrade: check if the event is resolved via Gamma. If yes,
    find the winning market_id (price ≥ 0.99), compute PnL from basket_json
    (shares × $1 - total_cost), and mark resolved.

    Paper trades are simulations — they have no /activity entry on Polymarket.
    Gamma prices are the canonical outcome for resolution.
    """
    import json as _json
    session = get_session()
    notifier = get_notifier()
    try:
        open_trades: list[PaperTrade] = list(
            session.execute(
                select(PaperTrade).where(PaperTrade.status == "open")
            ).scalars().all()
        )

        if not open_trades:
            logger.debug("[settle_paper] No open paper trades")
            return

        logger.info(f"[settle_paper] Checking {len(open_trades)} open paper trades via Gamma")
        resolved_count = 0

        for pt in open_trades:
            if not pt.group_id:
                continue
            if not _gamma.is_resolved_event(pt.group_id):
                continue

            winner_mid = _gamma.winning_market_id(pt.group_id)

            # Compute payout from basket_json
            try:
                basket = _json.loads(pt.basket_json)
            except Exception:
                basket = []

            total_cost = pt.virtual_stake_usd or sum(b.get("stake_usd", 0) for b in basket)
            winning_bin = next((b for b in basket if str(b.get("market_id")) == str(winner_mid)), None)

            if winning_bin:
                shares = winning_bin.get("shares") or 0.0
                payout = float(shares) * 1.0
                pnl = round(payout - total_cost, 4)
                status = "resolved_win" if pnl > 0 else "resolved_partial" if payout > 0 else "resolved_loss"
            else:
                pnl = round(-total_cost, 4)
                status = "resolved_basket_miss" if winner_mid else "resolved_loss"

            pt.pnl_usd = pnl
            pt.status = status
            pt.resolved_at = datetime.utcnow()
            pt.winning_market_id = winner_mid
            pt.resolved_via = "gamma_api"
            resolved_count += 1

            city = pt.city or pt.group_id or "?"
            outcome_em = "✅" if pnl > 0 else "❌"
            logger.info(
                f"[settle_paper] {city} → {status} PnL=${pnl:+.2f} winner={winner_mid}"
            )
            notifier.send(
                f"{outcome_em} [paper] {city} — {status} PnL=${pnl:+.2f}"
            )

        if resolved_count:
            session.commit()
            logger.info(f"[settle_paper] Settled {resolved_count} paper trades via Gamma")

    except Exception as exc:
        logger.error(f"[settle_paper] Job failed: {exc}")
        session.rollback()
    finally:
        session.close()


def _job_cleanup_snapshots() -> None:
    """Weekly cleanup: delete MarketSnapshot rows older than 7 days (P2-13)."""
    from sqlalchemy import delete as sa_delete
    session = get_session()
    try:
        cutoff = datetime.utcnow() - timedelta(days=7)
        result = session.execute(
            sa_delete(MarketSnapshot).where(MarketSnapshot.snapshot_time < cutoff)
        )
        session.commit()
        logger.info(f"[cleanup_snapshots] Deleted {result.rowcount} snapshot rows older than 7 days")
    except Exception as exc:
        logger.error(f"[cleanup_snapshots] Failed: {exc}")
        session.rollback()
    finally:
        session.close()


def _job_daily_summary() -> None:
    """Build and store a full daily PnL report; send to Telegram if configured."""
    logger.info("JOB: daily summary")
    notifier = get_notifier()
    session = get_session()
    try:
        yesterday = date.today() - timedelta(days=1)
        day_start = datetime.combine(yesterday, datetime.min.time())
        day_end = datetime.combine(date.today(), datetime.min.time())

        # ── Paper trades (simulation) ──────────────────────────────────────
        resolved = list(
            session.execute(
                select(PaperTrade).where(
                    PaperTrade.resolved_at >= day_start,
                    PaperTrade.resolved_at < day_end,
                    PaperTrade.status != "open",
                )
            ).scalars().all()
        )
        open_trades = list(
            session.execute(select(PaperTrade).where(PaperTrade.status == "open"))
            .scalars().all()
        )

        wins = sum(1 for t in resolved if t.status == "resolved_win")
        losses = sum(1 for t in resolved if t.status == "resolved_loss")
        basket_misses = sum(1 for t in resolved if t.status == "resolved_basket_miss")
        total_pnl = sum(t.pnl_usd or 0.0 for t in resolved)

        all_resolved = list(
            session.execute(
                select(PaperTrade).where(PaperTrade.status != "open")
            ).scalars().all()
        )
        paper_hwm = sum(t.pnl_usd or 0.0 for t in all_resolved)

        # HWM includes live PnL (P2-18 fix: was paper-only)
        all_live_resolved = list(
            session.execute(
                select(LiveTrade).where(LiveTrade.pnl_usd.isnot(None))
            ).scalars().all()
        )
        live_hwm = sum(t.pnl_usd or 0.0 for t in all_live_resolved)
        hwm = paper_hwm + live_hwm

        # ── Live trades — grouped by placed_at date (G2-2) ────────────────
        # Use placed_at (entry date) not resolved_at: resolution can lag 1-2 days
        live_resolved = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.placed_at >= day_start,
                    LiveTrade.placed_at < day_end,
                    LiveTrade.pnl_usd.isnot(None),
                )
            ).scalars().all()
        )
        live_pnl = sum(t.pnl_usd or 0.0 for t in live_resolved)
        # win/loss by sign of pnl_usd (redemption-cost), not by cash-flow direction (G2-2)
        live_wins = sum(1 for t in live_resolved if (t.pnl_usd or 0.0) > 0)
        live_losses = sum(1 for t in live_resolved if (t.pnl_usd or 0.0) < 0)
        live_staked = sum(t.stake_usd or 0.0 for t in live_resolved)
        live_roi = (live_pnl / live_staked * 100) if live_staked > 0 else 0.0
        all_reconciled = all(t.reconciled_with_polymarket for t in live_resolved)

        # By strategy for live trades
        live_by_strat: dict[str, dict] = {}
        for t in live_resolved:
            s = t.strategy_name or "unknown"
            if s not in live_by_strat:
                live_by_strat[s] = {"pnl": 0.0, "wins": 0, "losses": 0}
            live_by_strat[s]["pnl"] += t.pnl_usd or 0.0
            if (t.pnl_usd or 0.0) > 0:
                live_by_strat[s]["wins"] += 1
            else:
                live_by_strat[s]["losses"] += 1

        live_best = max(live_resolved, key=lambda t: t.pnl_usd or 0.0, default=None)
        live_worst = min(live_resolved, key=lambda t: t.pnl_usd or 0.0, default=None)

        # ── Persist daily summary ──────────────────────────────────────────
        summary_data = {
            "date": str(yesterday),
            "paper_pnl": total_pnl,
            "paper_trades": len(resolved),
            "paper_wins": wins,
            "paper_losses": losses,
            "paper_basket_misses": basket_misses,
            "live_pnl": live_pnl,
            "live_trades": len(live_resolved),
            "live_wins": live_wins,
            "live_losses": live_losses,
            "live_staked": live_staked,
            "live_roi_pct": live_roi,
            "open": len(open_trades),
            "hwm": hwm,
        }

        existing = session.get(DailySummary, yesterday)
        if existing:
            existing.total_pnl = total_pnl
            existing.trades_count = len(resolved)
            existing.wins = wins
            existing.losses = losses
            existing.basket_misses = basket_misses
            existing.open_positions = len(open_trades)
            existing.hwm = hwm
            existing.summary_json = json.dumps(summary_data)
            existing.sent_at = None
        else:
            session.add(DailySummary(
                date=yesterday,
                total_pnl=total_pnl,
                trades_count=len(resolved),
                wins=wins,
                losses=losses,
                basket_misses=basket_misses,
                open_positions=len(open_trades),
                hwm=hwm,
                summary_json=json.dumps(summary_data),
            ))
        session.commit()

        # ── Build Telegram messages (G2-2/G2-4) ───────────────────────────
        # Owner message: full details including paper
        owner_msg = f"📅 <b>Итоги дня: {yesterday}</b>\n\n"

        if live_resolved:
            live_staked_total = sum(t.stake_usd or 0.0 for t in live_resolved)
            live_received = live_staked_total + live_pnl
            live_rate = f"{live_wins / len(live_resolved) * 100:.0f}%" if live_resolved else "N/A"
            reconcile_flag = "✅" if all_reconciled else "⚠️ не сверено"
            owner_msg += (
                f"💼 <b>LIVE СДЕЛКИ</b>\n"
                f"Вложено: ${live_staked_total:.2f}   Получено: ${live_received:.2f}"
                f"   Итог: ${live_pnl:+.2f}\n"
                f"Сделок: {len(live_resolved)} | Побед: {live_wins} | Поражений: {live_losses}\n"
                f"Winrate: {live_rate} | ROI: {live_roi:+.1f}%\n"
                f"Сверено с Polymarket: {reconcile_flag}\n"
            )
            if live_by_strat:
                for strat, sv in sorted(live_by_strat.items()):
                    owner_msg += f"  {strat}: ${sv['pnl']:+.2f} ({sv['wins']}W/{sv['losses']}L)\n"
            if live_best and live_best.pnl_usd:
                owner_msg += f"🔝 {live_best.city or '?'} {live_best.bin_label}: ${live_best.pnl_usd:+.2f}\n"
            if live_worst and live_worst.pnl_usd and live_worst is not live_best:
                owner_msg += f"💸 {live_worst.city or '?'} {live_worst.bin_label}: ${live_worst.pnl_usd:+.2f}\n"
        else:
            owner_msg += "💼 <b>Живых сделок сегодня не было</b>\n"

        rate_str = f"{wins / len(resolved) * 100:.0f}%" if resolved else "N/A"
        owner_msg += f"\n📝 <b>PAPER</b> (тест, не реальные деньги)\n"
        if resolved:
            owner_msg += (
                f"PnL: ${total_pnl:+.2f} ({len(resolved)} сделок) — информационно\n"
                f"✅ {wins}W / ❌ {losses}L / 🚫 {basket_misses}BM | Winrate: {rate_str}\n"
            )
        else:
            owner_msg += "Сделок за день нет\n"

        # Client message: only if all trades reconciled with Polymarket (G2-2 gate)
        client_msg: str | None = None
        if live_resolved and all_reconciled:
            live_staked_total = sum(t.stake_usd or 0.0 for t in live_resolved)
            live_received = live_staked_total + live_pnl
            live_rate = f"{live_wins / len(live_resolved) * 100:.0f}%"
            client_msg = (
                f"📅 <b>День {yesterday}</b> (по дате открытия сделок)\n\n"
                f"💼 Вложено: ${live_staked_total:.2f}   Получено: ${live_received:.2f}\n"
                f"💰 Итог: ${live_pnl:+.2f}\n"
                f"Сделок: {len(live_resolved)} | Побед: {live_wins} | Поражений: {live_losses}\n"
                f"Сверено с Polymarket: ✅"
            )

        # Send to owner (all modes)
        sent = notifier.send(owner_msg)
        # Send to client (live mode only, reconciled only)
        from src.config.settings import settings as _s
        if client_msg and _s.trading_mode.lower() == "live":
            _send_to_clients(client_msg)

        if sent:
            if existing:
                existing.sent_at = datetime.utcnow()
            else:
                ds = session.get(DailySummary, yesterday)
                if ds:
                    ds.sent_at = datetime.utcnow()
            session.commit()

        logger.info(
            f"Daily summary {yesterday}: paper {len(resolved)} trades PnL=${total_pnl:+.2f} "
            f"({wins}W/{losses}L/{basket_misses}BM) | "
            f"live {len(live_resolved)} trades PnL=${live_pnl:+.2f} ROI={live_roi:+.1f}%"
        )
    except Exception as e:
        logger.error(f"Daily summary job failed: {e}")
    finally:
        session.close()


def _send_to_clients(msg: str) -> None:
    """Send msg to client chat IDs (G2-4). Silently skips if not configured."""
    from src.config.settings import settings
    raw = settings.client_chat_ids or ""
    chat_ids = [c.strip() for c in raw.split(",") if c.strip()]
    if not chat_ids:
        return
    try:
        import requests as _req
        token = settings.telegram_bot_token
        for cid in chat_ids:
            _req.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as exc:
        logger.warning(f"[send_to_clients] Failed: {exc}")


def _restore_trading_mode() -> None:
    """G2-4: Restore trading_mode from bot_state DB if it was changed via /mode command."""
    from src.db.models import BotState
    session = get_session()
    try:
        state = session.get(BotState, "trading_mode")
        if state and state.value in ("live", "paper"):
            from src.config.settings import settings
            if settings.trading_mode != state.value:
                settings.trading_mode = state.value
                logger.info(f"[scheduler] Restored trading_mode={state.value} from bot_state DB")
    except Exception as e:
        logger.warning(f"[scheduler] Could not restore trading_mode: {e}")
    finally:
        session.close()


def setup_scheduler() -> None:
    from src.config.settings import settings
    from src.risk.risk_manager import risk_manager

    _restore_trading_mode()

    # Check persistent emergency stop before registering live jobs
    emergency_stopped = risk_manager.is_emergency_stopped()
    if emergency_stopped:
        logger.critical(
            "⚠️ Emergency stop is ACTIVE in DB — live/tail jobs will NOT be registered. "
            "Use /reset_stop in Telegram then restart the bot to resume live trading."
        )

    schedule.every(30).minutes.do(_job_discover_markets)
    schedule.every(10).minutes.do(_job_snapshot_markets)
    schedule.every(60).minutes.do(_job_deactivate_resolved_groups)
    schedule.every(60).minutes.do(_job_fetch_open_meteo)
    schedule.every(15).minutes.do(_job_paper_trade_engine)
    if not emergency_stopped:
        schedule.every(15).minutes.do(_job_live_trade_engine)
    schedule.every(60).minutes.do(_job_resolve_paper_trades)
    schedule.every(15).minutes.do(_job_settle_paper)   # G3-3: Gamma-based paper settlement
    schedule.every(60).minutes.do(_job_paper_trade_summary)
    schedule.every(2).minutes.do(_job_reconcile_orders)
    schedule.every(15).minutes.do(_job_reconcile_pnl)
    schedule.every(30).minutes.do(_job_expire_stale_pending)
    schedule.every(30).minutes.do(_job_resolve_live_trades)
    schedule.every().sunday.at("04:00").do(_job_cleanup_snapshots)
    schedule.every().day.at("00:05").do(_job_daily_summary)
    mode = settings.trading_mode.upper()
    logger.info(
        f"Scheduler ready [{mode}]: discover=30 min | snapshots=10 min | "
        "open-meteo=60 min | deactivate=60 min | "
        "paper-trade=15 min | live-trade=15 min | resolve-paper=60 min | "
        "resolve-live=30 min | expire-pending=30 min | summary=60 min | daily=00:05 UTC"
    )


def run_initial_jobs() -> None:
    logger.info("Running initial data collection on startup...")
    _job_expire_stale_pending()
    _job_discover_markets()
    _job_fetch_open_meteo()
    _job_snapshot_markets()
    _job_resolve_paper_trades()
    _job_settle_paper()         # G3-3: Gamma paper settlement on startup
    _job_reconcile_pnl()       # G3-2: reconcile live PnL first
    _job_resolve_live_trades()
    _job_paper_trade_engine()
    _job_live_trade_engine()
    _job_paper_trade_summary()


def run_forever() -> None:
    setup_scheduler()
    run_initial_jobs()
    logger.info("Entering scheduler loop — Ctrl+C / Shift+F5 to stop")
    while True:
        schedule.run_pending()
        time.sleep(30)
