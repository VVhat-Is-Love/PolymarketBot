"""
Fix and test Polymarket V2 order flow.

Steps:
1. Create fresh API key bound to EOA
2. Test L2 auth (get_balance_allowance)
3. Try POLY_PROXY order placement (signature_type=1, maker=deposit_wallet, signer=EOA)
4. If that fails, try POLY_1271 (signature_type=3, maker=signer=deposit_wallet)

Run: python bugfixing/fix_and_test.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

from src.config.settings import settings
from py_clob_client_v2 import (
    ClobClient, ApiCreds, OrderArgs, PartialCreateOrderOptions,
    OrderType, OpenOrderParams, BalanceAllowanceParams, AssetType, OrderPayload,
)

HOST     = settings.polymarket_host
CHAIN_ID = settings.chain_id
PK       = settings.private_key
EOA      = settings.eoa_wallet_address.strip()
DEPOSIT  = settings.proxy_wallet_address.strip()

print(f"\n{'='*62}")
print("  Polymarket CLOB V2 -- Fix & Test")
print(f"{'='*62}")
print(f"  EOA:      {EOA}")
print(f"  DEPOSIT:  {DEPOSIT}")
print(f"  Host:     {HOST}")
print(f"  Chain:    {CHAIN_ID}")


def _find_market(client):
    try:
        result = client.get_sampling_simplified_markets()
        data = result.get("data", []) if isinstance(result, dict) else []
        for item in data:
            if not item.get("accepting_orders") or item.get("closed"):
                continue
            for t in item.get("tokens", []):
                tid   = t.get("token_id")
                price = float(t.get("price", 0))
                if tid and 0.03 < price < 0.97:
                    ts = client.get_tick_size(tid)
                    return tid, ts
    except Exception as exc:
        print(f"    sampling error: {exc}")
    return None, None


def _place_order(client, token_id, tick_size):
    size = round(1.0 / 0.02)
    resp = client.create_and_post_order(
        order_args=OrderArgs(token_id=token_id, price=0.02, size=size, side="BUY"),
        options=PartialCreateOrderOptions(tick_size=tick_size),
        order_type=OrderType.GTC,
    )
    oid = resp.get("orderID") or resp.get("id") if isinstance(resp, dict) else None
    return oid, resp


def _cancel(client, order_id):
    time.sleep(2)
    try:
        r = client.cancel_order(OrderPayload(orderID=order_id))
        print(f"    CANCELLED: {r}")
    except Exception as exc:
        print(f"    cancel error: {exc}")


# ── Step 1: create fresh API key ─────────────────────────────────
print(f"\n[1] Creating fresh API key (EOA signer, nonce=0)...")
l1_client = ClobClient(host=HOST, chain_id=CHAIN_ID, key=PK)
new_creds  = None

for nonce in [0, 1, 2]:
    try:
        try:
            new_creds = l1_client.create_api_key(nonce=nonce)
            print(f"    create_api_key(nonce={nonce}) OK")
        except Exception as ce:
            print(f"    create(nonce={nonce}) failed ({ce}), trying derive...")
            new_creds = l1_client.derive_api_key(nonce=nonce)
            print(f"    derive_api_key(nonce={nonce}) OK")

        test_client = ClobClient(host=HOST, chain_id=CHAIN_ID, key=PK, creds=new_creds)
        keys_resp = test_client.get_api_keys()
        print(f"    get_api_keys: {str(keys_resp)[:120]}")
        break
    except Exception as e:
        print(f"    nonce={nonce} invalid: {e}")
        new_creds = None

if new_creds is None:
    print("    FATAL: could not obtain a working API key")
    sys.exit(1)

print(f"\n  --- COPY THESE INTO .env ---")
print(f"  POLYMARKET_API_KEY={new_creds.api_key}")
print(f"  POLYMARKET_API_SECRET={new_creds.api_secret}")
print(f"  POLYMARKET_API_PASSPHRASE={new_creds.api_passphrase}")
print(f"  ----------------------------\n")


# ── Step 2: POLY_PROXY (signature_type=1) ────────────────────────
print(f"[2] POLY_PROXY (sig_type=1, funder={DEPOSIT[:20]}...)")
print(f"    order.maker  = {DEPOSIT}")
print(f"    order.signer = {EOA}")

client_proxy = ClobClient(
    host=HOST, chain_id=CHAIN_ID,
    key=PK, creds=new_creds,
    signature_type=1,
    funder=DEPOSIT,
)

print(f"\n  [2a] L2 auth / balance check...")
try:
    resp = client_proxy.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    raw  = resp.get("balance", 0) if isinstance(resp, dict) else 0
    cash = float(raw) / 1e6
    print(f"      OK -- balance: ${cash:.4f} pUSD")
except Exception as e:
    print(f"      FAILED: {type(e).__name__}: {e}")
    sys.exit(1)

try:
    orders = client_proxy.get_open_orders(OpenOrderParams())
    print(f"      Open orders: {len(orders) if isinstance(orders, list) else '?'}")
except Exception as e:
    print(f"      get_open_orders warning: {e}")

print(f"\n  [2b] Finding active market...")
token_id, tick_size = _find_market(client_proxy)
if not token_id:
    print(f"      No suitable market")
    sys.exit(1)
print(f"      token_id={token_id[:30]}... tick={tick_size}")

print(f"\n  [2c] Placing $1 BUY @ 0.02...")
proxy_worked = False
try:
    order_id, raw = _place_order(client_proxy, token_id, tick_size)
    print(f"      ORDER PLACED  id={order_id}")
    print(f"      raw={raw}")
    proxy_worked = True
    _cancel(client_proxy, order_id)
except Exception as e:
    err = str(e)
    print(f"      FAILED: {type(e).__name__}: {err[:200]}")
    if "maker address not allowed" in err:
        print(f"      => V2 exchange requires deposit wallet flow (POLY_1271)")
    elif "order signer" in err.lower() or "api key" in err.lower():
        print(f"      => Signer/API-key mismatch - trying POLY_1271 next...")


# ── Step 3: POLY_1271 (signature_type=3) ─────────────────────────
if not proxy_worked:
    print(f"\n[3] POLY_1271 (sig_type=3, funder={DEPOSIT[:20]}...)")
    print(f"    order.maker  = {DEPOSIT}")
    print(f"    order.signer = {DEPOSIT}  (ERC-7739 wrapped sig from EOA key)")

    client_1271 = ClobClient(
        host=HOST, chain_id=CHAIN_ID,
        key=PK, creds=new_creds,
        signature_type=3,
        funder=DEPOSIT,
    )

    print(f"\n  [3a] L2 auth / balance check...")
    try:
        resp = client_1271.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        raw  = resp.get("balance", 0) if isinstance(resp, dict) else 0
        cash = float(raw) / 1e6
        print(f"      OK -- balance: ${cash:.4f} pUSD")
    except Exception as e:
        print(f"      FAILED: {type(e).__name__}: {e}")

    print(f"\n  [3b] Placing $1 BUY @ 0.02 (POLY_1271)...")
    try:
        order_id, raw = _place_order(client_1271, token_id, tick_size)
        print(f"      ORDER PLACED  id={order_id}")
        print(f"      raw={raw}")
        print(f"\n  SUCCESS: POLY_1271 works!")
        _cancel(client_1271, order_id)
        proxy_worked = True   # reuse flag as "anything worked"
    except Exception as e:
        err = str(e)
        print(f"      FAILED: {type(e).__name__}: {err[:300]}")

        # Subtest 3c: DepositWalletSigner (POLY_ADDRESS = deposit_wallet in L2 headers)
        print(f"\n  [3c] Retry with DepositWalletSigner (POLY_ADDRESS=deposit_wallet)...")
        from py_clob_client_v2.signer import Signer

        class DepositWalletSigner(Signer):
            def __init__(self, pk, cid, deposit):
                super().__init__(pk, cid)
                self._deposit = deposit
            def address(self):
                return self._deposit

        patched = DepositWalletSigner(PK, CHAIN_ID, DEPOSIT)
        client_1271.signer         = patched
        client_1271.builder.signer = patched

        try:
            order_id, raw = _place_order(client_1271, token_id, tick_size)
            print(f"      ORDER PLACED  id={order_id}")
            print(f"      SUCCESS: DepositWalletSigner + POLY_1271 works!")
            _cancel(client_1271, order_id)
            proxy_worked = True
        except Exception as e2:
            print(f"      FAILED: {type(e2).__name__}: {str(e2)[:300]}")
            print(f"\n  ALL APPROACHES FAILED.")
            print(f"  See GitHub issues #58, #64, #65 for the open SDK bug.")
            sys.exit(1)

print(f"\n{'='*62}")
print(f"  SUCCESS!")
print(f"  Update .env with the API credentials shown above.")
print(f"{'='*62}\n")
