"""
Telegram bot — extended command set for the Polymarket trading bot.
Responds only to user IDs listed in ADMIN_TELEGRAM_ID (comma-separated).
Runs as a separate thread alongside the scheduler.

Commands:
  /status              — full dashboard: balance, PnL, positions, risk
  /pnl [period]        — PnL breakdown (today|week|month|all)
  /trades [filter]     — list positions (open|all|history|pending)
  /trade <id>          — detailed trade card by ID prefix
  /risk                — risk limits overview
  /paper               — paper trading status, open trades, strategy info
  /help                — list all commands
  /setlimit <t> <v>    — update risk limit with confirmation (daily|bet|total)
  /setstake <v>        — update MAX_SINGLE_BET_USD with confirmation
  /strategy [list|on|off] [name] — view/toggle strategies
  /cancel <id>         — cancel an open order with confirmation
  /gamma               — Gamma API circuit-breaker status + live probe
  /gamma_reset         — force-reset Gamma circuit breaker (after outage)
  /stop [reason]       — activate emergency stop (persisted to DB)
  /reset_stop          — clear emergency stop with two-stage confirmation
  /reset_paper_stop    — reset virtual paper stop (voids loss accounting)
  /mode [paper|live]   — switch trading mode
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, date
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
        from src.telegram.stats import pnl_stats, summary_counts, get_portfolio_breakdown

        portfolio = get_portfolio_breakdown()   # cash + positions from Polymarket API
        s = risk_manager.get_status()           # live DB-backed risk counters
        pnl_today = pnl_stats("today")
        pnl_all = pnl_stats("all")
        counts = summary_counts()

        mode = cfg.trading_mode.upper()
        emoji_stop = "🛑" if s.emergency_stop else "✅"
        loss_pct = (
            f"{s.daily_loss / s.daily_loss_limit_usd * 100:.0f}%"
            if s.daily_loss_limit_usd > 0 else "N/A"
        )

        pos_pnl_sign = "+" if portfolio["positions_pnl"] >= 0 else ""
        text = (
            f"🤖 <b>Bot Status — {mode} режим</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏦 Polymarket (своб.):  ${portfolio['cash_balance']:.2f}\n"
            f"📦 Позиций: {portfolio['positions_count']}  (${portfolio['positions_value']:.2f}  PnL: {pos_pnl_sign}${portfolio['positions_pnl']:.2f})\n"
            f"💼 Кошелёк итого:        ${portfolio['total']:.2f}\n\n"
            f"📈 PnL сегодня:  ${pnl_today['pnl']:+.2f}  ({pnl_today['count']} сделок)\n"
            f"📦 PnL всего:    ${pnl_all['pnl']:+.2f}\n"
            f"🏔 High Water Mark: ${counts['hwm']:+.2f}\n\n"
            f"🔴 Открытых/pending: {counts['open']}\n"
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
# /trades helpers
# ---------------------------------------------------------------------------

_CITY_EMOJI: dict[str, str] = {
    "london": "🇬🇧", "tokyo": "🇯🇵", "seoul": "🇰🇷",
    "atlanta": "🇺🇸", "dallas": "🇺🇸", "miami": "🇺🇸",
    "new york": "🇺🇸", "chicago": "🇺🇸", "los angeles": "🇺🇸",
    "moscow": "🇷🇺", "paris": "🇫🇷", "berlin": "🇩🇪",
    "beijing": "🇨🇳", "shanghai": "🇨🇳", "sydney": "🇦🇺",
    "dubai": "🇦🇪", "singapore": "🇸🇬", "toronto": "🇨🇦",
}


def _city_emoji(title: str) -> str:
    t = title.lower()
    for city, em in _CITY_EMOJI.items():
        if city in t:
            return em
    return "🌍"


def _pnl_emoji(pnl: float, pnl_pct: float) -> str:
    if pnl > 0:
        return "✅"
    if pnl_pct <= -90:
        return "💀"
    if pnl_pct <= -50:
        return "⚠️"
    if pnl_pct <= -20:
        return "❌"
    return ""


def _classify_position(p: dict) -> str:
    """Classify a position dict as 'won', 'lost', or 'active'."""
    if p.get("redeemable") or p["cur_price"] >= 0.99:
        return "won"
    if p["cur_price"] <= 0.01:
        return "lost"
    return "active"


def _format_position_line(p: dict) -> str:
    em = _city_emoji(p["title"])
    avg_c = p["avg_price"] * 100
    cur_c = p["cur_price"] * 100       # cur_price — correct API field name
    pnl = p["pnl"]
    pnl_pct = p["pnl_pct"]
    pnl_em = _pnl_emoji(pnl, pnl_pct)
    redeemable_tag = " 💰 забрать!" if p.get("redeemable") else ""
    sign = "+" if pnl >= 0 else ""
    pct_sign = "+" if pnl_pct >= 0 else ""
    return (
        f"\n{em} {p['title']}{redeemable_tag}\n"
        f"{p['outcome']} | {p['size']:.1f} долей | {avg_c:.1f}¢→{cur_c:.1f}¢\n"
        f"Вложено: ${p['cost']:.2f} → Сейчас: ${p['current_value']:.2f}\n"
        f"PnL: {sign}${pnl:.2f} ({pct_sign}{pnl_pct:.1f}%) {pnl_em}"
    )


def _positions_summary(positions: list[dict]) -> str:
    total_cost = sum(p["cost"] for p in positions)
    total_value = sum(p["current_value"] for p in positions)
    total_pnl = sum(p["pnl"] for p in positions)
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
    tot_sign = "+" if total_pnl >= 0 else ""
    tot_pct_sign = "+" if total_pnl_pct >= 0 else ""
    return (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Вложено: ${total_cost:.2f}\n"
        f"📈 Текущая стоимость: ${total_value:.2f}\n"
        f"🎰 Итого PnL: {tot_sign}${total_pnl:.2f} ({tot_pct_sign}{total_pnl_pct:.1f}%)"
    )


# ---------------------------------------------------------------------------
# /trades [filter]
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_trades(update, context) -> None:
    try:
        filter_status = (context.args[0] if context.args else "open").lower()
        if filter_status not in ("open", "all", "history", "pending", "filled", "cancelled"):
            await update.message.reply_text(
                "❓ Используй: /trades [open|all|history|pending]"
            )
            return

        lines: list[str] = []

        # ── open (default): active + won (redeemable) from API + pending DB ──
        if filter_status in ("open", "pending"):
            if filter_status == "open":
                from src.telegram.stats import get_real_polymarket_positions
                all_positions = get_real_polymarket_positions()
                # Show active + redeemable (won but not yet collected); hide lost
                positions = [p for p in all_positions if _classify_position(p) != "lost"]

                MAX_SHOW = 10
                shown = positions[:MAX_SHOW]
                rest = len(positions) - MAX_SHOW

                if shown:
                    lines.append(
                        f"📊 <b>Позиции на Polymarket ({len(positions)} активных)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    for p in shown:
                        lines.append(_format_position_line(p))
                    if rest > 0:
                        lines.append(f"\n…и ещё {rest} позиций | /trades all")
                    lines.append(_positions_summary(shown))
                else:
                    lines.append("📊 <b>Позиции на Polymarket</b>\nНет активных позиций")

            # ── Pending limit orders from local DB ──────────────────────────
            from src.telegram.stats import get_trades, get_resolution_dates
            pending_trades = get_trades("pending", limit=20)
            if pending_trades:
                group_ids = [t.group_id for t in pending_trades if t.group_id]
                res_dates = get_resolution_dates(group_ids)
                lines.append(
                    f"\n⏳ <b>Ожидают исполнения (лимитки, из ДБ) ({len(pending_trades)})</b>"
                )
                for t in pending_trades:
                    short_id = (t.id or "")[:8]
                    entry_price = t.target_price or 0.0
                    res_date = res_dates.get(t.group_id or "")
                    res_str = res_date.strftime("%d.%m") if res_date else "?"
                    city = t.city or (t.group_id or "")[:8] or "?"
                    lines.append(
                        f"[{short_id}] {city} {t.bin_label} → ${t.stake_usd or 0:.2f} @ ${entry_price:.4f}"
                    )
                lines.append("(не исполнены, рынок мог закрыться)")
            elif filter_status == "pending":
                lines.append("⏳ <b>Ожидают исполнения</b>\nНет ожидающих ордеров")

        # ── all: 3-way split — won / active / lost ──────────────────────────
        elif filter_status == "all":
            from src.telegram.stats import get_real_polymarket_positions
            all_positions = get_real_polymarket_positions()

            won = [p for p in all_positions if _classify_position(p) == "won"]
            active = [p for p in all_positions if _classify_position(p) == "active"]
            lost = [p for p in all_positions if _classify_position(p) == "lost"]

            lines.append(f"📊 <b>Все позиции на Polymarket</b>\n━━━━━━━━━━━━━━━━━━━━━━━━")

            if won:
                lines.append(f"\n✅ <b>Выигравшие ({len(won)}) — можно забрать</b>")
                for p in won:
                    lines.append(_format_position_line(p))
                lines.append(_positions_summary(won))

            if active:
                lines.append(f"\n🟢 <b>Активные ({len(active)})</b>")
                for p in active:
                    lines.append(_format_position_line(p))
                lines.append(_positions_summary(active))

            if lost:
                lines.append(f"\n❌ <b>Проигравшие ({len(lost)})</b>")
                for p in lost:
                    em = _city_emoji(p["title"])
                    lines.append(
                        f"  {em} {p['title']} | {p['outcome']} | "
                        f"{p['size']:.1f} долей | ставка ${p['cost']:.2f}"
                    )

            if not all_positions:
                lines.append("Нет позиций")

        # ── history / filled / cancelled: local DB ───────────────────────────
        else:
            db_filter = "history" if filter_status == "history" else filter_status
            from src.telegram.stats import get_trades, get_resolution_dates
            trades = get_trades(db_filter, limit=20)
            labels = {
                "history":   "История сделок",
                "filled":    "Исполненные",
                "cancelled": "Отменённые / истёкшие",
            }
            status_emoji = {
                "filled": "✅", "open": "⏳", "pending": "🕐",
                "expired": "⏰", "cancelled": "❌",
            }

            if not trades:
                await update.message.reply_text(
                    f"📋 {labels.get(filter_status, filter_status)}: нет данных"
                )
                return

            group_ids = [t.group_id for t in trades if t.group_id]
            res_dates = get_resolution_dates(group_ids)

            basket_groups: dict[str, list] = {}
            for t in sorted(
                trades,
                key=lambda t: (t.group_id or str(t.id), t.placed_at or datetime.min),
            ):
                key = t.group_id or str(t.id)
                basket_groups.setdefault(key, []).append(t)

            lines = [
                f"📋 <b>{labels.get(filter_status, filter_status)} ({len(trades)})</b>",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
            ]
            for group_key, group_trades in basket_groups.items():
                res_date = res_dates.get(group_key)
                res_str = res_date.strftime("%d.%m") if res_date else "?"
                is_basket = len(group_trades) > 1

                if is_basket:
                    total_stake = sum(t.stake_usd or 0.0 for t in group_trades)
                    city = group_trades[0].city or group_key[:8]
                    strategy = group_trades[0].strategy_name or "?"
                    lines.append(
                        f"\n🧺 <b>КОРЗИНА: {city} {res_str}</b>"
                        f" ({len(group_trades)} бина · ${total_stake:.2f} · {strategy})"
                    )
                    for t in group_trades:
                        em = status_emoji.get(t.status, "❓")
                        short_id = (t.id or "")[:8]
                        entry_price = t.filled_price or t.target_price or 0.0
                        if t.pnl_usd is not None:
                            pct = (t.pnl_usd / t.stake_usd * 100) if t.stake_usd else 0.0
                            pnl_part = f"${t.pnl_usd:+.2f} ({pct:+.1f}%)"
                        else:
                            pnl_part = "—"
                        lines.append(
                            f"  ↳ [{short_id}] {em} {t.bin_label}"
                            f"  ${t.stake_usd or 0:.2f} @ ${entry_price:.4f}  {pnl_part}"
                        )
                else:
                    t = group_trades[0]
                    em = status_emoji.get(t.status, "❓")
                    short_id = (t.id or "")[:8]
                    entry_price = t.filled_price or t.target_price or 0.0
                    placed = t.placed_at.strftime("%d.%m %H:%M") if t.placed_at else "—"
                    city_str = t.city or "?"
                    pnl_line = ""
                    if t.pnl_usd is not None:
                        pct = (t.pnl_usd / t.stake_usd * 100) if t.stake_usd else 0.0
                        pnl_line = f"\nPnL: ${t.pnl_usd:+.2f} ({pct:+.1f}%)"
                    lines.append(
                        f"\n[{short_id}] {em} {city_str} | Max Temp {res_str}\n"
                        f"Бин: {t.bin_label} | Стратегия: {t.strategy_name}\n"
                        f"Ставка: ${t.stake_usd or 0:.2f} @ ${entry_price:.4f}{pnl_line}\n"
                        f"Размещён: {placed}"
                    )

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
        elif trade.status == "pending":
            pnl_str = "⏳ ордер не исполнен"
        elif (
            trade.status == "open"
            and trade.filled_price is not None
            and trade.resolved_at is None
            and cur_price is not None
            and entry_price > 0
            and trade.size_shares
        ):
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
                "❓ Используй: /setlimit [daily|bet|total|tail] <value>\n"
                "  daily — дневной лимит потерь (live)\n"
                "  bet   — макс. корзинная ставка (basket)\n"
                "  total — общий стоп-лосс (устаревш.)\n"
                "  tail  — макс. размер tail-ставки (per position)"
            )
            return

        limit_type = context.args[0].lower()
        if limit_type not in ("daily", "bet", "total", "tail"):
            await update.message.reply_text("❌ Тип: daily, bet, total или tail")
            return

        try:
            new_val = float(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ Значение должно быть числом")
            return

        from src.risk.risk_manager import risk_manager
        from src.strategy.config import strategy_settings
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        s = risk_manager.get_status()
        old_map = {
            "daily": (s.daily_loss_limit_usd, "Дневной лимит потерь"),
            "bet":   (s.max_single_bet_usd, "Макс. корзинная ставка"),
            "total": (s.total_stop_loss_usd, "Общий стоп-лосс"),
            "tail":  (strategy_settings.tail_max_position_usd, "Макс. tail-ставка"),
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
# /help
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_help(update, context) -> None:
    text = (
        "<b>Команды бота</b>\n\n"
        "<b>Мониторинг</b>\n"
        "/status — дашборд: баланс, PnL, позиции, риск\n"
        "/pnl [today|week|month|all] — разбивка PnL по периоду\n"
        "/trades [open|all|history|pending] — список позиций\n"
        "/trade &lt;id&gt; — карточка сделки по ID\n"
        "/risk — лимиты риска\n"
        "/paper — статус paper-торговли\n\n"
        "<b>Управление</b>\n"
        "/mode [paper|live] — переключить режим торговли\n"
        "/stop [причина] — аварийная остановка (сохраняется)\n"
        "/reset_stop — снять аварийную остановку (с подтверждением)\n"
        "/reset_paper_stop — сбросить виртуальный paper-стоп\n"
        "/rebaseline — сбросить HWM drawdown до текущей equity (dd=0)\n"
        "/cancel &lt;id&gt; — отменить ордер (с подтверждением)\n\n"
        "<b>Настройки</b>\n"
        "/setlimit [daily|bet|total|tail] &lt;значение&gt; — обновить лимит (tail = cap tail-ставки)\n"
        "/setstake &lt;значение&gt; — обновить basket-ставку\n"
        "/strategy [list|on|off] [название] — включить/выключить стратегию\n\n"
        "<b>Диагностика</b>\n"
        "/gamma — статус Gamma API + живой probe endpoint\n"
        "/gamma_reset — сбросить circuit breaker Gamma вручную\n\n"
        "/help — эта справка"
    )
    await _reply(update, text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /stop, /start, /risk  (kept from original)
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_paper(update, context) -> None:
    """Show paper trading status: losses, limits, open trades, recent history."""
    import json as _json
    from sqlalchemy import select, func as sa_func
    from src.db.session import get_session
    from src.db.models import PaperTrade
    from src.risk.risk_manager import risk_manager

    s = risk_manager.get_status()
    paper_ok, paper_reason = risk_manager.can_run_paper()

    from src.config.settings import settings
    mode = (settings.trading_mode or "paper").upper()

    lines: list[str] = []
    lines.append(f"<b>Paper Trading</b>  (режим: {mode})")
    lines.append("")

    stop_icon = "🟢 активен" if paper_ok else "🔴 ОСТАНОВЛЕН"
    lines.append(f"Статус paper:  {stop_icon}")
    if not paper_ok:
        lines.append(f"Причина: {paper_reason}")

    lines.append(
        f"Убытки сегодня:  ${s.paper_daily_loss:.2f} / ${s.paper_daily_loss_limit_usd:.2f}"
    )
    lines.append(
        f"Убытки итого:    ${s.paper_total_loss:.2f} / ${s.paper_total_stop_loss_usd:.2f}"
    )
    lines.append("")

    # Strategy: paper uses basket_wide / basket_narrow
    lines.append("<b>Стратегия paper:</b> basket_wide / basket_narrow")
    lines.append("<i>(tail стратегия в paper-режиме не поддерживается — только live)</i>")
    lines.append("")

    session = get_session()
    try:
        # Open paper trades
        open_trades = session.execute(
            select(PaperTrade)
            .where(PaperTrade.status == "open")
            .order_by(PaperTrade.decision_time.desc())
            .limit(10)
        ).scalars().all()

        if open_trades:
            lines.append(f"<b>Открытые ({len(open_trades)}):</b>")
            for t in open_trades:
                city = t.city or "?"
                strat = t.strategy_name or "basket"
                stake = t.virtual_stake_usd or 0.0
                ts = t.decision_time.strftime("%m-%d %H:%M") if t.decision_time else "?"
                try:
                    basket = _json.loads(t.basket_json or "[]")
                    bins_str = ", ".join(b.get("bin_label", "?") for b in basket[:3])
                    if len(basket) > 3:
                        bins_str += f" +{len(basket) - 3}"
                except Exception:
                    bins_str = "?"
                lines.append(f"  #{t.id}  {city}  [{strat}]  ${stake:.2f}  {bins_str}  ({ts})")
        else:
            lines.append("Открытых paper-сделок нет.")

        lines.append("")

        # Recent closed
        recent = session.execute(
            select(PaperTrade)
            .where(PaperTrade.status != "open", PaperTrade.pnl_usd.isnot(None))
            .order_by(PaperTrade.resolved_at.desc())
            .limit(5)
        ).scalars().all()

        if recent:
            lines.append("<b>Последние закрытые:</b>")
            for t in recent:
                city = t.city or "?"
                pnl = t.pnl_usd or 0.0
                icon = "✅" if pnl >= 0 else "❌"
                ts = t.resolved_at.strftime("%m-%d") if t.resolved_at else "?"
                lines.append(f"  {icon} #{t.id}  {city}  PnL: ${pnl:+.2f}  ({t.status}) {ts}")
    finally:
        session.close()

    if not paper_ok:
        lines.append("")
        lines.append("Для сброса paper-стопа: /reset_paper_stop")

    await _reply(update, "\n".join(lines), parse_mode="HTML")


@_admin_only
async def cmd_reset_paper_stop(update, context) -> None:
    """Two-stage confirmation to reset virtual paper stop."""
    from src.risk.risk_manager import risk_manager
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    paper_ok, paper_reason = risk_manager.can_run_paper()
    if paper_ok:
        await update.message.reply_text(
            "📊 Paper стоп не активен — paper-торговля уже разрешена."
        )
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 ДА, СБРОСИТЬ", callback_data="rps:confirm"),
        InlineKeyboardButton("❌ Отмена", callback_data="x"),
    ]])
    await update.message.reply_text(
        "⚠️ <b>Сбросить виртуальный paper-стоп?</b>\n\n"
        f"Причина блокировки: {paper_reason}\n\n"
        "Все убыточные paper-сделки будут исключены из учёта.\n"
        "Торговые данные в БД сохранятся (статус: voided_legacy).\n\n"
        "Это действует только на виртуальный баланс.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@_admin_only
async def cmd_stop(update, context) -> None:
    reason = " ".join(context.args) if context.args else "Manual stop via Telegram"
    from src.risk.risk_manager import risk_manager
    risk_manager.set_emergency_stop(reason)
    await update.message.reply_text(
        f"🛑 Emergency stop АКТИВИРОВАН (сохранён в БД).\n"
        f"Причина: {reason}\n\n"
        f"Бот НЕ будет размещать новые ордера даже после перезапуска.\n"
        f"Для снятия: /reset_stop"
    )


@_admin_only
async def cmd_reset_stop(update, context) -> None:
    """Two-stage confirmation to clear persistent emergency stop."""
    from src.risk.risk_manager import risk_manager
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not risk_manager.is_emergency_stopped() and not risk_manager.get_status().emergency_stop:
        await update.message.reply_text("✅ Emergency stop не активен — торговля уже включена.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ДА, СНЯТЬ СТОП", callback_data="rs:confirm"),
        InlineKeyboardButton("❌ Отмена", callback_data="x"),
    ]])
    await update.message.reply_text(
        "⚠️ <b>Подтвердите сброс Emergency Stop</b>\n\n"
        "Это разрешит боту размещать новые ордера.\n"
        "Убедитесь, что ситуация под контролем перед продолжением.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@_admin_only
async def cmd_start(update, context) -> None:
    from src.risk.risk_manager import risk_manager
    if not risk_manager.get_status().emergency_stop and not risk_manager.is_emergency_stopped():
        await update.message.reply_text(
            "✅ Emergency stop не активен — торговля уже запущена.\n"
            "Для явного сброса используй /reset_stop"
        )
        return
    await update.message.reply_text(
        "⚠️ Для сброса Emergency Stop используй /reset_stop (требует подтверждения)."
    )


@_admin_only
async def cmd_risk(update, context) -> None:
    from src.risk.risk_manager import risk_manager
    from src.strategy.live_trader import _get_equity
    from src.strategy.config import strategy_settings
    equity = _get_equity()
    s = risk_manager.get_status(equity=equity)
    total_cap = s.total_deployed_cap_usd
    tail_cap = s.tail_max_usd
    eq_str = f"${equity:.2f}" if equity > 0 else "н/д"
    text = (
        f"⚠️ <b>Risk Limits</b>\n\n"
        f"💵 Basket ставка:      ${s.max_single_bet_usd:.2f}  (/setstake)\n"
        f"🎯 Tail ставка (cap):  ${strategy_settings.tail_max_position_usd:.2f}  (/setlimit tail)\n"
        f"📅 Макс. ставок/день:  {s.max_daily_bets}  (использовано: {s.daily_bets_count})\n"
        f"📉 Дневной лимит:      ${s.daily_loss_limit_usd:.2f}  (исп.: ${s.daily_loss:.2f})  (/setlimit daily)\n"
        f"💥 Стоп-лосс (total):  ${s.total_stop_loss_usd:.2f}  (/setlimit total)\n\n"
        f"💼 <b>Капитал в работе</b>  (equity {eq_str})\n"
        f"  🎯 Tail:   ${s.tail_deployed_usd:.2f} / ${tail_cap:.2f}  ({s.tail_deployed_pct:.0%})\n"
        f"  🧺 Basket: ${s.basket_deployed_usd:.2f}  (выключен)\n"
        f"  📊 Итого:  ${s.total_deployed_usd:.2f} / ${total_cap:.2f}  ({s.total_deployed_pct:.0%})\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /gamma  — Gamma API circuit-breaker status + live reachability probe
# /gamma_reset  — force-reset circuit breaker without bot restart
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_gamma(update, context) -> None:
    """Show Gamma circuit-breaker state and probe the endpoint."""
    import asyncio
    from src.scheduler import get_gamma

    g = get_gamma()
    now = datetime.now()

    if g._circuit_open_until and now < g._circuit_open_until:
        remaining = int((g._circuit_open_until - now).total_seconds() // 60)
        breaker_line = f"🔴 OPEN (ещё ~{remaining} мин, до {g._circuit_open_until:%H:%M:%S})"
    else:
        breaker_line = "🟢 CLOSED"

    text = (
        "🔌 <b>Gamma API — диагностика</b>\n\n"
        f"Circuit: {breaker_line}\n"
        f"open_count: {g._open_count}\n"
        f"consec_timeout: {g._consec_timeout}\n"
        f"consec_hard: {g._consec_hard}\n"
        f"last_status: <code>{g._last_fetch_status}</code>\n"
        f"last_fail_kind: <code>{g._last_fail_kind or '—'}</code>\n\n"
        "⏳ Зондирую endpoint (<code>/events/keyset</code>)…"
    )
    msg = await update.message.reply_text(text, parse_mode="HTML")

    loop = asyncio.get_event_loop()
    ok, elapsed, detail = await loop.run_in_executor(None, lambda: g.probe(timeout_s=15.0))

    probe_icon = "✅" if ok else "❌"
    result_text = text.replace(
        "⏳ Зондирую endpoint (<code>/events/keyset</code>)…",
        f"{probe_icon} Probe: {elapsed:.1f}s — <code>{detail}</code>\n\n"
        "/gamma_reset — сбросить брейкер вручную",
    )
    await msg.edit_text(result_text, parse_mode="HTML")


@_admin_only
async def cmd_gamma_reset(update, context) -> None:
    """Force-reset Gamma circuit breaker (use after IP/rate-limit fix or Gamma recovery)."""
    from src.scheduler import get_gamma

    g = get_gamma()
    prev = g._open_count
    was_open = g._circuit_open_until is not None and datetime.now() < g._circuit_open_until
    g.reset_circuit()
    state = "OPEN" if was_open else "closed"
    await update.message.reply_text(
        f"✅ Gamma circuit breaker сброшен.\n"
        f"Было: {state}, open_count={prev}\n"
        f"Следующий цикл discovery попробует немедленно."
    )


# ---------------------------------------------------------------------------
# /rebaseline  — prune equity HWM to current equity (FIX-1b)
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_rebaseline(update, context) -> None:
    """Two-stage: reset drawdown HWM_30d to current equity so dd=0 on next cycle."""
    from src.risk.risk_manager import risk_manager
    from src.strategy.live_trader import _get_equity
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    equity = _get_equity()
    s = risk_manager.get_status(equity=equity)
    hwm_est = equity / (1.0 - s.total_deployed_pct) if s.total_deployed_pct < 1.0 else equity

    eq_str = f"${equity:.2f}" if equity > 0 else "н/д"
    stop_note = ""
    if s.emergency_stop and s.emergency_stop_type == "drawdown":
        stop_note = "\n⚠️ Активен drawdown-стоп — будет снят автоматически после rebaseline."

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 ДА, REBASELINE", callback_data=f"rb:confirm:{equity:.4f}"),
        InlineKeyboardButton("❌ Отмена", callback_data="x"),
    ]])
    await update.message.reply_text(
        f"⚠️ <b>Rebaseline HWM?</b>\n\n"
        f"Текущая equity: <b>{eq_str}</b>\n"
        f"Это установит HWM_30d = {eq_str} (dd=0).\n\n"
        f"Все исторические пики за 30 дней будут удалены.\n"
        f"Drawdown-стоп не сработает до следующего просадки от нового базиса.{stop_note}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


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
                label = "Макс. корзинная ставка"
            elif limit_type == "tail":
                risk_manager.set_tail_max_position_usd(new_val)
                label = "Макс. tail-ставка"
            else:  # total
                risk_manager.set_total_stop_loss(new_val)
                label = "Общий стоп-лосс"
            logger.info(f"[telegram_bot] setlimit {limit_type}→${new_val:.2f} via callback")
            await query.edit_message_text(
                f"✅ <b>{label}</b> обновлён: ${new_val:.2f} (сохранено в БД)",
                parse_mode="HTML",
            )
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

        # ── mode switch: mode:live:confirm ───────────────────────────────
        if data == "mode:live:confirm":
            _apply_mode_switch("live")
            from src.config.settings import settings as _s
            actual = _s.trading_mode.upper()
            logger.info(f"[telegram_bot] Mode switched to {actual} via confirmation callback")
            await query.edit_message_text(
                f"✅ Режим переключён в <b>{actual}</b>.\n"
                "⚠️ Бот размещает реальные ордера.\n"
                "Для остановки: /mode paper или /stop",
                parse_mode="HTML",
            )
            return

        # ── reset_stop: rs:confirm ───────────────────────────────────────
        if data == "rs:confirm":
            from src.risk.risk_manager import risk_manager
            risk_manager.clear_emergency_stop()
            logger.info("[telegram_bot] Emergency stop cleared via /reset_stop callback")
            await query.edit_message_text(
                "✅ Emergency stop снят (БД обновлена).\n"
                "⚠️ Внимание: если bot был запущен с активным стопом, "
                "live-job не был зарегистрирован — перезапустите бота для восстановления."
            )
            return

        # ── rebaseline HWM: rb:confirm:{equity} ─────────────────────────
        if data.startswith("rb:confirm:"):
            try:
                equity_val = float(data.split(":", 2)[2])
            except (IndexError, ValueError):
                await query.edit_message_text("❌ Ошибка: неверный формат данных")
                return
            from src.risk.risk_manager import risk_manager
            risk_manager.rebaseline_hwm(equity_val)
            # If there's an active drawdown stop, clear it; dd=0 next cycle won't re-arm
            if risk_manager.is_emergency_stopped() and risk_manager.get_stop_type() == "drawdown":
                risk_manager.clear_emergency_stop()
                stop_cleared = "\n✅ Drawdown-стоп снят."
            else:
                stop_cleared = ""
            logger.info(f"[telegram_bot] HWM rebaselined to ${equity_val:.2f} via callback")
            await query.edit_message_text(
                f"📊 HWM_30d сброшен до ${equity_val:.2f}.\n"
                f"Drawdown = 0% на следующем цикле.{stop_cleared}\n"
                f"Новая просадка считается от этого базиса."
            )
            return

        # ── reset_paper_stop: rps:confirm ────────────────────────────────
        if data == "rps:confirm":
            from src.risk.risk_manager import risk_manager
            n = risk_manager.reset_paper_stop()
            logger.info(f"[telegram_bot] Paper stop reset via callback: {n} trades voided")
            await query.edit_message_text(
                f"📊 Paper-стоп сброшен.\n"
                f"Исключено из учёта: {n} убыточных сделок (статус: voided_legacy).\n"
                f"Paper-торговля возобновлена."
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
# /mode [paper|live]  — switch trading mode with confirmation (G2-4)
# ---------------------------------------------------------------------------

@_admin_only
async def cmd_mode(update, context) -> None:
    """Show or switch trading mode. Switching to 'live' requires two-stage confirmation."""
    from src.config.settings import settings
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    current = settings.trading_mode.lower()

    if not context.args:
        em = "💵 LIVE" if current == "live" else "📝 PAPER"
        await update.message.reply_text(
            f"Текущий режим: <b>{em}</b>\n\n"
            f"Сменить: /mode paper | /mode live",
            parse_mode="HTML",
        )
        return

    target = context.args[0].lower()
    if target not in ("paper", "live"):
        await update.message.reply_text("❓ Используй: /mode paper | /mode live")
        return

    if target == current:
        await update.message.reply_text(f"ℹ️ Уже в режиме {target.upper()}")
        return

    if target == "paper":
        # Paper switch is safe — do immediately
        _apply_mode_switch("paper")
        actual = settings.trading_mode.upper()
        logger.info(f"[telegram_bot] Mode switched to {actual} via /mode command")
        await update.message.reply_text(
            f"✅ Режим переключён в <b>{actual}</b> (симуляция).\n"
            "Биржевые ордера размещаться не будут.",
            parse_mode="HTML",
        )
        return

    # Live mode: two-stage confirmation
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ДА, ВКЛЮЧИТЬ LIVE", callback_data="mode:live:confirm"),
        InlineKeyboardButton("❌ Отмена", callback_data="x"),
    ]])
    await update.message.reply_text(
        "⚠️ <b>Переключить в LIVE режим?</b>\n\n"
        "Бот будет размещать реальные ордера на Polymarket.\n"
        "Баланс: реальные деньги.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


def _apply_mode_switch(mode: str) -> None:
    """Persist mode switch to bot_state DB and update settings in-process."""
    from src.db.session import get_session
    from src.db.models import BotState

    session = get_session()
    try:
        state = session.get(BotState, "trading_mode")
        if state:
            state.value = mode
        else:
            session.add(BotState(key="trading_mode", value=mode))
        session.commit()
    except Exception as exc:
        logger.error(f"[telegram_bot] Failed to persist mode switch: {exc}")
        session.rollback()
    finally:
        session.close()

    # Sync in-process settings so cmd_mode and schedulerjobs see the new mode immediately
    from src.config.settings import settings
    settings.trading_mode = mode


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
            app.add_handler(CommandHandler("paper",    cmd_paper))
            app.add_handler(CommandHandler("help",     cmd_help))

            # Control commands
            app.add_handler(CommandHandler("stop",             cmd_stop))
            app.add_handler(CommandHandler("reset_stop",       cmd_reset_stop))
            app.add_handler(CommandHandler("reset_paper_stop", cmd_reset_paper_stop))
            app.add_handler(CommandHandler("rebaseline",       cmd_rebaseline))
            app.add_handler(CommandHandler("start",            cmd_start))
            app.add_handler(CommandHandler("mode",             cmd_mode))
            app.add_handler(CommandHandler("setlimit",         cmd_setlimit))
            app.add_handler(CommandHandler("setstake",         cmd_setstake))
            app.add_handler(CommandHandler("strategy",         cmd_strategy))
            app.add_handler(CommandHandler("cancel",           cmd_cancel_trade))
            app.add_handler(CommandHandler("gamma",            cmd_gamma))
            app.add_handler(CommandHandler("gamma_reset",      cmd_gamma_reset))

            # Inline-button callbacks
            app.add_handler(CallbackQueryHandler(_callback_router))

            async def _heartbeat_loop() -> None:
                """Write telegram-thread heartbeat every 60 s for external watchdog."""
                from src.monitoring.heartbeat import write_heartbeat
                while True:
                    write_heartbeat("telegram")
                    await asyncio.sleep(60)

            async with app:
                await app.start()
                logger.info("[telegram_bot] Starting polling…")
                await app.updater.start_polling(drop_pending_updates=True)
                logger.info("[telegram_bot] Polling active — commands: "
                            "status, pnl, trades, trade, risk, paper, help, "
                            "stop, reset_stop, reset_paper_stop, start, mode, "
                            "setlimit, setstake, strategy, cancel")
                asyncio.create_task(_heartbeat_loop())
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
