# bugfixing/test_sig3_eoa_key.py
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, ApiCreds
from py_clob_client_v2.order_utils.model.side import Side
from src.config.settings import settings

# Deposit wallet задеплоен через relayer для OLD EOA
DEPOSIT = "0x55EBFe8e1E9F0A430BD42d2C03e38fF50aF2d956"

creds = ApiCreds(
    api_key=settings.polymarket_api_key,
    api_secret=settings.polymarket_api_secret,
    api_passphrase=settings.polymarket_api_passphrase,
)

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=settings.private_key,
    creds=creds,
    signature_type=3,
    funder=DEPOSIT,
)

print(f"EOA: {client.get_address()}")
print(f"Funder: {DEPOSIT}")

markets = client.get_sampling_markets(next_cursor="")
token_id = markets["data"][0]["tokens"][0]["token_id"]

try:
    resp = client.create_and_post_order(
        order_args=OrderArgs(
            token_id=token_id,
            price=0.02,
            side=Side.BUY,
            size=5,
        ),
        options=PartialCreateOrderOptions(tick_size="0.01"),
        order_type=OrderType.GTC,
    )
    print(f"✅ ОРДЕР: {resp}")
except Exception as e:
    print(f"❌ {str(e)[:200]}")