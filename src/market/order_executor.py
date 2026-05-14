"""
Order execution via Polymarket CLOB V2 API.
Exponential backoff: 3 attempts with 1s / 2s / 4s delays.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from loguru import logger

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


def place_limit_order(
    market_id: str,
    token_id: str,
    side: Literal["BUY", "SELL"],
    price: float,
    size: float,
) -> str | None:
    """
    Place a GTC limit order via CLOB V2.
    Returns order_id on success, None on failure.
    """
    from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions, OrderType
    from src.risk.risk_manager import risk_manager

    bet_size_approx = round(price * size, 4)
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

    try:
        resp = _with_backoff(
            _place,
            module="order_executor",
            method="place_limit_order",
            market_id=market_id,
        )
        order_id = resp.get("orderID") or resp.get("id") if isinstance(resp, dict) else str(resp)
        logger.info(
            f"[order_executor] ORDER PLACED: market_id={market_id} "
            f"side={side} price={price} size={size:.4f} order_id={order_id}"
        )
        return order_id
    except Exception:
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
