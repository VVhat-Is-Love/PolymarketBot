"""
Verify live order placement with working config:
  signature_type=3 (POLY_1271)
  funder=PROXY_WALLET_ADDRESS (0x9Ce3...)
  API key: 40c96989...

Places a tiny BUY @ very low price (safe, won't match), then cancels.
Run: python bugfixing/verify_live_order.py
"""
import sys, time
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
PROXY    = settings.proxy_wallet_address.strip()

creds = ApiCreds(
    api_key=settings.polymarket_api_key,
    api_secret=settings.polymarket_api_secret,
    api_passphrase=settings.polymarket_api_passphrase,
)

print(f"\n{'='*60}")
print("  Live order verification: POLY_1271 (sig_type=3)")
print(f"{'='*60}")
print(f"  HOST:  {HOST}")
print(f"  PROXY: {PROXY}")
print(f"  KEY:   {settings.polymarket_api_key[:12]}...")

# Build client exactly as polymarket_auth.py does
client = ClobClient(
    host=HOST, chain_id=CHAIN_ID,
    key=PK, creds=creds,
    signature_type=3,
    funder=PROXY,
)

# [1] Balance
print(f"\n[1] Balance check...")
try:
    resp = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw  = resp.get("balance", 0) if isinstance(resp, dict) else 0
    cash = float(raw) / 1e6
    print(f"    OK  balance: ${cash:.4f} pUSD")
except Exception as e:
    print(f"    ERR: {e}")
    sys.exit(1)

# [2] Find market
print(f"\n[2] Finding a liquid market...")
token_id = None; tick_size = "0.01"
try:
    result = client.get_sampling_simplified_markets()
    data = result.get("data", []) if isinstance(result, dict) else []
    for item in data:
        if not item.get("accepting_orders") or item.get("closed"):
            continue
        for t in item.get("tokens", []):
            tid = t.get("token_id"); price = float(t.get("price", 0))
            if tid and 0.05 < price < 0.95:
                token_id = tid
                tick_size = client.get_tick_size(tid)
                print(f"    token: {tid[:30]}... price={price} tick={tick_size}")
                break
        if token_id:
            break
except Exception as e:
    print(f"    ERR finding market: {e}")

if not token_id:
    print("    No market found — cannot test order placement")
    sys.exit(1)

# [3] Place tiny safe order (price=0.01 — very unlikely to match, will be cancelled)
# Minimum size is 5 shares per Polymarket API rules.
print(f"\n[3] Placing BUY 5 shares @ 0.01 (safe price, POLY_1271)...")
order_id = None
try:
    resp = client.create_and_post_order(
        order_args=OrderArgs(token_id=token_id, price=0.01, size=5, side="BUY"),
        options=PartialCreateOrderOptions(tick_size=tick_size),
        order_type=OrderType.GTC,
    )
    order_id = resp.get("orderID") or resp.get("id") if isinstance(resp, dict) else None
    status   = resp.get("status") if isinstance(resp, dict) else "?"
    print(f"    ORDER PLACED: id={order_id}  status={status}")
    print(f"    raw: {resp}")
except Exception as e:
    print(f"    FAILED: {type(e).__name__}: {e}")
    sys.exit(1)

# [4] Cancel immediately
if order_id:
    print(f"\n[4] Cancelling order {order_id}...")
    time.sleep(1)
    try:
        cancel_resp = client.cancel_order(OrderPayload(orderID=order_id))
        print(f"    CANCELLED: {cancel_resp}")
    except Exception as e:
        print(f"    Cancel ERR (not critical — order may self-expire): {e}")

print(f"\n{'='*60}")
print("  RESULT: ORDER PLACEMENT WORKS WITH POLY_1271")
print(f"  Config: signature_type=3, funder={PROXY}")
print(f"{'='*60}\n")
