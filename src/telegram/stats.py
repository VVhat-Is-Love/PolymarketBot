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
from src.db.models import LiveTrade, MarketGroup, MarketSnapshot


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
    PnL is attributed by placed_at date (entry date), NOT resolved_at.
    This avoids daily-stop distortion from the ~1.5-day resolution lag.

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
            q = q.where(LiveTrade.placed_at >= start)  # attributed by entry date

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

        # By day (only for multi-day periods) — grouped by placed_at (entry date)
        by_day: list[dict] = []
        if period in ("week", "month", "all"):
            days: dict[str, dict] = {}
            for t in trades:
                if t.placed_at:  # attribute PnL to the day the trade was opened
                    key = t.placed_at.strftime("%d.%m")
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
            "history":   ["filled", "cancelled", "expired"],
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


# ---------------------------------------------------------------------------
# Resolution dates for market groups
# ---------------------------------------------------------------------------

def get_resolution_dates(group_ids: list[str]) -> dict[str, date]:
    """Return {group_id: resolution_date} for the given group IDs."""
    if not group_ids:
        return {}
    session = get_session()
    try:
        rows = session.execute(
            select(MarketGroup.group_id, MarketGroup.resolution_date).where(
                MarketGroup.group_id.in_(group_ids)
            )
        ).all()
        return {r.group_id: r.resolution_date for r in rows}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Real Polymarket positions (authoritative source)
# ---------------------------------------------------------------------------

