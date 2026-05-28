"""
polymarket_data.py — Polymarket Data API client (no auth required).

Source of truth for resolved PnL and open positions.
Field names verified against live /activity response 2026-05-29:
  type      = "TRADE" (side=BUY/SELL) | "REDEEM"
  usdcSize  = USD amount
  asset     = token ID (filled for TRADE, empty for REDEEM)
  conditionId = condition ID (always present)
  timestamp = unix seconds
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import requests
from loguru import logger

DATA_API = "https://data-api.polymarket.com"
_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json", "User-Agent": "polymarket-bot/2.0"})

# Cash-in types: these increase our realized balance
CASH_IN_TYPES = {"REDEEM", "SELL", "REWARD", "REBATE", "MERGE"}
# Cash-out types: these decrease our realized balance
CASH_OUT_TYPES = {"BUY", "SPLIT"}


def get_activity(wallet: str, limit: int = 500) -> list[dict[str, Any]]:
    """
    Fetch all activity for a wallet from Polymarket Data API.
    Paginates automatically until exhausted.
    """
    items: list[dict] = []
    offset = 0
    while True:
        try:
            r = _SESSION.get(
                f"{DATA_API}/activity",
                params={"user": wallet, "limit": limit, "offset": offset},
                timeout=20,
            )
            r.raise_for_status()
            batch: list[dict] = r.json()
        except Exception as exc:
            logger.error(f"[polymarket_data] get_activity failed at offset={offset}: {exc}")
            break

        if not batch:
            break
        items.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.3)

    logger.debug(f"[polymarket_data] Fetched {len(items)} activity items for {wallet[:12]}…")
    return items


def get_positions(wallet: str, redeemable: bool | None = None) -> list[dict[str, Any]]:
    """
    Fetch open positions for a wallet.
    redeemable=True → only positions that can be redeemed for $1.
    """
    params: dict = {"user": wallet, "sizeThreshold": 0.1}
    if redeemable is not None:
        params["redeemable"] = str(redeemable).lower()
    try:
        r = _SESSION.get(f"{DATA_API}/positions", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.error(f"[polymarket_data] get_positions failed: {exc}")
        return []


def realized_pnl_by_condition(activity: list[dict]) -> dict[str, float]:
    """
    Aggregate realized PnL per conditionId from cash flows.

    PnL = Σ(REDEEM + SELL + …) − Σ(BUY + SPLIT + …)

    Note: REDEEM entries have empty `asset`; we key by conditionId for both.
    """
    pnl: dict[str, float] = defaultdict(float)
    for a in activity:
        t = a.get("type", "").upper()
        side = a.get("side", "").upper()
        # Normalise: TRADE:BUY → BUY, TRADE:SELL → SELL
        if t == "TRADE":
            t = side  # "BUY" or "SELL"
        try:
            amt = float(a.get("usdcSize") or 0)
        except (TypeError, ValueError):
            amt = 0.0

        cond_id = a.get("conditionId", "")
        if not cond_id:
            continue

        if t in CASH_IN_TYPES:
            pnl[cond_id] += amt
        elif t in CASH_OUT_TYPES:
            pnl[cond_id] -= amt

    return dict(pnl)


def open_market_value(positions: list[dict]) -> dict[str, float]:
    """
    Mark-to-market value of open (not yet resolved) positions, keyed by conditionId.
    curPrice × size gives current market value.
    """
    result: dict[str, float] = {}
    for p in positions:
        if p.get("redeemable"):
            continue  # resolved, not open
        cond_id = p.get("conditionId", "")
        if not cond_id:
            continue
        try:
            cur_price = float(p.get("curPrice") or 0)
            size = float(p.get("size") or 0)
        except (TypeError, ValueError):
            continue
        result[cond_id] = cur_price * size
    return result
