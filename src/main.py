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


def _report_live_status() -> None:
    from sqlalchemy import select
    from src.db.session import get_session
    from src.db.models import LiveTrade
    from src.strategy.live_trader import _get_wallet_balance

    logger.info("=" * 55)
    logger.info("LIVE ACCOUNT STATUS")
    logger.info("=" * 55)

    # Wallet balance
    try:
        wallet = _get_wallet_balance()
        logger.info(f"  Wallet balance : ${wallet:.2f} USDC")
    except Exception as exc:
        logger.warning(f"  Wallet balance : ERROR — {exc}")

    session = get_session()
    try:
        all_trades: list[LiveTrade] = list(
            session.execute(select(LiveTrade)).scalars().all()
        )

        open_trades = [t for t in all_trades if t.status in ("pending", "open")]
        filled = [t for t in all_trades if t.status == "filled"]
        expired = [t for t in all_trades if t.status == "expired"]
        cancelled = [t for t in all_trades if t.status == "cancelled"]

        total_pnl = sum(t.pnl_usd or 0.0 for t in all_trades)
        total_staked_filled = sum(t.stake_usd for t in filled)

        logger.info(
            f"  Open positions : {len(open_trades)} | "
            f"Filled: {len(filled)} | Expired: {len(expired)} | Cancelled: {len(cancelled)}"
        )
        logger.info(
            f"  Total staked   : ${total_staked_filled:.2f} (filled orders only)"
        )
        logger.info(f"  Total PnL      : ${total_pnl:+.2f}")

        if open_trades:
            logger.info("  --- Open positions ---")
            for t in open_trades:
                logger.info(
                    f"    {t.city or t.group_id} {t.bin_label} | "
                    f"stake=${t.stake_usd:.2f} @ ${t.target_price:.4f} | {t.status}"
                )

        if filled:
            logger.info("  --- Last 5 filled ---")
            for t in sorted(filled, key=lambda x: x.filled_at or x.placed_at, reverse=True)[:5]:
                pnl = f"PnL ${t.pnl_usd:+.2f}" if t.pnl_usd is not None else "PnL ?"
                logger.info(
                    f"    {t.city or t.group_id} {t.bin_label} | "
                    f"${t.stake_usd:.2f} @ ${t.filled_price or t.target_price:.4f} | {pnl}"
                )
    finally:
        session.close()

    logger.info("=" * 55)


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