def get_real_polymarket_positions() -> list[dict]:
    """
    Fetch all positions from Polymarket data-api.
    All monetary values (cost, current_value, pnl, pnl_pct) are pre-computed
    by the API — not recalculated locally.

    Key field note: the API uses 'curPrice' (NOT 'currentPrice').

    Returns [] on any network/HTTP error (errors logged).
    Classification helpers (won / active / lost) live in bot.py callers.
    """
    import requests
    from src.config.settings import settings

    if not settings.proxy_wallet_address:
        logger.warning("[positions] PROXY_WALLET_ADDRESS не задан")
        return []

    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={
                "user": settings.proxy_wallet_address,
                "sizeThreshold": 0.1,
                "sortBy": "CURRENT",
                "sortDirection": "DESC",
                "limit": 100,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json() or []
    except Exception as e:
        logger.error(f"[positions] data-api запрос упал: {e}")
        return []

    if raw:
        logger.debug(f"[positions] sample raw position: {raw[0]}")

    positions = []
    for p in raw:
        cur_price = float(p.get("curPrice", 0))      # curPrice — правильное имя поля
        avg_price = float(p.get("avgPrice", 0))
        positions.append({
            "title": p.get("title", "Unknown"),
            "outcome": p.get("outcome", "?"),
            "size": float(p.get("size", 0)),
            "avg_price": avg_price,
            "cur_price": cur_price,
            "cost": float(p.get("initialValue", 0)),
            "current_value": float(p.get("currentValue", 0)),
            "pnl": float(p.get("cashPnl", 0)),
            "pnl_pct": float(p.get("percentPnl", 0)),
            "redeemable": bool(p.get("redeemable", False)),
            "end_date": p.get("endDate", ""),
            "condition_id": p.get("conditionId", ""),
            "asset": str(p.get("asset", "")),   # token id — for bid-valuation (get_equity_bid)
        })
    return positions


# ---------------------------------------------------------------------------
# Portfolio breakdown (cash + open positions)
# ---------------------------------------------------------------------------

def get_portfolio_breakdown() -> dict:
    """
    Returns:
      cash_balance     — free USDC inside Polymarket (via CLOB API)
      positions_value  — sum(size * currentPrice) for open positions (via data-api)
      positions_count  — number of open positions
      positions_pnl    — total unrealized PnL across all positions
      total            — cash_balance + positions_value
    Each key falls back to 0.0 on error; errors are logged as warnings.
    """
    result = {
        "cash_balance": 0.0,
        "positions_value": 0.0,
        "positions_count": 0,
        "positions_pnl": 0.0,
        "total": 0.0,
    }

    # 1. Free USDC inside the exchange via CLOB V2 API
    try:
        from src.blockchain.polymarket_auth import get_clob_client
        from src.blockchain.balance import get_polymarket_cash_balance
        client = get_clob_client()
        result["cash_balance"] = get_polymarket_cash_balance(client)
    except Exception as exc:
        logger.warning(f"[stats] cash_balance fetch failed: {exc}")

    # 2. Current market value of open positions via Polymarket data-api
    try:
        positions = get_real_polymarket_positions()
        # Exclude clearly lost positions (cur_price ~0 and not redeemable)
        relevant = [p for p in positions if p["cur_price"] > 0.01 or p["redeemable"]]
        result["positions_value"] = sum(p["current_value"] for p in relevant)
        result["positions_count"] = len(relevant)
        result["positions_pnl"] = sum(p["pnl"] for p in relevant)
    except Exception as exc:
        logger.warning(f"[stats] positions_value fetch failed: {exc}")

    result["total"] = result["cash_balance"] + result["positions_value"]
    return result


def get_equity_bid() -> dict:
    """
    Equity valued conservatively at BID for the drawdown emergency stop.

    Same underlying source as get_portfolio_breakdown (Polymarket cash + open
    positions), but each LIVE position is marked at its best BID (realizable
    value) instead of the data-api curPrice/mark. A thin-book mid spike therefore
    cannot inflate equity — or the 30-day HWM derived from it — and false-trip or
    false-clear the stop. Redeemable (won) positions are valued at currentValue
    (≈ $1/share redemption, not biddable). Falls back to mark when a book is
    unavailable for a token.

    Returns {cash, positions_value_bid, total_bid, ok}.
    ok=False ⇒ a balance OR positions fetch failed → caller MUST defer (never move
    HWM / drawdown on a partial/failed read; that is what makes the stop robust).
    NOTE: this is intentionally separate from get_portfolio_breakdown so the
    equity-caps path (which uses mark) is left untouched.
    """
    import requests
    from src.config.settings import settings

    out = {"cash": 0.0, "positions_value_bid": 0.0, "total_bid": 0.0, "ok": False}
    if not settings.proxy_wallet_address:
        return out

    # 1. Free USDC inside the exchange (same source as get_portfolio_breakdown)
    try:
        from src.blockchain.polymarket_auth import get_clob_client
        from src.blockchain.balance import get_polymarket_cash_balance
        out["cash"] = get_polymarket_cash_balance(get_clob_client())
    except Exception as exc:
        logger.warning(f"[stats] equity_bid cash fetch failed: {exc} — defer")
        return out  # ok stays False

    # 2. Open positions (checked fetch so ok is honest — empty[] from an outage
    #    must NOT be read as 'no positions', which would understate equity and
    #    falsely trip the drawdown stop).
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": settings.proxy_wallet_address, "sizeThreshold": 0.1, "limit": 100},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json() or []
    except Exception as exc:
        logger.warning(f"[stats] equity_bid positions fetch failed: {exc} — defer")
        return out  # ok stays False

    from src.market.clob_client import get_book_snapshot
    pv = 0.0
    for p in raw:
        try:
            size = float(p.get("size", 0))
            cur = float(p.get("curPrice", 0))
            cur_value = float(p.get("currentValue", 0))
        except (TypeError, ValueError):
            continue
        redeemable = bool(p.get("redeemable", False))
        if not (cur > 0.01 or redeemable):
            continue  # lost / dust
        if redeemable:
            pv += cur_value           # won → redemption value, not a book bid
            continue
        token = str(p.get("asset") or "")
        bid = None
        if token:
            try:
                snap = get_book_snapshot(token)
                if snap:
                    bid = snap.get("best_bid")
            except Exception:
                bid = None
        pv += size * bid if bid else cur_value   # bid if live book, else fall back to mark

    out["positions_value_bid"] = pv
    out["total_bid"] = out["cash"] + pv
    out["ok"] = True
    return out
