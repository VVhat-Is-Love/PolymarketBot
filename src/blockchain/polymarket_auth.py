"""
Polymarket CLOB client factory — Level 1 auth (private key, no API secret required).
Chain ID 137 = Polygon mainnet.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from py_clob_client.client import ClobClient as _ClobClientType

_lock = threading.Lock()
_client: "_ClobClientType | None" = None


def get_clob_client() -> "_ClobClientType":
    """Return the singleton ClobClient (Level 1 auth).  Thread-safe."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        from src.config.settings import settings
        from py_clob_client.client import ClobClient

        if not settings.private_key:
            raise RuntimeError("PRIVATE_KEY is not set — cannot initialise ClobClient")

        _client = ClobClient(
            host=settings.polymarket_host,
            chain_id=settings.chain_id,
            key=settings.private_key,
            # creds=None → Level 1 (private key signs orders; no API secret needed)
        )
        logger.info(
            f"ClobClient initialised: host={settings.polymarket_host} "
            f"chain_id={settings.chain_id} "
            f"wallet={settings.proxy_wallet_address[:10]}…"
        )
        return _client


def reset_clob_client() -> None:
    """For testing — allow re-initialisation."""
    global _client
    with _lock:
        _client = None
