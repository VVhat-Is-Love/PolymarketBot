"""
Telegram bot — extended command set for the Polymarket trading bot.
Responds only to user IDs listed in ADMIN_TELEGRAM_ID (comma-separated).
Runs as a separate thread alongside the scheduler.

Commands:
  /status           — full dashboard: balance, PnL, positions, risk
  /pnl [period]     — PnL breakdown (today|week|month|all)
  /trades [filter]  — list trades (open|pending|filled|all)
  /trade <id>       — detailed trade card by ID prefix
  /setlimit <t> <v> — update risk limit with confirmation (daily|bet|total)
  /setstake <v>     — update MAX_SINGLE_BET_USD with confirmation
  /strategy [list|on|off] [name] — view/toggle strategies
  /cancel <id>      — cancel an open order with confirmation
  /stop [reason]    — activate emergency stop
  /start            — clear emergency stop
  /risk             — risk limits overview
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from loguru import logger


# ---------------------------------------------------------------------------
# Admin access control
# ---------------------------------------------------------------------------

def _get_admin_ids() -> set[int]:
    """Parse ADMIN_TELEGRAM_ID (comma-separated) into a set of ints."""
    from src.config.settings import settings
    raw = settings.admin_telegram_id or ""
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                logger.warning(f"[telegram_bot] Invalid admin ID: {part!r}")
    return ids


def _is_admin(user_id: int) -> bool:
    return user_id in _get_admin_ids()


def _admin_only(handler):
    """Decorator: silently ignore messages from non-admin users."""
    async def wrapper(update, context):
        admin_ids = _get_admin_ids()
        if not admin_ids:
            logger.warning("[telegram_bot] No admin IDs configured — rejecting all commands")
            return
        if update.effective_user and update.effective_user.id not in admin_ids:
            logger.warning(
                f"[telegram_bot] Unauthorised from user_id={update.effective_user.id}"
            )
            return
        return await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------

async def _reply(update, text: str, parse_mode: str = "HTML", **kwargs) -> None:
    """Reply to a command message, splitting into chunks if > 4000 chars."""
    MAX = 4000
    if len(text) <= MAX:
        await update.message.reply_text(text, parse_mode=parse_mode, **kwargs)
        return
    # Split on newlines to avoid breaking mid-line
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > MAX:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode=parse_mode)


# ---------------------------------------------------------------------------
# /status — full dashboard
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_status(update, context) -> None:
    try:
        from src.config.settings import settings as cfg
        from src.risk.risk_manager import risk_manager
        from src.strategy.live_trader import _get_wallet_balance
        from src.telegram.stats import pnl_stats, summary_counts, open_positions_value

        wallet = _get_wallet_balance()
        portfolio = open_positions_value()
        s = risk_manager.get_status()
        pnl_today = pnl_stats("today")
        pnl_all = pnl_stats("all")
        counts = summary_counts()

        mode = cfg.trading_mode.upper()
        emoji_stop = "🛑" if s.emergency_stop else "✅"
        loss_pct = (
            f"{s.daily_loss / s.daily_loss_limit_usd * 100:.0f}%"
            if s.daily_loss_limit_usd > 0 else "N/A"
        )

        text = (
            f"🤖 <b>Bot Status — {mode} режим</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Баланс: ${wallet:.2f} (USDC)\n"
            f"📊 Портфель: ${portfolio:.2f}\n\n"
            f"📈 PnL сегодня:  ${pnl_today['pnl']:+.2f}  ({pnl_today['count']} сделок)\n"
            f"📦 PnL всего:    ${pnl_all['pnl']:+.2f}\n"
            f"🏔 High Water Mark: ${counts['hwm']:+.2f}\n\n"
            f"🔴 Открытых позиций: {counts['open']}\n"
            f"⏳ Ожидают исхода:   {counts['awaiting']}\n"
            f"✅ Исполнено сегодня: {counts['filled_today']}\n\n"
            f"📉 Дневной лимит потерь: ${s.daily_loss:.2f} / ${s.daily_loss_limit_usd:.2f} ({loss_pct})\n"
            f"🎯 Winrate (всего): {counts['winrate']:.0f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Emergency Stop: {emoji_stop} {s.emergency_stop_reason or 'OFF'}"
        )
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as exc:
        logger.error(f"[telegram_bot] /status error: {exc}")
        await update.message.reply_text(f"❌ Ошибка: {str(exc)[:300]}")


# ---------------------------------------------------------------------------
# /pnl [period]
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_pnl(update, context) -> None:
    try:
        period = (context.args[0] if context.args else "today").lower()
        if period not in ("today", "week", "month", "all"):
            await update.message.reply_text("❓ Используй: /pnl [today|week|month|all]")
            return

        from src.telegram.stats import pnl_stats
        st = pnl_stats(period)

        total = st["wins"] + st["losses"]
        wr = f"{st['wins'] / total * 100:.0f}%" if total > 0 else "N/A"

        lines = [
            f"📊 <b>PnL за {st['label']}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 Итого: ${st['pnl']:+.2f}",
            f"✅ Побед: {st['wins']} | ❌ Поражений: {st['losses']} | Winrate: {wr}",
            f"📦 Поставлено: ${st['total_staked']:.2f} | ROI: {st['roi_pct']:+.1f}%",
        ]

        if st["by_strategy"]:
            lines.append("\n<b>По стратегиям:</b>")
            for strat, sv in sorted(st["by_strategy"].items()):
                lines.append(
                    f"  {strat}: ${sv['pnl']:+.2f}"
                    f"  ({sv['wins']}W/{sv['losses']}L)"
                )

        if st["by_day"]:
            lines.append("\n<b>По дням:</b>")
            for day in st["by_day"][-10:]:  # last 10 days
                wr_day = f"{day['wins']}W/{day['losses']}L"
                lines.append(f"  {day['date']}: ${day['pnl']:+.2f}  ({wr_day})")

        await _reply(update, "\n".join(lines))
    except Exception as exc:
        logger.error(f"[telegram_bot] /pnl error: {exc}")
        await update.message.reply_text(f"❌ Ошибка: {str(exc)[:300]}")


# ---------------------------------------------------------------------------
# /trades [filter]
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_trades(update, context) -> None:
    try:
        filter_status = (context.args[0] if context.args else "open").lower()
        if filter_status not in ("open", "pending", "filled", "cancelled", "all"):
            await update.message.reply_text(
                "❓ Используй: /trades [open|pending|filled|cancelled|all]"
            )
            return

        from src.telegram.stats import get_trades, get_current_price

        trades = get_trades(filter_status, limit=10)
        labels = {
            "open":      "Открытые позиции",
            "pending":   "Ожидают исполнения",
            "filled":    "Исполненные",
            "cancelled": "Отменённые / истёкшие",
            "all":       "Все сделки",
        }
        status_emoji = {
            "filled": "✅", "open": "⏳", "pending": "🕐",
            "expired": "⏰", "cancelled": "❌",
        }

        if not trades:
            await update.message.reply_text(f"📋 {labels[filter_status]}: нет данных")
            return

        lines = [
            f"📋 <b>{labels[filter_status]} ({len(trades)})</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        for t in trades:
            em = status_emoji.get(t.status, "❓")
            short_id = (t.id or "")[:8]
            entry_price = t.filled_price or t.target_price or 0.0
            placed = t.placed_at.strftime("%d.%m %H:%M") if t.placed_at else "—"

            pnl_str = ""
            if t.pnl_usd is not None:
                pct = (t.pnl_usd / t.stake_usd * 100) if t.stake_usd else 0.0
                pnl_str = f"  PnL: ${t.pnl_usd:+.2f} ({pct:+.1f}%)\n"
            elif t.status in ("open", "filled") and t.resolved_at is None:
                cur = get_current_price(t.market_id)
                if cur is not None and entry_price > 0 and t.size_shares:
                    unreal = (cur - entry_price) * t.size_shares
                    unreal_pct = (cur - entry_price) / entry_price * 100
                    pnl_str = f"  PnL: ${unreal:+.2f} ({unreal_pct:+.1f}%) нереал.\n"

            block = (
                f"[{short_id}] {em} {t.city or t.group_id or '?'} | {t.bin_label}\n"
                f"  ${t.stake_usd or 0:.2f} @ ${entry_price:.4f} | {t.strategy_name}\n"
                f"{pnl_str}"
                f"  Открыта: {placed}"
            )
            lines.append(block)

        await _reply(update, "\n".join(lines))
    except Exception as exc:
        logger.error(f"[telegram_bot] /trades error: {exc}")
        await update.message.reply_text(f"❌ Ошибка: {str(exc)[:300]}")


# ---------------------------------------------------------------------------
# /trade <id>  — detailed card
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_trade(update, context) -> None:
    try:
        if not context.args:
            await update.message.reply_text("❓ Используй: /trade <id>")
            return

        prefix = context.args[0]
        from src.telegram.stats import get_trade_by_prefix, get_current_price

        trade = get_trade_by_prefix(prefix)
        if not trade:
            await update.message.reply_text(f"❌ Сделка '{prefix}' не найдена")
            return

        entry_price = trade.filled_price or trade.target_price or 0.0
        cur_price = get_current_price(trade.market_id)

        pnl_str = "N/A"
        if trade.pnl_usd is not None:
            pct = (trade.pnl_usd / trade.stake_usd * 100) if trade.stake_usd else 0.0
            pnl_str = f"${trade.pnl_usd:+.2f} ({pct:+.1f}%)"
        elif cur_price is not None and entry_price > 0 and trade.size_shares:
            unreal = (cur_price - entry_price) * trade.size_shares
            unreal_pct = (cur_price - entry_price) / entry_price * 100
            pnl_str = f"${unreal:+.2f} ({unreal_pct:+.1f}%) нереал."

        status_emoji = {
            "filled": "✅", "open": "⏳", "pending": "🕐",
            "expired": "⏰", "cancelled": "❌",
        }
        em = status_emoji.get(trade.status, "❓")
        placed = trade.placed_at.strftime("%d.%m.%Y %H:%M:%S UTC") if trade.placed_at else "—"
        order_short = (
            (trade.order_id[:16] + "…") if trade.order_id and len(trade.order_id) > 16
            else (trade.order_id or "нет")
        )

        text = (
            f"🔍 <b>Сделка #{(trade.id or '')[:8]}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Рынок: {trade.city or trade.group_id or '?'}\n"
            f"Бин: {trade.bin_label} | {trade.side.upper()}\n"
            f"Стратегия: {trade.strategy_name}\n\n"
            f"💵 Ставка: ${trade.stake_usd or 0:.2f}\n"
            f"📥 Цена входа: ${entry_price:.4f}\n"
        )
        if cur_price is not None:
            text += f"📊 Текущая цена: ${cur_price:.4f}\n"
        text += (
            f"💰 PnL: {pnl_str}\n\n"
            f"📅 Открыта: {placed}\n"
            f"🔗 Order ID: {order_short}\n\n"
            f"Статус: {em} {trade.status.upper()}"
        )
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as exc:
        logger.error(f"[telegram_bot] /trade error: {exc}")
        await update.message.reply_text(f"❌ Ошибка: {str(exc)[:300]}")


# ---------------------------------------------------------------------------
# /setlimit <type> <value>  — with inline confirmation
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_setlimit(update, context) -> None:
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "❓ Используй: /setlimit [daily|bet|total] <value>\n"
                "  daily — дневной лимит потерь\n"
                "  bet   — макс. размер ставки\n"
                "  total — общий стоп-лосс"
            )
            return

        limit_type = context.args[0].lower()
        if limit_type not in ("daily", "bet", "total"):
            await update.message.reply_text("❌ Тип: daily, bet или total")
            return

        try:
            new_val = float(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ Значение должно быть числом")
            return

        from src.risk.risk_manager import risk_manager
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        s = risk_manager.get_status()
        old_map = {
            "daily": (s.daily_loss_limit_usd, "Дневной лимит потерь"),
            "bet":   (s.max_single_bet_usd, "Макс. ставка"),
            "total": (s.total_stop_loss_usd, "Общий стоп-лосс"),
        }
        old_val, label = old_map[limit_type]

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"sl:{limit_type}:{new_val}"),
            InlineKeyboardButton("❌ Отмена", callback_data="x"),
        ]])
        await update.message.reply_text(
            f"⚠️ Изменить <b>{label}</b>: ${old_val:.2f} → ${new_val:.2f}?",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as exc:
        logger.error(f"[telegram_bot] /setlimit error: {exc}")
        await update.message.reply_text(f"❌ Ошибка: {str(exc)[:300]}")


# ---------------------------------------------------------------------------
# /setstake <value>  — with inline confirmation
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_setstake(update, context) -> None:
    try:
        if not context.args:
            await update.message.reply_text("❓ Используй: /setstake <value>")
            return

        try:
            new_val = float(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Значение должно быть числом")
            return

        from src.risk.risk_manager import risk_manager
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        old_val = risk_manager.get_status().max_single_bet_usd
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"ss:{new_val}"),
            InlineKeyboardButton("❌ Отмена", callback_data="x"),
        ]])
        await update.message.reply_text(
            f"⚠️ Изменить <b>MAX_SINGLE_BET_USD</b>: ${old_val:.2f} → ${new_val:.2f}?",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as exc:
        logger.error(f"[telegram_bot] /setstake error: {exc}")
        await update.message.reply_text(f"❌ Ошибка: {str(exc)[:300]}")


# ---------------------------------------------------------------------------
# /strategy [list|on|off] [name]
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_strategy(update, context) -> None:
    try:
        from src.telegram.strategy_flags import KNOWN_STRATEGIES, get_all, set_enabled
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        sub = (context.args[0] if context.args else "list").lower()

        # /strategy  or  /strategy list
        if sub == "list":
            flags = get_all()
            lines = ["📋 <b>Стратегии</b>", "━━━━━━━━━━━━"]
            for name in KNOWN_STRATEGIES:
                state = "✅ включена" if flags.get(name, True) else "❌ выключена"
                lines.append(f"{state} — {name}")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        if sub in ("on", "off"):
            if len(context.args) < 2:
                await update.message.reply_text(f"❓ Используй: /strategy {sub} <name>")
                return

            name = context.args[1].lower()
            if name not in KNOWN_STRATEGIES:
                await update.message.reply_text(
                    f"❌ Неизвестная стратегия: {name}\n"
                    f"Доступные: {', '.join(KNOWN_STRATEGIES)}"
                )
                return

            short_map = {"basket_wide": "bw", "basket_narrow": "bn", "tail_no": "tn"}
            short = short_map[name]
            val = "1" if sub == "on" else "0"
            action_label = "ВКЛЮЧИТЬ" if sub == "on" else "ВЫКЛЮЧИТЬ"

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"sg:{short}:{val}"),
                InlineKeyboardButton("❌ Отмена", callback_data="x"),
            ]])
            await update.message.reply_text(
                f"⚠️ {action_label} стратегию <b>{name}</b>?",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return

        await update.message.reply_text(
            "❓ Используй:\n"
            "  /strategy list\n"
            "  /strategy on  <basket_wide|basket_narrow|tail_no>\n"
            "  /strategy off <basket_wide|basket_narrow|tail_no>"
        )
    except Exception as exc:
        logger.error(f"[telegram_bot] /strategy error: {exc}")
        await update.message.reply_text(f"❌ Ошибка: {str(exc)[:300]}")


# ---------------------------------------------------------------------------
# /cancel <trade_id>  — with inline confirmation
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_cancel_trade(update, context) -> None:
    try:
        if not context.args:
            await update.message.reply_text("❓ Используй: /cancel <trade_id>")
            return

        prefix = context.args[0]
        from src.telegram.stats import get_trade_by_prefix
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        trade = get_trade_by_prefix(prefix)
        if not trade:
            await update.message.reply_text(f"❌ Сделка '{prefix}' не найдена")
            return

        if trade.status not in ("pending", "open"):
            await update.message.reply_text(
                f"❌ Нельзя отменить сделку со статусом '{trade.status}'"
            )
            return

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отменить ордер", callback_data=f"tc:{trade.id}"),
            InlineKeyboardButton("❌ Нет", callback_data="x"),
        ]])
        await update.message.reply_text(
            f"⚠️ Отменить ордер <b>{(trade.id or '')[:8]}</b>?\n"
            f"{trade.city or '?'} | {trade.bin_label} | ${trade.stake_usd or 0:.2f} "
            f"| {trade.strategy_name}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as exc:
        logger.error(f"[telegram_bot] /cancel error: {exc}")
        await update.message.reply_text(f"❌ Ошибка: {str(exc)[:300]}")


# ---------------------------------------------------------------------------
# /stop, /start, /risk  (kept from original)
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_stop(update, context) -> None:
    reason = " ".join(context.args) if context.args else "Manual stop via Telegram"
    from src.risk.risk_manager import risk_manager
    risk_manager.set_emergency_stop(reason)
    await update.message.reply_text(
        f"🛑 Emergency stop активирован.\nПричина: {reason}\n\nБот НЕ будет размещать новые ордера."
    )


@_admin_only
async def cmd_start(update, context) -> None:
    from src.risk.risk_manager import risk_manager
    if not risk_manager.get_status().emergency_stop:
        await update.message.reply_text("✅ Emergency stop не активен — торговля уже запущена.")
        return
    risk_manager.clear_emergency_stop()
    await update.message.reply_text("✅ Emergency stop снят. Торговля возобновлена.")


@_admin_only
async def cmd_risk(update, context) -> None:
    from src.risk.risk_manager import risk_manager
    s = risk_manager.get_status()
    text = (
        f"⚠️ <b>Risk Limits</b>\n\n"
        f"💵 Макс. ставка:       ${s.max_single_bet_usd:.2f}  (/setstake)\n"
        f"📅 Макс. ставок/день:  {s.max_daily_bets}  (использовано: {s.daily_bets_count})\n"
        f"🔗 Макс. одновременно: {s.max_concurrent_bets}  (открыто: {s.concurrent_open_bets})\n"
        f"📉 Дневной лимит:      ${s.daily_loss_limit_usd:.2f}  (исп.: ${s.daily_loss:.2f})  (/setlimit daily)\n"
        f"💥 Общий стоп-лосс:    ${s.total_stop_loss_usd:.2f}  (исп.: ${s.total_loss:.2f})  (/setlimit total)\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Inline-button callback router
# ---------------------------------------------------------------------------

async def _callback_router(update, context) -> None:
    query = update.callback_query

    # Admin check for callbacks
    if query.from_user and not _is_admin(query.from_user.id):
        await query.answer("Unauthorized", show_alert=True)
        return

    await query.answer()
    data = query.data or ""

    try:
        # ── Dismiss ──────────────────────────────────────────────────────
        if data == "x":
            await query.edit_message_text("❌ Отменено")
            return

        # ── setlimit: sl:{type}:{value} ──────────────────────────────────
        if data.startswith("sl:"):
            parts = data.split(":")
            limit_type = parts[1]
            new_val = float(parts[2])

            from src.risk.risk_manager import risk_manager
            if limit_type == "daily":
                risk_manager.set_daily_loss_limit(new_val)
                label = "Дневной лимит потерь"
            elif limit_type == "bet":
                risk_manager.set_max_single_bet(new_val)
                label = "Макс. ставка"
            else:  # total
                risk_manager.set_total_stop_loss(new_val)
                label = "Общий стоп-лосс"
            logger.info(f"[telegram_bot] setlimit {limit_type}→${new_val:.2f} via callback")
            await query.edit_message_text(f"✅ <b>{label}</b> обновлён: ${new_val:.2f}", parse_mode="HTML")
            return

        # ── setstake: ss:{value} ─────────────────────────────────────────
        if data.startswith("ss:"):
            new_val = float(data.split(":", 1)[1])
            from src.risk.risk_manager import risk_manager
            risk_manager.set_max_single_bet(new_val)
            logger.info(f"[telegram_bot] setstake→${new_val:.2f} via callback")
            await query.edit_message_text(f"✅ MAX_SINGLE_BET_USD обновлён: ${new_val:.2f}")
            return

        # ── strategy toggle: sg:{short}:{0|1} ────────────────────────────
        if data.startswith("sg:"):
            parts = data.split(":")
            short = parts[1]
            enabled = parts[2] == "1"
            short_map = {"bw": "basket_wide", "bn": "basket_narrow", "tn": "tail_no"}
            full_name = short_map.get(short, short)

            from src.telegram.strategy_flags import set_enabled
            set_enabled(full_name, enabled)
            state = "включена ✅" if enabled else "выключена ❌"
            logger.info(f"[telegram_bot] strategy {full_name} {'on' if enabled else 'off'} via callback")
            await query.edit_message_text(
                f"Стратегия <b>{full_name}</b> {state}", parse_mode="HTML"
            )
            return

        # ── trade cancel: tc:{trade_id} ──────────────────────────────────
        if data.startswith("tc:"):
            trade_id = data.split(":", 1)[1]

            from sqlalchemy import select
            from src.db.session import get_session
            from src.db.models import LiveTrade
            from src.market.order_executor import cancel_order

            session = get_session()
            try:
                trade = session.get(LiveTrade, trade_id)
                if not trade:
                    await query.edit_message_text("❌ Сделка не найдена")
                    return

                clob_ok = False
                if trade.order_id:
                    clob_ok = cancel_order(trade.order_id)

                trade.status = "cancelled"
                trade.cancelled_at = datetime.utcnow()
                session.commit()

                msg = "✅ Ордер отменён" if clob_ok else "⚠️ Статус → cancelled (CLOB не подтвердил)"
                logger.info(
                    f"[telegram_bot] cancel trade {trade_id[:8]} clob_ok={clob_ok}"
                )
                await query.edit_message_text(f"{msg}: #{trade_id[:8]}")
            finally:
                session.close()
            return

        await query.edit_message_text("❓ Неизвестное действие")

    except Exception as exc:
        logger.error(f"[telegram_bot] callback error: {exc}")
        try:
            await query.edit_message_text(f"❌ Ошибка: {str(exc)[:300]}")
        except Exception:
            pass


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
        from telegram.ext import Application, CommandHandler, CallbackQueryHandler
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

            # Query commands
            app.add_handler(CommandHandler("status",   cmd_status))
            app.add_handler(CommandHandler("pnl",      cmd_pnl))
            app.add_handler(CommandHandler("trades",   cmd_trades))
            app.add_handler(CommandHandler("trade",    cmd_trade))
            app.add_handler(CommandHandler("risk",     cmd_risk))

            # Control commands
            app.add_handler(CommandHandler("stop",     cmd_stop))
            app.add_handler(CommandHandler("start",    cmd_start))
            app.add_handler(CommandHandler("setlimit", cmd_setlimit))
            app.add_handler(CommandHandler("setstake", cmd_setstake))
            app.add_handler(CommandHandler("strategy", cmd_strategy))
            app.add_handler(CommandHandler("cancel",   cmd_cancel_trade))

            # Inline-button callbacks
            app.add_handler(CallbackQueryHandler(_callback_router))

            async with app:
                await app.start()
                logger.info("[telegram_bot] Starting polling…")
                await app.updater.start_polling(drop_pending_updates=True)
                logger.info("[telegram_bot] Polling active — commands: "
                            "status, pnl, trades, trade, risk, stop, start, "
                            "setlimit, setstake, strategy, cancel")
                await asyncio.Event().wait()

        try:
            loop.run_until_complete(_run())
            break
        except Exception as exc:
            logger.warning(f"[telegram_bot] Connection error: {exc} — retrying in 60s")
            try:
                loop.close()
            except Exception:
                pass
            time.sleep(60)
