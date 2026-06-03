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


def _redeem_amount_by_event(activity: list[dict]) -> dict[tuple[str, str], float]:
    """
    De-duplicated REDEEM payout per (conditionId, on-chain event).

    G4-23: a NegRisk redemption — redeemPositions(conditionId, [yesIdx, noIdx]) —
    reports the GROSS payout on EACH index row in /activity. The winning index
    carries the full $1/share, the losing index carries the same gross again
    (or $0), so naively summing every REDEEM row double-counts the payout
    (Seoul +$0.74 → +$7.45, Chicago +$0.57 → +$7.16 = 2×redeem − cost).

    We collapse rows that belong to the SAME on-chain redemption to a single
    payout. Identity = (conditionId, transactionHash) when a hash is present,
    else (conditionId, timestamp). Within one identity the rows are the same
    economic event, so we keep the MAX usdcSize (the winning leg) ONCE. Two
    genuinely separate redeems of the same condition (different tx/timestamp)
    keep distinct identities and are both counted.
    """
    amt_by_event: dict[tuple[str, str], float] = {}
    for a in activity:
        if a.get("type", "").upper() != "REDEEM":
            continue
        cid = a.get("conditionId", "")
        if not cid:
            continue
        try:
            amt = float(a.get("usdcSize") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        txid = (
            a.get("transactionHash")
            or a.get("transaction_hash")
            or a.get("hash")
            or str(a.get("timestamp", ""))
        )
        key = (cid, str(txid))
        # Same on-chain event reported on both index rows → count once (max leg).
        if amt > amt_by_event.get(key, 0.0):
            amt_by_event[key] = amt
    return amt_by_event


def redeem_payout_by_condition(activity: list[dict]) -> dict[str, float]:
    """De-duplicated total REDEEM payout per conditionId (G4-23). UI/ledger truth."""
    out: dict[str, float] = defaultdict(float)
    for (cid, _txid), amt in _redeem_amount_by_event(activity).items():
        out[cid] += amt
    return dict(out)


def realized_pnl_by_token(activity: list[dict]) -> dict[str, float]:
    """
    Aggregate realized PnL per token_id from cash flows.

    PnL(token) = Σ(SELL proceeds) + Σ(REDEEM payout) − Σ(BUY cost)

    REDEEM has an empty `asset` field — we link it to its token via the single
    BUY token bought for that conditionId (confirmed: each resolved conditionId
    has exactly one winning BUY asset). Falls back to keying by "cond:<id>" in
    the rare case of multiple BUY tokens per condition (e.g. averaged-in).

    REDEEM payouts are de-duplicated per on-chain event first (see
    _redeem_amount_by_event) — NegRisk reports the gross payout on every index
    row, which would otherwise double-count the win.
    """
    # Pass 1: build conditionId → [token_ids] map from BUY activity
    cond_tokens: dict[str, list[str]] = defaultdict(list)
    for a in activity:
        if a.get("type") == "TRADE" and a.get("side", "").upper() == "BUY":
            token = a.get("asset", "")
            cid = a.get("conditionId", "")
            if token and cid:
                cond_tokens[cid].append(token)

    # Pass 2: TRADE cash flows (BUY debit / SELL proceeds) inline
    pnl: dict[str, float] = defaultdict(float)
    for a in activity:
        if a.get("type", "").upper() != "TRADE":
            continue
        try:
            amt = float(a.get("usdcSize") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        token = a.get("asset", "")
        side = a.get("side", "").upper()
        if side == "BUY" and token:
            pnl[token] -= amt
        elif side == "SELL" and token:
            pnl[token] += amt

    # Pass 3: REDEEM payouts — de-duplicated per on-chain event, then routed to token
    for (cid, _txid), amt in _redeem_amount_by_event(activity).items():
        tokens = cond_tokens.get(cid, [])
        if len(tokens) == 1:
            pnl[tokens[0]] += amt
        elif tokens:
            # Multiple BUY tokens for same condition (averaged-in): split evenly
            per = amt / len(tokens)
            for tok in tokens:
                pnl[tok] += per
        else:
            # No matching BUY found — fall back to conditionId key
            pnl[f"cond:{cid}"] += amt

    return dict(pnl)


def buy_shares_by_token(activity: list[dict]) -> dict[str, float]:
    """
    Σ of BUY share size per token_id from /activity.

    This is the ONLY proof that a position actually exists on Polymarket — a
    local order_id / CLOB 'matched' status does NOT mean the BUY settled.
    Used to gate fills (forward) and PnL finalization (backward).
    """
    shares: dict[str, float] = defaultdict(float)
    for a in activity:
        if a.get("type") == "TRADE" and a.get("side", "").upper() == "BUY":
            token = a.get("asset", "")
            if not token:
                continue
            try:
                sz = float(a.get("size") or 0)
            except (TypeError, ValueError):
                sz = 0.0
            shares[token] += sz
    return dict(shares)


def open_cost_by_token(activity: list[dict]) -> dict[str, float]:
    """
    Current open USD exposure per token_id from /activity cash flows:
        Σ(BUY usdcSize) − Σ(SELL usdcSize), floored at 0.

    G4-25: the token-cap double-guard uses this so an order that FAILED on the
    client but actually executed on-chain (status=None / timeout) still counts
    toward the per-token cap — the DB alone misses it (recorded cancelled/absent).
    A sold position nets back toward 0, so legitimate re-entry isn't blocked.
    """
    cost: dict[str, float] = defaultdict(float)
    for a in activity:
        if a.get("type", "").upper() != "TRADE":
            continue
        token = a.get("asset", "")
        if not token:
            continue
        try:
            amt = float(a.get("usdcSize") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        side = a.get("side", "").upper()
        if side == "BUY":
            cost[token] += amt
        elif side == "SELL":
            cost[token] -= amt
    return {tok: v for tok, v in cost.items() if v > 1e-9}


def redeemed_condition_ids(activity: list[dict]) -> set[str]:
    """Set of conditionIds that have a REDEEM cash-in event (winning positions)."""
    out: set[str] = set()
    for a in activity:
        if a.get("type", "").upper() == "REDEEM":
            cid = a.get("conditionId", "")
            if cid:
                out.add(cid)
    return out


def realized_pnl_by_condition(activity: list[dict]) -> dict[str, float]:
    """Legacy alias — kept for any callers outside scheduler. Prefer realized_pnl_by_token."""
    # Build token map then collapse back to conditionId
    token_pnl = realized_pnl_by_token(activity)
    cond_tokens: dict[str, list[str]] = defaultdict(list)
    for a in activity:
        if a.get("type") == "TRADE" and a.get("side", "").upper() == "BUY":
            token = a.get("asset", "")
            cid = a.get("conditionId", "")
            if token and cid:
                cond_tokens[cid].append(token)

    result: dict[str, float] = defaultdict(float)
    for cid, tokens in cond_tokens.items():
        for tok in tokens:
            if tok in token_pnl:
                result[cid] += token_pnl[tok]
    # also carry cond: fallback keys
    for key, val in token_pnl.items():
        if key.startswith("cond:"):
            result[key[5:]] += val
    return dict(result)


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
