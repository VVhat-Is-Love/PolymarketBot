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


def _job_discover_markets() -> None:
    logger.info("JOB: market discovery")
    try:
        saved = discover_and_save_weather_markets(_gamma)
        if saved > 0:
            logger.info(f"Discovery added {saved} markets — triggering immediate snapshot")
            _job_snapshot_markets()
    except Exception as e:
        logger.error(f"Market discovery job failed: {e}")


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

    except Exception as exc:
        logger.error(f"[reconcile] Job failed: {exc}")
    finally:
        session.close()


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


def _job_resolve_live_trades() -> None:
    """
    For every filled-but-unresolved LiveTrade, poll Gamma prices.
    When price_yes → 0 or 1 the market has resolved — record PnL and
    send a Telegram notification. Runs every 30 min.
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
        hwm = sum(t.pnl_usd or 0.0 for t in all_resolved)

        # ── Live trades ────────────────────────────────────────────────────
        live_resolved = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.resolved_at >= day_start,
                    LiveTrade.resolved_at < day_end,
                    LiveTrade.pnl_usd.isnot(None),
                )
            ).scalars().all()
        )
        live_pnl = sum(t.pnl_usd or 0.0 for t in live_resolved)
        live_wins = sum(1 for t in live_resolved if (t.pnl_usd or 0.0) > 0)
        live_losses = len(live_resolved) - live_wins
        live_staked = sum(t.stake_usd or 0.0 for t in live_resolved)
        live_roi = (live_pnl / live_staked * 100) if live_staked > 0 else 0.0

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

        # ── Build Telegram message ─────────────────────────────────────────
        msg = f"📅 <b>Итоги дня: {yesterday}</b>\n\n"

        # LIVE section — always first and prominent
        if live_resolved:
            live_rate = f"{live_wins / len(live_resolved) * 100:.0f}%"
            msg += (
                f"💼 <b>LIVE СДЕЛКИ</b>\n"
                f"💰 PnL: ${live_pnl:+.2f} ({len(live_resolved)} сделок)\n"
                f"✅ Побед: {live_wins} | ❌ Поражений: {live_losses}\n"
                f"Winrate: {live_rate} | ROI: {live_roi:+.1f}%\n"
            )
            if live_by_strat:
                for strat, sv in sorted(live_by_strat.items()):
                    msg += f"  {strat}: ${sv['pnl']:+.2f} ({sv['wins']}W/{sv['losses']}L)\n"
            if live_best and live_best.pnl_usd:
                msg += f"🔝 {live_best.city or '?'} {live_best.bin_label}: ${live_best.pnl_usd:+.2f}\n"
            if live_worst and live_worst.pnl_usd and live_worst is not live_best:
                msg += f"💸 {live_worst.city or '?'} {live_worst.bin_label}: ${live_worst.pnl_usd:+.2f}\n"
        else:
            msg += "💼 <b>Живых сделок сегодня не было</b>\n"

        # PAPER section — always shown, clearly labelled as test/informational
        rate_str = f"{wins / len(resolved) * 100:.0f}%" if resolved else "N/A"
        msg += f"\n📝 <b>PAPER</b> (тест, не реальные деньги)\n"
        if resolved:
            msg += (
                f"PnL: ${total_pnl:+.2f} ({len(resolved)} сделок) — информационно\n"
                f"✅ {wins}W / ❌ {losses}L / 🚫 {basket_misses}BM | Winrate: {rate_str}\n"
            )
        else:
            msg += "Сделок за день нет\n"

        if notifier.send(msg):
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


def setup_scheduler() -> None:
    from src.config.settings import settings
    from src.risk.risk_manager import risk_manager

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
    schedule.every(60).minutes.do(_job_paper_trade_summary)
    schedule.every(2).minutes.do(_job_reconcile_orders)
    schedule.every(30).minutes.do(_job_expire_stale_pending)
    schedule.every(30).minutes.do(_job_resolve_live_trades)
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
