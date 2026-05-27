"""
Polymarket CLOB V2 client factory.
Uses py-clob-client-v2 (1.0.1+). Supports Level 1 and Level 2 auth.

Working configuration (confirmed 2026-05-25):
  signature_type=3  (POLY_1271 — ERC-1271 via proxy/deposit wallet)
  funder=PROXY_WALLET_ADDRESS (0x9Ce3E9a1e408B2c635674020db6Ab685294BAC2B)
  API key bound to EOA: 40c96989-75a0-df6f-76cf-73decc053622
  Result: orders placed, status=matched, real blockchain transactions confirmed.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from py_clob_client_v2 import ClobClient as _ClobClientType

_lock = threading.Lock()
_client: "_ClobClientType | None" = None


def get_clob_client() -> "_ClobClientType":
    """Return the singleton V2 ClobClient. Thread-safe."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        from src.config.settings import settings
        from py_clob_client_v2 import ClobClient

        if not settings.private_key:
            raise RuntimeError("PRIVATE_KEY is not set — cannot initialise ClobClient")

        has_creds = bool(
            settings.polymarket_api_key
            and settings.polymarket_api_secret
            and settings.polymarket_api_passphrase
        )

        # POLY_1271 (3): ERC-1271 signature. EOA signs on behalf of the proxy wallet.
        # funder = proxy wallet address registered as maker in V2 exchange.
        # Confirmed working: order.signer=proxy_wallet, signature validated via ERC-1271.
        proxy = settings.proxy_wallet_address or None

        if has_creds:
            from py_clob_client_v2 import ApiCreds
            creds = ApiCreds(
                api_key=settings.polymarket_api_key,
                api_secret=settings.polymarket_api_secret,
                api_passphrase=settings.polymarket_api_passphrase,
            )
            _client = ClobClient(
                host=settings.polymarket_host,
                chain_id=settings.chain_id,
                key=settings.private_key,
                creds=creds,
                signature_type=3,   # POLY_1271 — confirmed working
                funder=proxy,
            )
            level = "Level 2 (can place orders + query balance)"
        else:
            _client = ClobClient(
                host=settings.polymarket_host,
                chain_id=settings.chain_id,
                key=settings.private_key,
                signature_type=3,   # POLY_1271
                funder=proxy,
            )
            level = "Level 1 (read-only — add API credentials to place orders)"

        logger.info(
            f"ClobClient V2 initialised: host={settings.polymarket_host} "
            f"chain_id={settings.chain_id} "
            f"funder={proxy} "
            f"auth={level}"
        )
        return _client


def reset_clob_client() -> None:
    """For testing — allow re-initialisation."""
    global _client
    with _lock:
        _client = None


def derive_api_credentials() -> None:
    """
    Derive Level 2 API credentials from your private key (one-time setup).

    Run this once:  python -c "from src.blockchain.polymarket_auth import derive_api_credentials; derive_api_credentials()"

    Prints POLYMARKET_API_KEY / _SECRET / _PASSPHRASE to stdout.
    Copy them into .env — do NOT run this again after saving (it may rotate the key).
    """
    from src.config.settings import settings
    from py_clob_client_v2 import ClobClient

    if not settings.private_key:
        raise RuntimeError("PRIVATE_KEY is not set in .env")

    client = ClobClient(
        host=settings.polymarket_host,
        chain_id=settings.chain_id,
        key=settings.private_key,
    )

    creds = client.create_or_derive_api_key()

    print("\n" + "=" * 60)
    print("  Polymarket API credentials (copy to .env)")
    print("=" * 60)
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    print("=" * 60)
    print("  Save these to .env and restart the bot.")
    print("=" * 60 + "\n")
