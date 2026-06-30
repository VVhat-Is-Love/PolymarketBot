import sys
import threading
from pathlib import Path
from loguru import logger


def _setup_logging() -> None:
    from src.config.settings import settings

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    log_path = Path(settings.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_path,
        level="DEBUG",
        rotation="10 MB",
        retention=5,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} — {message}",
        encoding="utf-8",
    )
    logger.info(f"Logging: console={settings.log_level} | file=DEBUG → {log_path}")


def _run_migrations() -> None:
    from alembic.config import Config
    from alembic import command

    logger.info("Applying Alembic migrations...")
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    logger.info("Migrations applied successfully")


def _report_open_trades() -> None:
    from sqlalchemy import select
    from src.db.session import get_session
    from src.db.models import PaperTrade, MarketGroup

    session = get_session()
    try:
        trades = list(
            session.execute(select(PaperTrade).where(PaperTrade.status == "open"))
            .scalars().all()
        )
        if trades:
            logger.info(f"=== {len(trades)} OPEN PAPER TRADE(S) ===")
            for t in trades:
                group = session.get(MarketGroup, t.group_id)
                city = group.city if group else t.group_id
                res_date = group.resolution_date if group else "?"
                edge = f"{t.estimated_edge*100:.1f}%" if t.estimated_edge is not None else "?"
                temp = f"{t.consensus_temp:.1f}°" if t.consensus_temp is not None else "?"
                logger.info(
                    f"  {city} {res_date} | consensus={temp} edge={edge} "
                    f"stake=${t.virtual_stake_usd:.2f} | opened {t.decision_time:%Y-%m-%d %H:%M}"
                )
        else:
            logger.info("No open paper trades")
    finally:
        session.close()


def _bucket_executed(all_trades: list) -> tuple[list, list, list]:
    """
    Split executed trades into (closed, pending_redeem, open_filled).

    • closed        = sold + resolved → outcome final, realized PnL is honest.
    • pending_redeem = won, awaiting on-chain redemption → expected GAIN, never
                       booked at −cost (that is the G4-23 win-as-loss bug).
    • open_filled    = filled but market not resolved yet → still live.
    """
    closed = [t for t in all_trades if t.status in ("sold", "resolved")]
    pending = [t for t in all_trades if t.status == "pending_redeem"]
    open_filled = [t for t in all_trades if t.status == "filled"]
    return closed, pending, open_filled


def _pending_expected_gain(pending: list) -> float:
    """Expected net gain for won-unredeemed legs: shares pay $1 each at redemption,
    so gain ≈ shares − cost. Floored at 0 — a pending win is never shown negative."""
    return round(
        sum(max(0.0, (t.size_shares or 0.0) - (t.stake_usd or 0.0)) for t in pending), 2
    )


def _executed_realized_pnl(executed: list) -> tuple[float, str]:
    """
    Realized PnL for the executed trades, summed over the SAME set used for
    'staked', from /activity (the source of truth) so the figure is recomputed
    every startup and never freezes at a stale stored value.

    realized_pnl_by_token already aggregates BUY−SELL+REDEEM per token, so we sum
    once per unique token. With numerator and denominator from one set, a per-
    position realized loss can never exceed its on-chain cost. Falls back to the
    stored pnl_usd (same executed set) when /activity is unavailable.
    """
    from src.config.settings import settings
    try:
        if settings.proxy_wallet_address:
            from src.market.polymarket_data import get_activity, realized_pnl_by_token
            activity = get_activity(settings.proxy_wallet_address)
            if activity:
                pnl_by_token = realized_pnl_by_token(activity)
                total, seen = 0.0, set()
                for t in executed:
                    tok = t.token_id
                    if tok and tok not in seen:
                        seen.add(tok)
                        total += pnl_by_token.get(tok, 0.0)
                    elif not tok:
                        total += t.pnl_usd or 0.0  # legacy row w/o token → stored
                return round(total, 2), "activity"
    except Exception as exc:
        logger.debug(f"[live_status] /activity PnL unavailable ({exc}) — using stored")
    return round(sum(t.pnl_usd or 0.0 for t in executed), 2), "stored"


