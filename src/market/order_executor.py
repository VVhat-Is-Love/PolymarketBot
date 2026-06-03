"""
Order execution via Polymarket CLOB V2 API.
Exponential backoff: 3 attempts with 1s / 2s / 4s delays.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from loguru import logger
from src.risk.risk_manager import risk_manager

_BACKOFF = [1, 2, 4]


@dataclass
class Order:
    order_id: str
    market_id: str
    side: str
    price: float
    size: float
    status: str  # 'open' | 'filled' | 'cancelled' | 'unknown'


def _get_client():
    from src.blockchain.polymarket_auth import get_clob_client
    return get_clob_client()


def _with_backoff(fn, *, module: str, method: str, market_id: str = ""):
    """Call fn() with exponential backoff; never logs secrets."""
    last_err = None
    for attempt, delay in enumerate(_BACKOFF, start=1):
        try:
            return fn()
        except Exception as exc:
            last_err = exc
            logger.error(
                f"[{module}] {method} failed (attempt {attempt}/{len(_BACKOFF)}): "
                f"market_id={market_id or 'N/A'} error={type(exc).__name__}: {exc}"
            )
            if attempt < len(_BACKOFF):
                time.sleep(delay)
    raise last_err


_MIN_SHARES = 5.0   # Polymarket V2: minimum shares per order
_MIN_USD    = 1.0   # Polymarket V2: minimum notional per order


def _place_error_kind(exc: Exception) -> str:
    """
    Classify a placement failure (G4-25):

      • "clean"     — explicit client reject (4xx with a real status, not
                      408/429): the order definitively did NOT post → safe to
                      retry blindly.
      • "ambiguous" — status_code is None, a timeout, a connection error, or 5xx:
                      the order MAY have executed on-chain. Must NOT retry before
                      verifying via /activity, or we double-fill (the Dallas bug).
    """
    import requests
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return "ambiguous"
    # py-clob-client / requests surface the HTTP status here (None on a request
    # exception that never got a response).
    code = getattr(exc, "status_code", None)
    if code is None:
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
    if code is None:
        return "ambiguous"          # request never completed cleanly
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "ambiguous"
    if 400 <= code < 500 and code not in (408, 429):
        return "clean"              # explicit reject → not placed
    return "ambiguous"              # 408/429/5xx → may have landed


def _verify_order_placed(token_id: str, expected_shares: float) -> tuple[str, str | None]:
    """
    After an ambiguous error, determine whether the order actually exists.
    Checks open-orders first (resting) then /activity BUY (filled).

    Returns (state, real_order_id_or_None) where state is:
      • "landed"  — order is on the book or a matching BUY is in /activity.
      • "absent"  — /activity was readable and shows NO matching BUY → safe retry.
      • "unknown" — verification couldn't run (no wallet / network error) → the
                    caller must NOT retry (avoid a possible double-fill).
    """
    # 1) Resting on the book? (best-effort; get_open_orders swallows its errors)
    try:
        for o in get_open_orders():
            if o.side.upper() == "BUY" and token_id in (o.market_id or ""):
                return "landed", (o.order_id or None)
    except Exception:
        pass
    # 2) Filled — authoritative check against /activity BUY for this token.
    try:
        from src.config.settings import settings
        from src.market.polymarket_data import get_activity, buy_shares_by_token
        wallet = settings.proxy_wallet_address
        if not wallet:
            return "unknown", None
        bought = buy_shares_by_token(get_activity(wallet)).get(token_id, 0.0)
        tol = max(0.5, expected_shares * 0.02)
        if bought >= (expected_shares - tol):
            return "landed", None
        return "absent", None
    except Exception:
        return "unknown", None


def place_limit_order(
    market_id: str,
    token_id: str,
    side: Literal["BUY", "SELL"],
    price: float,
    size: float,
    skip_risk_check: bool = False,
) -> str | None:
    """
    Place a GTC limit order via CLOB V2.
    Returns order_id on success, None on failure.
    skip_risk_check=True: caller already did a basket-level check (P1-7).
    """
    from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions, OrderType

    # ── Polymarket V2 minimum-size pre-checks ─────────────────────────────────
    if size < _MIN_SHARES:
        logger.warning(
            f"[order_executor] SKIPPED (below min shares): "
            f"market_id={market_id} side={side} price={price} size={size:.4f} "
            f"(min={_MIN_SHARES} shares)"
        )
        return None

    notional = price * size
    if notional < _MIN_USD:
        logger.warning(
            f"[order_executor] SKIPPED (below min notional): "
            f"market_id={market_id} side={side} price={price} size={size:.4f} "
            f"notional=${notional:.4f} (min=${_MIN_USD})"
        )
        return None
    # ──────────────────────────────────────────────────────────────────────────

    if not skip_risk_check:
        bet_size_approx = round(notional, 4)
        allowed, reason = risk_manager.can_place_order(bet_size_approx)
        if not allowed:
            logger.warning(
                f"[order_executor] place_limit_order blocked by RiskManager: "
                f"market_id={market_id} reason={reason}"
            )
            return None

    def _place():
        client = _get_client()
        tick_size = client.get_tick_size(token_id)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,  # "BUY" or "SELL" — V2 accepts both string and Side enum
        )
        return client.create_and_post_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size=tick_size),
            order_type=OrderType.GTC,
        )

    # G4-25: verify-aware placement. Never blindly retry an ambiguous error — a
    # status=None / timeout / 5xx order may have executed on-chain. Verify via
    # /activity (or open-orders) before retrying so a transient error can't turn
    # into a double-fill. Only BUY verification matters here (SELL is for exits).
    verify = (side.upper() == "BUY")
    last_err: Exception | None = None
    for attempt, delay in enumerate(_BACKOFF, start=1):
        try:
            resp = _place()
            order_id = (
                resp.get("orderID") or resp.get("id")
                if isinstance(resp, dict) else str(resp)
            )
            logger.info(
                f"[order_executor] ORDER PLACED: market_id={market_id} "
                f"side={side} price={price} size={size:.4f} order_id={order_id}"
            )
            return order_id
        except Exception as exc:
            last_err = exc
            kind = _place_error_kind(exc)
            logger.error(
                f"[order_executor] place attempt {attempt}/{len(_BACKOFF)} "
                f"[{kind}]: market_id={market_id} token={token_id[:12]}… "
                f"side={side} error={type(exc).__name__}: {str(exc)[:200]}"
            )

            if kind == "ambiguous" and verify:
                logger.warning(
                    f"[order_executor] ambiguous placement error — "
                    f"verifying via /activity before retry "
                    f"(token={token_id[:12]}… size={size:.4f})"
                )
                state, real_id = _verify_order_placed(token_id, size)
                if state == "landed":
                    oid = real_id or f"verified-onchain:{token_id[:16]}"
                    logger.warning(
                        f"[order_executor] order CONFIRMED on-chain despite error — "
                        f"recording, NOT retrying: market_id={market_id} order_id={oid}"
                    )
                    return oid
                if state == "unknown":
                    logger.error(
                        "[order_executor] could not verify placement — "
                        "skipping retry to avoid double-fill"
                    )
                    return None
                # state == "absent": /activity readable, no BUY → retry is safe.
                logger.info(
                    "[order_executor] no fill found on /activity — retry is safe"
                )

            if attempt < len(_BACKOFF):
                time.sleep(delay)

    logger.error(
        f"[order_executor] place_limit_order FAILED after {len(_BACKOFF)} attempts: "
        f"market_id={market_id} side={side} price={price} size={size} "
        f"error={type(last_err).__name__ if last_err else 'N/A'}: {str(last_err)[:200]}"
    )
    return None


def cancel_order(order_id: str) -> bool:
    """Cancel an open order. Returns True on success."""
    from py_clob_client_v2 import OrderPayload

    def _cancel():
        client = _get_client()
        return client.cancel_order(OrderPayload(orderID=order_id))

    try:
        _with_backoff(
            _cancel,
            module="order_executor",
            method="cancel_order",
        )
        logger.info(f"[order_executor] ORDER CANCELLED: order_id={order_id}")
        return True
    except Exception:
        return False


def get_order_status(order_id: str) -> Literal["open", "filled", "cancelled", "unknown"]:
    """Poll the CLOB V2 API for current order status."""
    def _get():
        client = _get_client()
        return client.get_order(order_id)

    try:
        resp = _with_backoff(
            _get,
            module="order_executor",
            method="get_order_status",
        )
        if isinstance(resp, dict):
            raw = (resp.get("status") or resp.get("orderStatus") or "").upper()
        else:
            raw = str(getattr(resp, "status", "")).upper()

        mapping = {
            "OPEN": "open",
            "LIVE": "open",
            "MATCHED": "filled",
            "FILLED": "filled",
            "CANCELLED": "cancelled",
            "CANCELED": "cancelled",
        }
        status = mapping.get(raw, "unknown")
        logger.debug(f"[order_executor] order_id={order_id} raw_status={raw} mapped={status}")
        return status
    except Exception:
        return "unknown"


def get_open_orders() -> list[Order]:
    """Return all currently open orders for this account."""
    from py_clob_client_v2 import OpenOrderParams

    def _get():
        client = _get_client()
        return client.get_open_orders(OpenOrderParams())

    try:
        resp = _with_backoff(
            _get,
            module="order_executor",
            method="get_open_orders",
        )
        orders = []
        raw_list = resp if isinstance(resp, list) else (resp.get("data") or [])
        for o in raw_list:
            if not isinstance(o, dict):
                continue
            orders.append(Order(
                order_id=o.get("id", ""),
                market_id=o.get("asset_id", o.get("market", "")),
                side=o.get("side", ""),
                price=float(o.get("price", 0)),
                size=float(o.get("original_size", o.get("size", 0))),
                status="open",
            ))
        logger.info(f"[order_executor] get_open_orders: {len(orders)} open orders")
        return orders
    except Exception:
        return []
