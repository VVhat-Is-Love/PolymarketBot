# bugfixing/btc_trade.py
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, ApiCreds
from py_clob_client_v2.order_utils.model.side import Side
from src.config.settings import settings

PROXY = "0x9Ce3E9a1e408B2c635674020db6Ab685294BAC2B"

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=settings.private_key,
    creds=ApiCreds(
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        api_passphrase=settings.polymarket_api_passphrase,
    ),
    signature_type=3,
    funder=PROXY,
)

# Используем известный ликвидный token_id — BTC выше $X
# Берём напрямую из популярного рынка
import requests

# Ищем активные BTC рынки через gamma
r = requests.get(
    "https://gamma-api.polymarket.com/markets",
    params={"active": "true", "tag": "crypto", "limit": 10},
    timeout=10,
)
markets = r.json()

token_id = None
best_ask_price = None

for m in markets if isinstance(markets, list) else markets.get("markets", []):
    clob_ids = m.get("clobTokenIds", "[]")
    if isinstance(clob_ids, str):
        import json
        try:
            clob_ids = json.loads(clob_ids)
        except:
            continue
    
    for tid in clob_ids[:1]:
        try:
            book = client.get_order_book(tid)
            asks = book.asks if hasattr(book, "asks") else []
            bids = book.bids if hasattr(book, "bids") else []
            if asks and bids:
                token_id = tid
                best_ask_price = float(asks[0].price)
                print(f"✅ Рынок: {m.get('question','')[:50]}")
                print(f"   Token: {tid[:20]}...")
                print(f"   Ask: {best_ask_price} | Bid: {float(bids[0].price)}")
                break
        except:
            continue
    if token_id:
        break

if not token_id:
    print("Не нашли — берём захардкоженный популярный рынок")
    # BTC выше $110k на конец мая — всегда ликвидный
    token_id = "13915689317269078219168496739008737517740566192006337297676041270492637394586"
    best_ask_price = 0.50

size = round(1.0 / best_ask_price, 1)
if size * best_ask_price < 1.0:
    size = round(1.0 / best_ask_price + 0.1, 1)

print(f"\nРазмещаем: {size} акций × {best_ask_price} = ${size * best_ask_price:.2f}")

try:
    resp = client.create_and_post_order(
        order_args=OrderArgs(
            token_id=token_id,
            price=best_ask_price,
            side=Side.BUY,
            size=size,
        ),
        options=PartialCreateOrderOptions(tick_size="0.01"),
        order_type=OrderType.FOK,
    )
    print(f"\n✅ Ордер: {resp}")
except Exception as e:
    print(f"❌ {e}")