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
    if settings.trading_mode.lower() != "live":
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

        best = max(resolved, key=lambda t: t.pnl_usd or 0.0, default=None)
        worst = min(resolved, key=lambda t: t.pnl_usd or 0.0, default=None)

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
        rate_str = f"{wins / len(resolved) * 100:.0f}%" if resolved else "N/A"
        best_str = (
            f"🔝 {best.city or best.group_id}: ${best.pnl_usd:+.2f}"
            if best and best.pnl_usd else ""
        )
        worst_str = (
            f"💸 {worst.city or worst.group_id}: ${worst.pnl_usd:+.2f}"
            if worst and worst.pnl_usd else ""
        )

        # Paper section
        msg = (
            f"📅 <b>Итоги дня: {yesterday}</b>\n\n"
            f"📝 <b>Бумажная торговля</b> ({len(resolved)} сделок)\n"
            f"  💰 PnL: ${total_pnl:+.2f}\n"
            f"  ✅ Win: {wins} | ❌ Loss: {losses} | 🚫 Basket miss: {basket_misses}\n"
            f"  Winrate: {rate_str}\n"
            f"  📈 HWM: ${hwm:+.2f}\n"
            f"  💼 Открытых: {len(open_trades)}\n"
        )
        if best_str:
            msg += f"  {best_str}\n"
        if worst_str:
            msg += f"  {worst_str}\n"

        # Live section
        if live_resolved:
            live_rate = f"{live_wins / len(live_resolved) * 100:.0f}%" if live_resolved else "N/A"
            msg += (
                f"\n💸 <b>Live торговля</b> ({len(live_resolved)} сделок)\n"
                f"  💰 PnL: ${live_pnl:+.2f} | ROI: {live_roi:+.1f}%\n"
                f"  ✅ Win: {live_wins} | ❌ Loss: {live_losses} | Winrate: {live_rate}\n"
                f"  📦 Поставлено: ${live_staked:.2f}\n"
            )
            if live_by_strat:
                msg += "  По стратегиям:\n"
                for strat, sv in sorted(live_by_strat.items()):
                    msg += f"    {strat}: ${sv['pnl']:+.2f} ({sv['wins']}W/{sv['losses']}L)\n"
            if live_best and live_best.pnl_usd:
                msg += f"  🔝 {live_best.city or '?'} {live_best.bin_label}: ${live_best.pnl_usd:+.2f}\n"
            if live_worst and live_worst.pnl_usd and live_worst is not live_best:
                msg += f"  💸 {live_worst.city or '?'} {live_worst.bin_label}: ${live_worst.pnl_usd:+.2f}\n"
        else:
            msg += "\n💸 <b>Live торговля</b>: нет закрытых сделок за день\n"

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
    schedule.every(30).minutes.do(_job_discover_markets)
    schedule.every(10).minutes.do(_job_snapshot_markets)
    schedule.every(60).minutes.do(_job_deactivate_resolved_groups)
    schedule.every(60).minutes.do(_job_fetch_open_meteo)
    schedule.every(15).minutes.do(_job_paper_trade_engine)
    schedule.every(15).minutes.do(_job_live_trade_engine)
    schedule.every(60).minutes.do(_job_resolve_paper_trades)
    schedule.every(60).minutes.do(_job_paper_trade_summary)
    schedule.every().day.at("00:05").do(_job_daily_summary)
    mode = settings.trading_mode.upper()
    logger.info(
        f"Scheduler ready [{mode}]: discover=30 min | snapshots=10 min | "
        "open-meteo=60 min | deactivate=60 min | "
        "paper-trade=15 min | live-trade=15 min | resolve=60 min | "
        "summary=60 min | daily=00:05 UTC"
    )


def run_initial_jobs() -> None:
    logger.info("Running initial data collection on startup...")
    _job_discover_markets()
    _job_fetch_open_meteo()
    _job_snapshot_markets()
    _job_resolve_paper_trades()
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
