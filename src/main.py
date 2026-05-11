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
