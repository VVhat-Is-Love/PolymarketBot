"""
Test POLY_GNOSIS_SAFE (signature_type=2) for deposit wallet orders.

With sig_type=2:
  order.signer = EOA  (matches API_KEY.owner -> server check passes)
  order.maker  = deposit_wallet (funder, registered in V2)
  signature    = standard EIP-712 from EOA
  exchange     validates: EOA is authorized signer for the Safe (deposit_wallet)

If the deposit wallet (Safe) has EOA as its owner, sig_type=2 may work.

Run: python bugfixing/test_gnosis_safe.py
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
EOA      = settings.eoa_wallet_address.strip()
DEPOSIT  = settings.proxy_wallet_address.strip()

# The correct API key for EOA
creds = ApiCreds(
    api_key="40c96989-75a0-df6f-76cf-73decc053622",
    api_secret="5JH-8s1HZN18-pACj5kGXcjC6tM9ypwRFMiL8RpcR4s=",
    api_passphrase="2eb9e6801821f6dabad92e5ceebcf1258cfb695ec2b615e0eb70ff0e8064f9a8",
)

print(f"\n{'='*60}")
print("  POLY_GNOSIS_SAFE (sig_type=2) Test")
print(f"{'='*60}")
print(f"  EOA:     {EOA}")
print(f"  DEPOSIT: {DEPOSIT}")
print(f"  order.signer = EOA (matches API_KEY.owner)")
print(f"  order.maker  = DEPOSIT (funder)")

client = ClobClient(
    host=HOST, chain_id=CHAIN_ID,
    key=PK, creds=creds,
    signature_type=2,   # POLY_GNOSIS_SAFE
    funder=DEPOSIT,
)
print(f"\n  client.get_address(): {client.get_address()}")

# L2 auth check
print(f"\n[1] Balance (sig_type=2, POLY_ADDRESS=EOA)...")
try:
    resp = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw  = resp.get("balance", 0) if isinstance(resp, dict) else 0
    cash = float(raw) / 1e6
    print(f"    OK  balance: ${cash:.4f} pUSD")
except Exception as e:
    print(f"    ERR: {e}")

# Find market
print(f"\n[2] Finding market...")
token_id = None; tick_size = "0.01"
try:
    result = client.get_sampling_simplified_markets()
    data = result.get("data", []) if isinstance(result, dict) else []
    for item in data:
        if not item.get("accepting_orders") or item.get("closed"):
            continue
        for t in item.get("tokens", []):
            tid = t.get("token_id"); price = float(t.get("price", 0))
            if tid and 0.03 < price < 0.97:
                token_id = tid
                tick_size = client.get_tick_size(tid)
                print(f"    token: {tid[:30]}... price={price} tick={tick_size}")
                break
        if token_id:
            break
except Exception as e:
    print(f"    ERR: {e}")

if not token_id:
    print("    No market found")
    sys.exit(1)

# Place order
print(f"\n[3] Placing $1 BUY @ 0.02 (POLY_GNOSIS_SAFE)...")
try:
    resp = client.create_and_post_order(
        order_args=OrderArgs(token_id=token_id, price=0.02, size=50, side="BUY"),
        options=PartialCreateOrderOptions(tick_size=tick_size),
        order_type=OrderType.GTC,
    )
    order_id = resp.get("orderID") or resp.get("id") if isinstance(resp, dict) else None
    print(f"    ORDER PLACED: id={order_id}")
    print(f"    raw: {resp}")

    time.sleep(2)
    cancel_resp = client.cancel_order(OrderPayload(orderID=order_id))
    print(f"    CANCELLED: {cancel_resp}")

    print(f"\n{'='*60}")
    print("  SUCCESS! POLY_GNOSIS_SAFE works!")
    print(f"  Update polymarket_auth.py: signature_type=2, funder={DEPOSIT}")
    print(f"{'='*60}\n")

except Exception as e:
    err = str(e)
    print(f"    FAILED: {type(e).__name__}: {err[:300]}")
    if "maker address not allowed" in err:
        print(f"    => Safe not registered / EOA not authorized as Safe signer")
    elif "order signer" in err.lower():
        print(f"    => Server signer check still failing")
    elif "invalid signature" in err.lower() or "signature" in err.lower():
        print(f"    => Exchange contract rejected the signature")

print(f"\n{'='*60}")
print("  DONE")
print(f"{'='*60}\n")
