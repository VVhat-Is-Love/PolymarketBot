"""
Diagnose V2 auth: intercept HTTP requests, show exact headers,
try all meaningful combinations including patched signer approach.
Run: python bugfixing/diagnose_v2.py
"""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

from src.config.settings import settings

DEPOSIT = settings.proxy_wallet_address.strip()  # 0x5817FCb2300Fb88bd36e30e6EC01FA595d466d4F
EOA     = settings.eoa_wallet_address.strip()
PK      = settings.private_key

print(f"EOA:     {EOA}")
print(f"DEPOSIT: {DEPOSIT}")
print(f"API_KEY: {settings.polymarket_api_key}")

from py_clob_client_v2 import ClobClient, ApiCreds, BalanceAllowanceParams, AssetType
from py_clob_client_v2.signer import Signer

creds = ApiCreds(
    api_key=settings.polymarket_api_key,
    api_secret=settings.polymarket_api_secret,
    api_passphrase=settings.polymarket_api_passphrase,
)

# ── Patch: create a signer that reports deposit wallet address ─
class DepositWalletSigner(Signer):
    """Reports deposit wallet as address() but signs with EOA private key."""
    def __init__(self, private_key: str, chain_id: int, deposit_wallet: str):
        super().__init__(private_key, chain_id)
        self._deposit_wallet = deposit_wallet

    def address(self):
        return self._deposit_wallet

patched_signer = DepositWalletSigner(PK, settings.chain_id, DEPOSIT)
print(f"\nPatched signer.address(): {patched_signer.address()}")
print(f"Patched signer.private_key[:8]: {patched_signer.private_key[:8]}...")


# ── Intercept HTTP requests to show headers ────────────────────
import py_clob_client_v2.http_helpers.helpers as _http
_original_request = _http.request

def _traced_request(endpoint, method, headers, data, params):
    print(f"\n>>> {method} {endpoint}")
    if headers:
        for k, v in headers.items():
            # Truncate long signature values
            display_v = v[:40] + "..." if len(str(v)) > 43 else v
            print(f"    {k}: {display_v}")
    try:
        result = _original_request(endpoint, method, headers, data, params)
        print(f"    ← OK")
        return result
    except Exception as e:
        print(f"    ← ERROR: {e}")
        raise

_http.request = _traced_request


# ── Test 1: sig_type=3 + patched signer (POLY_1271, deposit wallet address) ─
print(f"\n{'='*55}")
print("TEST 1: POLY_1271 + DepositWalletSigner (POLY_ADDRESS=deposit)")
print(f"{'='*55}")
try:
    client = ClobClient(
        host=settings.polymarket_host,
        chain_id=settings.chain_id,
        key=PK,
        creds=creds,
        signature_type=3,
        funder=DEPOSIT,
    )
    # Patch the signer after creation
    client.signer = patched_signer
    client.builder.signer = patched_signer
    # ExchangeOrderBuilderV2 also has a signer reference
    if hasattr(client, '_exchange_builder') and client._exchange_builder:
        client._exchange_builder.signer = patched_signer

    bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  Balance result: {bal}")

    raw_bal = bal.get("balance", 0) if isinstance(bal, dict) else 0
    cash = float(raw_bal) / 1e6
    print(f"  Cash balance: ${cash:.4f} pUSD")
except Exception as e:
    print(f"  FAILED: {e}")


# ── Test 2: try placing an order with patched signer ──────────
print(f"\n{'='*55}")
print("TEST 2: Place order (POLY_1271 + patched signer)")
print(f"{'='*55}")
try:
    from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions, OrderType

    # Get an active market
    m = client.get_sampling_simplified_markets()
    data = m.get("data", []) if isinstance(m, dict) else []
    token_id = None; tick_size = "0.01"
    for item in data:
        if item.get("accepting_orders") and not item.get("closed"):
            for t in item.get("tokens", []):
                tid = t.get("token_id"); p = t.get("price", 0)
                if tid and 0.05 < p < 0.95:
                    token_id = tid
                    tick_size = client.get_tick_size(tid)
                    print(f"  Market token: {tid[:25]}... price={p} tick={tick_size}")
                    break
        if token_id:
            break

    if not token_id:
        print("  No market found")
    else:
        resp = client.create_and_post_order(
            order_args=OrderArgs(token_id=token_id, price=0.02, size=50, side="BUY"),
            options=PartialCreateOrderOptions(tick_size=tick_size),
            order_type=OrderType.GTC,
        )
        print(f"  ORDER RESULT: {resp}")
except Exception as e:
    print(f"  FAILED: {type(e).__name__}: {e}")


print(f"\n{'='*55}")
print("DIAGNOSIS COMPLETE")
print(f"{'='*55}\n")
