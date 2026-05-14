"""
Polygon blockchain balance reader.
- get_usdc_balance: native USDC on EOA (MetaMask wallet)
- get_pusd_balance: pUSD (PolyUSD) on-chain balance for proxy/EOA wallet
- get_polymarket_cash_balance: CLOB V2 API balance (funds inside the exchange)
Falls back through public RPCs if POLYGON_RPC_URL fails or is unset.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from loguru import logger

USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

_USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]

_PUBLIC_RPCS = [
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
]

_w3_lock = threading.Lock()
_w3 = None


def _get_web3():
    global _w3
    from web3 import Web3

    if _w3 is not None:
        try:
            if _w3.is_connected():
                return _w3
        except Exception:
            pass

    with _w3_lock:
        from src.config.settings import settings

        rpc_list = ([settings.polygon_rpc_url] if settings.polygon_rpc_url else []) + _PUBLIC_RPCS

        for rpc in rpc_list:
            try:
                candidate = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                if candidate.is_connected():
                    logger.info(f"[balance] RPC connected: {rpc}")
                    _w3 = candidate
                    return _w3
            except Exception as exc:
                logger.debug(f"[balance] RPC {rpc} unavailable: {exc}")

        raise RuntimeError(
            "No Polygon RPC available. "
            "Set POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY in .env"
        )


def get_usdc_balance(wallet_address: str) -> float:
    """
    Read native USDC balance for wallet_address directly from Polygon.
    Returns amount in USD (float). Retries 3× with exponential backoff.
    """
    from web3 import Web3

    checksum = Web3.to_checksum_address(wallet_address)
    for attempt in range(3):
        try:
            w3 = _get_web3()
            usdc = w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=_USDC_ABI,
            )
            raw = usdc.functions.balanceOf(checksum).call()
            decimals = usdc.functions.decimals().call()
            return raw / (10 ** decimals)
        except Exception as exc:
            logger.warning(f"[balance] USDC read attempt {attempt + 1}/3 failed: {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return 0.0


def get_matic_balance(wallet_address: str) -> float:
    """Read MATIC balance (needed for gas). Warns if below 0.1."""
    from web3 import Web3

    try:
        w3 = _get_web3()
        wei = w3.eth.get_balance(Web3.to_checksum_address(wallet_address))
        matic = float(w3.from_wei(wei, "ether"))
        if matic < 0.1:
            logger.warning(
                f"[balance] Low MATIC balance ({matic:.4f} MATIC) — transactions may fail"
            )
        return matic
    except Exception as exc:
        logger.warning(f"[balance] MATIC balance failed: {exc}")
        return 0.0

# pUSD (PolyUSD) — Polymarket V2 collateral token on Polygon
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


def get_pusd_balance(wallet_address: str) -> float:
    """Read pUSD (PolyUSD) on-chain balance. Polymarket V2 stores deposits in pUSD."""
    from web3 import Web3

    checksum = Web3.to_checksum_address(wallet_address)
    for attempt in range(3):
        try:
            w3 = _get_web3()
            pusd = w3.eth.contract(
                address=Web3.to_checksum_address(PUSD_ADDRESS),
                abi=_USDC_ABI,
            )
            raw = pusd.functions.balanceOf(checksum).call()
            decimals = pusd.functions.decimals().call()
            return raw / (10 ** decimals)
        except Exception as exc:
            logger.warning(f"[balance] pUSD read attempt {attempt + 1}/3 failed: {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return 0.0


def get_polymarket_cash_balance(client) -> float:
    """
    Read USDC balance held inside Polymarket exchange via CLOB V2 API.
    Returns amount in USD. Uses get_balance_allowance (COLLATERAL asset type).
    """
    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if isinstance(resp, dict):
            raw = resp.get("balance") or resp.get("allowance") or 0
            return float(raw) / 1e6
    except Exception as exc:
        logger.warning(f"[balance] CLOB cash balance failed: {exc}")
    return 0.0