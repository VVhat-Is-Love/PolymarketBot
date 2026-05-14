"""
V2 order placement test.
Places a $1 limit BUY at price 0.02 (will never fill), waits 3s, cancels it.
Run from repo root: python bugfixing/test_order_v2.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from py_clob_client_v2 import (
    ClobClient, ApiCreds, OrderArgs, OrderPayload,
    PartialCreateOrderOptions, OrderType, OpenOrderParams,
    BalanceAllowanceParams, AssetType,
)

from src.config.settings import settings

HOST = settings.polymarket_host
CHAIN_ID = settings.chain_id
PK = settings.private_key

print(f"\n{'='*55}")
print("  Polymarket CLOB V2 — Order Test")
print(f"{'='*55}")
print(f"  Host:     {HOST}")
print(f"  Chain ID: {CHAIN_ID}")

# ── Step 1: init client ──────────────────────────────────────
print("\n[1] Initialising V2 client...")
creds = ApiCreds(
    api_key=settings.polymarket_api_key,
    api_secret=settings.polymarket_api_secret,
    api_passphrase=settings.polymarket_api_passphrase,
)
client = ClobClient(
    host=HOST,
    chain_id=CHAIN_ID,
    key=PK,
    creds=creds,
    signature_type=1,                       # POLY_PROXY: EOA signs for proxy wallet
    funder=settings.proxy_wallet_address,   # proxy wallet is the maker
)
print(f"    OK — EOA: {client.get_address()}")
print(f"    Proxy (funder): {settings.proxy_wallet_address}")

# ── Step 2: check balance ────────────────────────────────────
print("\n[2] Checking CLOB balance (pUSD)...")
try:
    resp = client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    raw_bal = resp.get("balance", 0) if isinstance(resp, dict) else 0
    cash = float(raw_bal) / 1e6
    print(f"    Balance: ${cash:.2f} pUSD")
except Exception as e:
    print(f"    WARNING: balance check failed: {e}")

# ── Step 3: find an active market via sampling endpoint ──────
print("\n[3] Finding an active market (sampling markets)...")
token_id = None
tick_size = "0.01"

try:
    # get_sampling_simplified_markets returns markets actively used for rewards/sampling
    result = client.get_sampling_simplified_markets()
    data = result.get("data", []) if isinstance(result, dict) else (result or [])
    for item in data:
        if not item.get("accepting_orders") or item.get("closed"):
            continue
        for t in item.get("tokens", []):
            tid = t.get("token_id")
            price = t.get("price", 0)
            if tid and 0.01 < price < 0.99:
                token_id = tid
                tick_size = client.get_tick_size(tid)
                print(f"    Market:    {str(item.get('condition_id', ''))[:20]}...")
                print(f"    Token ID:  {tid[:25]}...")
                print(f"    Price:     {price}")
                print(f"    Tick size: {tick_size}")
                break
        if token_id:
            break
except Exception as e:
    print(f"    ERROR in sampling markets: {e}")

if not token_id:
    print("    Falling back to paginated scan...")
    try:
        cursor = "MA=="
        while cursor and not token_id:
            m = client.get_simplified_markets(next_cursor=cursor)
            for item in (m.get("data", []) if isinstance(m, dict) else []):
                if item.get("accepting_orders") and not item.get("closed"):
                    for t in item.get("tokens", []):
                        tid = t.get("token_id")
                        p = t.get("price", 0)
                        if tid and 0.01 < p < 0.99:
                            token_id = tid
                            tick_size = client.get_tick_size(tid)
                            print(f"    Token ID:  {tid[:25]}... price={p} tick={tick_size}")
                            break
                if token_id:
                    break
            nc = m.get("next_cursor") if isinstance(m, dict) else None
            if not nc or nc == cursor:
                break
            cursor = nc
    except Exception as e:
        print(f"    Scan error: {e}")

if not token_id:
    print("    ERROR: no active market found — cannot run test")
    sys.exit(1)

# ── Step 4: place limit order ────────────────────────────────
print("\n[4] Placing $1 limit BUY @ 0.02 (will never fill)...")
order_id = None
try:
    size = round(1.0 / 0.02, 0)  # $1 / $0.02 = 50 shares
    resp = client.create_and_post_order(
        order_args=OrderArgs(
            token_id=token_id,
            price=0.02,
            size=size,
            side="BUY",
        ),
        options=PartialCreateOrderOptions(tick_size=tick_size),
        order_type=OrderType.GTC,
    )
    if isinstance(resp, dict):
        order_id = resp.get("orderID") or resp.get("id")
    print(f"    ORDER PLACED: order_id={order_id}")
    print(f"    Raw response: {resp}")
except Exception as e:
    if "maker address not allowed" in str(e):
        print(f"    WALLET NOT REGISTERED IN V2 EXCHANGE")
        print(f"    Action required: go to polymarket.com, connect MetaMask ({settings.eoa_wallet_address}),")
        print(f"    and complete the deposit/onboarding flow to register your address.")
        print(f"    After that, re-run this script.")
    else:
        print(f"    ERROR placing order: {type(e).__name__}: {e}")
    sys.exit(1)

if not order_id:
    print("    ERROR: no order_id returned in response")
    sys.exit(1)

# ── Step 5: wait 3 seconds ───────────────────────────────────
print("\n[5] Waiting 3 seconds...")
time.sleep(3)

# ── Step 6: check order status ──────────────────────────────
print("\n[6] Checking order status...")
try:
    status_resp = client.get_order(order_id)
    print(f"    Status response: {status_resp}")
except Exception as e:
    print(f"    WARNING: get_order failed: {e}")

# ── Step 7: cancel ──────────────────────────────────────────
print("\n[7] Cancelling order...")
try:
    cancel_resp = client.cancel_order(OrderPayload(orderID=order_id))
    print(f"    CANCELLED: {cancel_resp}")
except Exception as e:
    print(f"    ERROR cancelling: {type(e).__name__}: {e}")

# ── Step 8: verify open orders ──────────────────────────────
print("\n[8] Verifying open orders after cancel...")
try:
    open_orders = client.get_open_orders(OpenOrderParams())
    count = len(open_orders) if isinstance(open_orders, list) else "?"
    print(f"    Open orders remaining: {count}")
except Exception as e:
    print(f"    WARNING: get_open_orders failed: {e}")

print(f"\n{'='*55}")
print("  TEST COMPLETE")
print(f"{'='*55}\n")
