"""
Health check — run once manually before starting the bot.

Usage:
    python -m src.scripts.health_check

Checks:
  - Polygon RPC connection
  - Wallet address derivation from private key
  - USDC balance (blockchain)
  - MATIC balance (gas)
  - CLOB Level 2 auth
  - Open orders count
"""
from __future__ import annotations

import sys


def _ok(label: str, value: str = "") -> None:
    suffix = f": {value}" if value else ""
    print(f"  \033[32m✅\033[0m {label}{suffix}")


def _fail(label: str, detail: str = "") -> None:
    suffix = f": {detail}" if detail else ""
    print(f"  \033[31m❌\033[0m {label}{suffix}")


def run() -> int:
    """Returns 0 on full pass, 1 if any check failed."""
    failed = False
    print("\n" + "=" * 55)
    print("  Polymarket Bot — Health Check")
    print("=" * 55)

    # ── Settings ────────────────────────────────────────────
    try:
        from src.config.settings import settings
    except Exception as exc:
        _fail("Settings load", str(exc))
        return 1

    # ── Private key / wallet ────────────────────────────────
    try:
        from eth_account import Account
        wallet = Account.from_key(settings.private_key).address
        _ok("Wallet address", wallet)
    except Exception as exc:
        _fail("Wallet address (PRIVATE_KEY invalid?)", str(exc))
        wallet = settings.proxy_wallet_address or ""
        failed = True

    # ── Polygon RPC + USDC balance ──────────────────────────
    try:
        from src.blockchain.balance import get_usdc_balance, get_matic_balance, _get_web3
        w3 = _get_web3()
        rpc_url = w3.provider.endpoint_uri  # type: ignore[attr-defined]
        _ok("RPC connected", str(rpc_url))

        usdc = get_usdc_balance(wallet)
        _ok("USDC balance", f"${usdc:.2f}")

        matic = get_matic_balance(wallet)
        label = "MATIC balance"
        if matic < 0.1:
            _fail(label, f"{matic:.4f} MATIC — LOW, transactions may fail")
            failed = True
        else:
            _ok(label, f"{matic:.4f} MATIC")

    except Exception as exc:
        _fail("Blockchain (set POLYGON_RPC_URL in .env)", str(exc))
        failed = True

    # ── CLOB Level 2 auth ───────────────────────────────────
    try:
        from src.blockchain.polymarket_auth import get_clob_client
        client = get_clob_client()

        # Verify auth by fetching open orders (L2 required)
        from py_clob_client.clob_types import OpenOrderParams
        orders = client.get_orders(OpenOrderParams())
        count = len(orders) if isinstance(orders, list) else "?"
        _ok("CLOB Level 2 auth", f"open orders: {count}")
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "Unauthorized" in msg or "Invalid api key" in msg:
            _fail(
                "CLOB auth (run: python -m src.scripts.derive_creds)",
                "API key invalid or missing",
            )
        else:
            _fail("CLOB auth", msg)
        failed = True

    # ── Trading mode ────────────────────────────────────────
    _ok("Trading mode", settings.trading_mode.upper())

    print("=" * 55)
    if failed:
        print("  \033[31mSome checks FAILED — fix issues above before running the bot.\033[0m")
        print("=" * 55 + "\n")
        return 1
    else:
        print("  \033[32mAll checks passed — bot is ready.\033[0m")
        print("=" * 55 + "\n")
        return 0


if __name__ == "__main__":
    sys.exit(run())
