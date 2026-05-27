"""
Data-aggregation helpers for the Telegram bot.
All functions open and close their own DB sessions.
No trading logic here — read-only queries only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from loguru import logger
from sqlalchemy import select, func

from src.db.session import get_session
from src.db.models import LiveTrade, MarketSnapshot


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def _period_bounds(period: str) -> tuple[datetime, datetime, str]:
    """Returns (start, end, label) for the given period keyword."""
    now = datetime.utcnow()
    today = now.date()

    if period == "today":
        start = datetime.combine(today, datetime.min.time())
        label = f"сегодня ({today.strftime('%d.%m')})"
    elif period == "week":
        week_start = today - timedelta(days=today.weekday())
        start = datetime.combine(week_start, datetime.min.time())
        label = f"неделю ({week_start.strftime('%d.%m')}–{today.strftime('%d.%m')})"
    elif period == "month":
        month_start = today.replace(day=1)
        start = datetime.combine(month_start, datetime.min.time())
        label = f"месяц ({today.strftime('%B %Y')})"
    else:  # "all"
        start = datetime(2020, 1, 1)
        label = "всё время"

    return start, now, label


# ---------------------------------------------------------------------------
# PnL statistics
# ---------------------------------------------------------------------------

def pnl_stats(period: str = "today") -> dict:
    """
    Aggregate PnL for resolved LiveTrades in the given period.
    A trade is "resolved" when resolved_at IS NOT NULL and pnl_usd IS NOT NULL.

    Returns:
        pnl, wins, losses, count, total_staked, roi_pct,
        by_strategy (dict name→{pnl, wins, losses}),
        by_day (list of {date, pnl, wins, losses}),
        label (human-readable period name).
    """
    start, _end, label = _period_bounds(period)
    session = get_session()
    try:
        q = select(LiveTrade).where(
            LiveTrade.resolved_at.isnot(None),
            LiveTrade.pnl_usd.isnot(None),
        )
        if period != "all":
            q = q.where(LiveTrade.resolved_at >= start)

        trades = list(session.execute(q).scalars().all())

        pnl = sum(t.pnl_usd or 0.0 for t in trades)
        wins = sum(1 for t in trades if (t.pnl_usd or 0.0) > 0)
        losses = len(trades) - wins
        total_staked = sum(t.stake_usd or 0.0 for t in trades)
        roi_pct = (pnl / total_staked * 100) if total_staked > 0 else 0.0

        # By strategy
        by_strategy: dict[str, dict] = {}
        for t in trades:
            strat = t.strategy_name or "unknown"
            if strat not in by_strategy:
                by_strategy[strat] = {"pnl": 0.0, "wins": 0, "losses": 0}
            by_strategy[strat]["pnl"] += t.pnl_usd or 0.0
            if (t.pnl_usd or 0.0) > 0:
                by_strategy[strat]["wins"] += 1
            else:
                by_strategy[strat]["losses"] += 1

        # By day (only for multi-day periods)
        by_day: list[dict] = []
        if period in ("week", "month", "all"):
            days: dict[str, dict] = {}
            for t in trades:
                if t.resolved_at:
                    key = t.resolved_at.strftime("%d.%m")
                    if key not in days:
                        days[key] = {"date": key, "pnl": 0.0, "wins": 0, "losses": 0}
                    days[key]["pnl"] += t.pnl_usd or 0.0
                    if (t.pnl_usd or 0.0) > 0:
                        days[key]["wins"] += 1
                    else:
                        days[key]["losses"] += 1
            by_day = sorted(days.values(), key=lambda x: x["date"])

        return {
            "pnl": pnl,
            "wins": wins,
            "losses": losses,
            "count": len(trades),
            "total_staked": total_staked,
            "roi_pct": roi_pct,
            "by_strategy": by_strategy,
            "by_day": by_day,
            "label": label,
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Dashboard counts
# ---------------------------------------------------------------------------

def summary_counts() -> dict:
    """
    Quick counters for the /status dashboard:
        open          — pending + open orders
        awaiting      — filled but not yet resolved
        filled_today  — filled today
        hwm           — cumulative all-time PnL (high-water mark proxy)
        winrate       — all-time win percentage
    """
    session = get_session()
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())

        open_count = session.execute(
            select(func.count()).select_from(LiveTrade).where(
                LiveTrade.status.in_(["pending", "open"])
            )
        ).scalar() or 0

        awaiting = session.execute(
            select(func.count()).select_from(LiveTrade).where(
                LiveTrade.status == "filled",
                LiveTrade.resolved_at.is_(None),
            )
        ).scalar() or 0

        filled_today = session.execute(
            select(func.count()).select_from(LiveTrade).where(
                LiveTrade.status == "filled",
                LiveTrade.filled_at >= today_start,
            )
        ).scalar() or 0

        hwm = session.execute(
            select(func.coalesce(func.sum(LiveTrade.pnl_usd), 0.0)).where(
                LiveTrade.pnl_usd.isnot(None)
            )
        ).scalar() or 0.0

        resolved_all = list(
            session.execute(
                select(LiveTrade).where(LiveTrade.pnl_usd.isnot(None))
            ).scalars().all()
        )
        all_wins = sum(1 for t in resolved_all if (t.pnl_usd or 0) > 0)
        winrate = (all_wins / len(resolved_all) * 100) if resolved_all else 0.0

        return {
            "open": open_count,
            "awaiting": awaiting,
            "filled_today": filled_today,
            "hwm": float(hwm),
            "winrate": winrate,
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Open-position portfolio value
# ---------------------------------------------------------------------------

def open_positions_value() -> float:
    """
    Estimated current market value of all open / filled-unresolved positions.
    = Σ (latest_price_yes × shares) for each active trade.
    Uses the most recent MarketSnapshot; falls back to entry price if no snapshot.
    """
    session = get_session()
    try:
        trades = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.status.in_(["pending", "open", "filled"]),
                    LiveTrade.resolved_at.is_(None),
                )
            ).scalars().all()
        )

        total = 0.0
        for t in trades:
            snap = session.execute(
                select(MarketSnapshot)
                .where(MarketSnapshot.market_id == t.market_id)
                .order_by(MarketSnapshot.snapshot_time.desc())
                .limit(1)
            ).scalars().first()

            price = (snap.price_yes if snap else None) or t.target_price or 0.0
            shares = t.size_shares or 0.0
            total += price * shares

        return total
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Trade listing
# ---------------------------------------------------------------------------

def get_trades(filter_status: str = "open", limit: int = 10) -> list[LiveTrade]:
    """
    Return LiveTrades filtered by status category.

    filter_status:
        'open'      → pending + open (placed but not yet filled)
        'pending'   → pending only
        'filled'    → filled (order executed, market may still be open)
        'cancelled' → cancelled + expired
        'all'       → everything
    """
    session = get_session()
    try:
        status_map: dict[str, list[str]] = {
            "open":      ["pending", "open"],
            "pending":   ["pending"],
            "filled":    ["filled"],
            "cancelled": ["cancelled", "expired"],
            "all":       ["pending", "open", "filled", "cancelled", "expired"],
        }
        statuses = status_map.get(filter_status, ["pending", "open"])

        rows = list(
            session.execute(
                select(LiveTrade)
                .where(LiveTrade.status.in_(statuses))
                .order_by(LiveTrade.placed_at.desc())
                .limit(limit)
            ).scalars().all()
        )
        # Detach from session so we can use them after close
        session.expunge_all()
        return rows
    finally:
        session.close()


def get_trade_by_prefix(trade_prefix: str) -> LiveTrade | None:
    """Find a LiveTrade whose UUID starts with trade_prefix."""
    session = get_session()
    try:
        trade = session.execute(
            select(LiveTrade).where(LiveTrade.id.like(f"{trade_prefix}%"))
        ).scalars().first()
        if trade:
            session.expunge(trade)
        return trade
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Current market price
# ---------------------------------------------------------------------------

def get_current_price(market_id: str) -> float | None:
    """Return the most recent price_yes from MarketSnapshot, or None."""
    session = get_session()
    try:
        snap = session.execute(
            select(MarketSnapshot)
            .where(MarketSnapshot.market_id == market_id)
            .order_by(MarketSnapshot.snapshot_time.desc())
            .limit(1)
        ).scalars().first()
        return snap.price_yes if snap else None
    finally:
        session.close()
