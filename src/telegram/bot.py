"""
Telegram bot with command polling.
Responds only to ADMIN_TELEGRAM_ID.  Runs as a separate thread alongside the scheduler.

Commands:
  /status — balance, open positions, daily PnL
  /stop   — activate EMERGENCY_STOP
  /start  — clear EMERGENCY_STOP
  /trades — last 5 live_trades
  /risk   — current risk limits and usage
"""
from __future__ import annotations

import asyncio
import time
from loguru import logger


def _admin_only(handler):
    """Decorator: silently ignore messages from non-admin users."""
    async def wrapper(update, context):
        from src.config.settings import settings
        try:
            admin_id = int(settings.admin_telegram_id)
        except (ValueError, TypeError):
            return
        if update.effective_user and update.effective_user.id != admin_id:
            logger.warning(
                f"[telegram_bot] Unauthorised access from user_id={update.effective_user.id}"
            )
            return
        return await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_status(update, context) -> None:
    from src.risk.risk_manager import risk_manager
    s = risk_manager.get_status()
    emoji = "🛑" if s.emergency_stop else "✅"
    text = (
        f"🤖 <b>Bot Status</b>\n\n"
        f"📊 Daily PnL: ${s.daily_pnl:+.2f}\n"
        f"📉 Daily Loss: ${s.daily_loss:.2f} / ${s.daily_loss_limit_usd:.2f}\n"
        f"🔴 Open Positions: {s.concurrent_open_bets} / {s.max_concurrent_bets}\n"
        f"📅 Daily Bets: {s.daily_bets_count} / {s.max_daily_bets}\n"
        f"💥 Total Loss: ${s.total_loss:.2f} / ${s.total_stop_loss_usd:.2f}\n\n"
        f"Emergency Stop: {emoji} {s.emergency_stop_reason or 'OFF'}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


@_admin_only
async def cmd_stop(update, context) -> None:
    reason_parts = context.args
    reason = " ".join(reason_parts) if reason_parts else "Manual stop via Telegram"
    from src.risk.risk_manager import risk_manager
    risk_manager.set_emergency_stop(reason)
    await update.message.reply_text(
        f"🛑 Emergency stop activated.\nReason: {reason}\n\nBot will NOT place new orders."
    )


@_admin_only
async def cmd_start(update, context) -> None:
    from src.risk.risk_manager import risk_manager
    if not risk_manager.get_status().emergency_stop:
        await update.message.reply_text("✅ No emergency stop is active — trading is already running.")
        return
    risk_manager.clear_emergency_stop()
    await update.message.reply_text("✅ Emergency stop cleared. Trading resumed.")


@_admin_only
async def cmd_trades(update, context) -> None:
    from sqlalchemy import select
    from src.db.session import get_session
    from src.db.models import LiveTrade

    session = get_session()
    try:
        rows = list(
            session.execute(
                select(LiveTrade).order_by(LiveTrade.placed_at.desc()).limit(5)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()

    if not rows:
        await update.message.reply_text("📋 No live trades yet.")
        return

    lines = ["📋 <b>Last 5 Live Trades</b>\n"]
    status_emoji = {
        "filled": "✅", "open": "⏳", "pending": "⏳",
        "expired": "⏰", "cancelled": "❌",
    }
    for t in rows:
        em = status_emoji.get(t.status, "❓")
        price = f"${t.filled_price:.4f}" if t.filled_price else f"${t.target_price:.4f}*"
        pnl = f" PnL ${t.pnl_usd:+.2f}" if t.pnl_usd is not None else ""
        lines.append(
            f"{em} {t.city or t.group_id} {t.bin_label} | "
            f"${t.stake_usd:.2f} @ {price}{pnl}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_admin_only
async def cmd_risk(update, context) -> None:
    from src.risk.risk_manager import risk_manager
    s = risk_manager.get_status()
    text = (
        f"⚠️ <b>Risk Limits</b>\n\n"
        f"💵 Max bet per group:   ${s.max_single_bet_usd:.2f}\n"
        f"📅 Max daily bets:      {s.max_daily_bets} (used: {s.daily_bets_count})\n"
        f"🔗 Max concurrent:      {s.max_concurrent_bets} (open: {s.concurrent_open_bets})\n"
        f"📉 Daily loss limit:    ${s.daily_loss_limit_usd:.2f} (used: ${s.daily_loss:.2f})\n"
        f"💥 Total stop-loss:     ${s.total_stop_loss_usd:.2f} (used: ${s.total_loss:.2f})\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_telegram_bot() -> None:
    """
    Blocking entry point — call from a dedicated thread.
    Returns immediately (no-op) if TELEGRAM_BOT_TOKEN is not set.
    """
    from src.config.settings import settings
    token = settings.telegram_bot_token
    if not token:
        logger.warning("[telegram_bot] TELEGRAM_BOT_TOKEN not set — bot disabled")
        return

    try:
        from telegram.ext import Application, CommandHandler
    except ImportError:
        logger.error("[telegram_bot] python-telegram-bot not installed — bot disabled")
        return

    while True:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run():
            app = (
                Application.builder()
                .token(token)
                .connect_timeout(30.0)
                .read_timeout(30.0)
                .write_timeout(30.0)
                .pool_timeout(30.0)
                .build()
            )
            app.add_handler(CommandHandler("status", cmd_status))
            app.add_handler(CommandHandler("stop", cmd_stop))
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("trades", cmd_trades))
            app.add_handler(CommandHandler("risk", cmd_risk))

            async with app:
                await app.start()
                logger.info("[telegram_bot] Starting polling…")
                await app.updater.start_polling(drop_pending_updates=True)
                logger.info("[telegram_bot] Polling active — waiting for commands")
                await asyncio.Event().wait()

        try:
            loop.run_until_complete(_run())
            break  # clean exit — stop retrying
        except Exception as exc:
            logger.warning(f"[telegram_bot] Connection error: {exc} — retrying in 60s")
            try:
                loop.close()
            except Exception:
                pass
            time.sleep(60)