def _report_live_status() -> None:
    import requests as _req
    from sqlalchemy import select
    from src.db.session import get_session
    from src.db.models import LiveTrade
    from src.config.settings import settings

    sep = "=" * 58
    logger.info(sep)
    logger.info("  LIVE ACCOUNT STATUS")
    logger.info(sep)

    eoa = settings.eoa_wallet_address
    proxy = settings.proxy_wallet_address

    # ── On-chain: EOA wallet (MetaMask) ─────────────────────
    try:
        from src.blockchain.balance import get_usdc_balance, get_matic_balance
        if eoa:
            usdc_eoa = get_usdc_balance(eoa)
            logger.info(f"  ✅  USDC в кошельке (EOA):          ${usdc_eoa:.2f}")
            matic = get_matic_balance(eoa)
            if matic < 0.1:
                logger.warning(f"  ⚠️   MATIC (газ):                  {matic:.4f} — МАЛО!")
            else:
                logger.info(f"  ✅  MATIC (газ):                    {matic:.4f}")
        else:
            logger.warning("  ⚠️   EOA_WALLET_ADDRESS не задан — on-chain балансы пропущены")
    except Exception as exc:
        logger.warning(f"  ❌  On-chain балансы (EOA): {exc}")

    # ── CLOB V2 auth + balance + history ─────────────────────
    client = None
    try:
        from src.blockchain.polymarket_auth import get_clob_client
        from src.blockchain.balance import get_polymarket_cash_balance
        from py_clob_client_v2 import OpenOrderParams
        client = get_clob_client()
        orders = client.get_open_orders(OpenOrderParams())
        order_count = len(orders) if isinstance(orders, list) else "?"
        logger.info(f"  ✅  CLOB V2 авторизация | Открытых ордеров: {order_count}")

        cash = get_polymarket_cash_balance(client)
        logger.info(f"  ✅  Портфель Polymarket (pUSD):     ${cash:.2f}")

        trades_api = client.get_trades()
        trade_count = len(trades_api) if isinstance(trades_api, list) else "?"
        logger.info(f"  ✅  История трейдов (CLOB) | Всего: {trade_count}")
    except Exception as exc:
        logger.warning(f"  ❌  CLOB API: {exc}")

    # ── Polymarket open positions via data-api ────────────────
    if proxy:
        try:
            resp2 = _req.get(
                "https://data-api.polymarket.com/positions",
                params={"user": proxy},
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if resp2.status_code == 200:
                positions = resp2.json()
                logger.debug(f"[live_status] /positions raw count: {len(positions) if isinstance(positions, list) else '?'}")
                if positions:
                    logger.info(f"  ✅  Открытых позиций: {len(positions)}")
                    for p in positions[:3]:
                        title = p.get("title", "Unknown")[:38]
                        outcome = p.get("outcome", "?")
                        size = float(p.get("size", 0))
                        price = float(p.get("currentPrice", 0))
                        logger.info(f"       • {title} | {outcome} | {size:.2f} × ${price:.3f}")
                else:
                    logger.info("  ✅  Открытых позиций нет")
            else:
                logger.warning(f"  ⚠️   data-api /positions вернул {resp2.status_code}: {resp2.text[:120]}")
        except Exception as exc:
            logger.warning(f"  ⚠️   Позиции: {exc}")
    else:
        logger.warning("  ⚠️   PROXY_WALLET_ADDRESS не задан — портфель Polymarket пропущен")

    # ── Local DB trade stats ─────────────────────────────────
    session = get_session()
    try:
        all_trades: list[LiveTrade] = list(
            session.execute(select(LiveTrade)).scalars().all()
        )
        open_trades = [t for t in all_trades if t.status in ("pending", "open")]
        # G4-29b: realized PnL counts ONLY closed positions (sold + resolved).
        # Won-but-unredeemed legs (pending_redeem) are NOT booked at −cost — that
        # repeats the G4-23 win-as-loss bug (6 pending wins showed −$26). They go
        # to a separate "ждёт redeem" bucket as an expected gain. Numerator and
        # denominator of the realized line come from the SAME closed set.
        closed, pending_redeem, open_filled = _bucket_executed(all_trades)
        not_filled = [t for t in all_trades if t.status == "not_filled"]
        expired = [t for t in all_trades if t.status == "expired"]
        cancelled = [t for t in all_trades if t.status == "cancelled"]
        executed_n = len(closed) + len(pending_redeem) + len(open_filled)

        realized_pnl, pnl_source = _executed_realized_pnl(closed)
        staked_closed = sum(t.stake_usd or 0.0 for t in closed)
        pending_cost = sum(t.stake_usd or 0.0 for t in pending_redeem)
        pending_expected = _pending_expected_gain(pending_redeem)
        filled = closed + pending_redeem + open_filled  # for the listing below

        logger.info(
            f"  ✅  DB сделки: открытых={len(open_trades)} | исполнено={executed_n} | "
            f"not_filled={len(not_filled)} | истекло={len(expired)} | отменено={len(cancelled)}"
        )
        logger.info(
            f"       Реализовано (закрытые {len(closed)}): PnL ${realized_pnl:+.2f} "
            f"| поставлено ${staked_closed:.2f} (source={pnl_source})"
        )
        if pending_redeem:
            logger.info(
                f"       Ждёт redeem: {len(pending_redeem)} поз. — в рынке "
                f"${pending_cost:.2f}, ожидается ~+${pending_expected:.2f} "
                f"(НЕ вычитается из PnL)"
            )
        if open_filled:
            staked_open = sum(t.stake_usd or 0.0 for t in open_filled)
            logger.info(
                f"       Открыто (ждёт резолва): {len(open_filled)} поз. — ${staked_open:.2f}"
            )

        if open_trades:
            logger.info("  --- Открытые позиции (DB) ---")
            for t in open_trades:
                logger.info(
                    f"    {t.city or t.group_id} {t.bin_label} | "
                    f"stake=${t.stake_usd:.2f} @ ${t.target_price:.4f} | {t.status}"
                )

        if filled:
            logger.info("  --- Последние 5 исполненных ---")
            for t in sorted(filled, key=lambda x: x.filled_at or x.placed_at, reverse=True)[:5]:
                pnl_str = f"PnL ${t.pnl_usd:+.2f}" if t.pnl_usd is not None else "PnL ?"
                logger.info(
                    f"    {t.city or t.group_id} {t.bin_label} | "
                    f"${t.stake_usd:.2f} @ ${t.filled_price or t.target_price:.4f} | {pnl_str}"
                )
    finally:
        session.close()

    logger.info(sep)


def main() -> None:
    _setup_logging()

    try:
        _run_migrations()
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)

    _report_open_trades()

    from src.config.settings import settings
    logger.info(f"Trading mode: {settings.trading_mode.upper()}")

    from src.telegram.strategy_flags import log_startup_flags
    log_startup_flags()   # spells out "basket DISABLED" hard flag at startup

    if settings.trading_mode == "live":
        _report_live_status()

    # ---- Thread 1: scheduler (data collection + trading) ----
    from src.scheduler import run_forever

    scheduler_thread = threading.Thread(
        target=run_forever,
        name="SchedulerThread",
        daemon=False,
    )
    scheduler_thread.start()
    logger.info("Scheduler thread started")

    # ---- Thread 2: Telegram bot (command polling) ----
    if settings.telegram_bot_token:
        from src.telegram.bot import run_telegram_bot

        telegram_thread = threading.Thread(
            target=run_telegram_bot,
            name="TelegramThread",
            daemon=True,  # daemon so Ctrl-C kills it cleanly
        )
        telegram_thread.start()
        logger.info("Telegram bot thread started")
    else:
        logger.info("Telegram bot disabled (TELEGRAM_BOT_TOKEN not set)")

    # Wait for scheduler (main loop); Telegram thread is daemon
    try:
        scheduler_thread.join()
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()
