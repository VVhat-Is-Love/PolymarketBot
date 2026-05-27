"""
Trace exact HTTP headers and order payload for POLY_1271 with DepositWalletSigner.
Intercept the request to see exactly what gets sent.

Run: python bugfixing/trace_1271.py
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(override=True)

from src.config.settings import settings

HOST     = settings.polymarket_host
CHAIN_ID = settings.chain_id
PK       = settings.private_key
if not PK.startswith("0x"):
    PK_HEX = "0x" + PK
else:
    PK_HEX = PK

EOA     = settings.eoa_wallet_address.strip()
DEPOSIT = settings.proxy_wallet_address.strip()

# ── Intercept HTTP to log request details ─────────────────────────
import py_clob_client_v2.http_helpers.helpers as _http
_orig = _http.request

def _trace(endpoint, method, headers, data, params):
    print(f"\n>>> {method} {endpoint.replace(HOST, '')}")
    if headers:
        for k, v in sorted(headers.items()):
            display = str(v)[:60] + "..." if len(str(v)) > 63 else str(v)
            print(f"  {k}: {display}")
    if data and "order" in str(data)[:100]:
        try:
            d = json.loads(data) if isinstance(data, str) else data
            print(f"  owner: {d.get('owner', '?')}")
            o = d.get("order", {})
            print(f"  order.signer: {o.get('signer', '?')}")
            print(f"  order.maker:  {o.get('maker', '?')}")
            print(f"  order.signatureType: {o.get('signatureType', '?')}")
            print(f"  order.signature: {str(o.get('signature', ''))[:40]}...")
        except Exception:
            pass
    try:
        result = _orig(endpoint, method, headers, data, params)
        print(f"  -> OK: {str(result)[:100]}")
        return result
    except Exception as e:
        print(f"  -> ERR: {e}")
        raise

_http.request = _trace

from py_clob_client_v2 import ClobClient, ApiCreds, OrderArgs, PartialCreateOrderOptions, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client_v2.signer import Signer

class DepositWalletSigner(Signer):
    def __init__(self, pk, cid, deposit):
        super().__init__(pk, cid)
        self._deposit = deposit
    def address(self):
        return self._deposit

# Use the correct EOA API key
creds = ApiCreds(
    api_key="40c96989-75a0-df6f-76cf-73decc053622",
    api_secret="5JH-8s1HZN18-pACj5kGXcjC6tM9ypwRFMiL8RpcR4s=",
    api_passphrase="2eb9e6801821f6dabad92e5ceebcf1258cfb695ec2b615e0eb70ff0e8064f9a8",
)

# Patch signer from the start (before ClobClient is built)
patched_signer = DepositWalletSigner(PK_HEX, CHAIN_ID, DEPOSIT)
print(f"patched_signer.address(): {patched_signer.address()}")
print(f"patched_signer._deposit:  {patched_signer._deposit}")

print(f"\n{'='*60}")
print("TEST A: ClobClient with POLY_1271 + DepositWalletSigner baked in")
print(f"{'='*60}")

client_a = ClobClient(
    host=HOST, chain_id=CHAIN_ID,
    key=None,        # don't use key= so we can inject our own signer
    creds=creds,
    signature_type=3,
    funder=DEPOSIT,
)
# Manually inject the patched signer (key=None means signer=None by default)
client_a.signer         = patched_signer
client_a.builder.signer = patched_signer
# Rebuild the mode (was L0 without signer, now L2)
from py_clob_client_v2.constants import L2
client_a.mode = L2

print(f"\nclient_a.get_address(): {client_a.get_address()}")

# Balance (uses patched signer -> POLY_ADDRESS=deposit_wallet)
print(f"\n--- Balance check ---")
try:
    resp = client_a.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw = resp.get("balance", 0) if isinstance(resp, dict) else 0
    print(f"Balance: ${float(raw)/1e6:.4f} pUSD")
except Exception as e:
    print(f"Balance err: {e}")

# Find a market
print(f"\n--- Finding market ---")
token_id = None; tick_size = "0.01"
try:
    result = client_a.get_sampling_simplified_markets()
    data = result.get("data", []) if isinstance(result, dict) else []
    for item in data:
        if not item.get("accepting_orders") or item.get("closed"):
            continue
        for t in item.get("tokens", []):
            tid = t.get("token_id"); price = float(t.get("price", 0))
            if tid and 0.03 < price < 0.97:
                token_id = tid
                tick_size = client_a.get_tick_size(tid)
                print(f"token_id: {tid[:30]}... tick={tick_size}")
                break
        if token_id: break
except Exception as e:
    print(f"market err: {e}")

if not token_id:
    print("No market found")
    sys.exit(1)

# Place order
print(f"\n--- Placing order (POLY_1271, DepositWalletSigner) ---")
print(f"Expected POLY_ADDRESS header: {patched_signer.address()}")
print(f"Expected order.signer:        {DEPOSIT}")
try:
    resp = client_a.create_and_post_order(
        order_args=OrderArgs(token_id=token_id, price=0.02, size=50, side="BUY"),
        options=PartialCreateOrderOptions(tick_size=tick_size),
        order_type=OrderType.GTC,
    )
    print(f"SUCCESS: {resp}")
except Exception as e:
    print(f"FAILED: {e}")

print(f"\n{'='*60}")
print("TEST B: Try empty owner field (patch order_to_json_v2)")
print(f"{'='*60}")

import py_clob_client_v2.order_utils.model.order_data_v2 as _odv2
_orig_to_json = _odv2.order_to_json_v2

def _patched_to_json(order, owner, order_type, post_only=False, defer_exec=False):
    result = _orig_to_json(order, owner, order_type, post_only, defer_exec)
    print(f"  [patch] original owner: {owner}")
    result["owner"] = ""   # empty owner — skip server-side check?
    print(f"  [patch] patched owner: ''")
    return result

_odv2.order_to_json_v2 = _patched_to_json

# Need to reload in client module too
import py_clob_client_v2.client as _client_mod
_client_mod.order_to_json_v2 = _patched_to_json

try:
    resp = client_a.create_and_post_order(
        order_args=OrderArgs(token_id=token_id, price=0.02, size=50, side="BUY"),
        options=PartialCreateOrderOptions(tick_size=tick_size),
        order_type=OrderType.GTC,
    )
    print(f"SUCCESS: {resp}")
except Exception as e:
    print(f"FAILED: {e}")

print(f"\n{'='*60}")
print("DONE")
print(f"{'='*60}\n")
