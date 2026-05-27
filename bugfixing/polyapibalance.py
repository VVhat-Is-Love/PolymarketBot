# bugfixing/test_order.py
import time
from src.blockchain.polymarket_auth import get_clob_client
from py_clob_client_v2.clob_types import OrderArgs, OrderType

client = get_clob_client()

# 1. Берём живой рынок с хорошей ликвидностью
# "Will Bitcoin be above $100k on June 1?" — всегда есть ликвидность
# Найдём любой активный рынок
print("=== Ищем активный рынок ===")
markets = client.get_markets(next_cursor="")
for m in markets.get("data", [])[:10]:
    print(f"  {m.get('question', '')[:60]} | active={m.get('active')} | tokens={[t.get('token_id','') for t in m.get('tokens',[])]}")

# 2. Берём первый активный рынок
active = [m for m in markets.get("data", []) if m.get("active") and not m.get("closed")]
if not active:
    print("Нет активных рынков")
    exit()

market = active[0]
token_id = market["tokens"][0]["token_id"]  # YES токен
question = market.get("question", "")[:60]
print(f"\n=== Выбран рынок: {question} ===")
print(f"Token ID (YES): {token_id}")

# 3. Смотрим стакан
book = client.get_order_book(token_id)
bids = book.bids if hasattr(book, 'bids') else book.get("bids", [])
asks = book.asks if hasattr(book, 'asks') else book.get("asks", [])
print(f"Bids: {bids[:3]}")
print(f"Asks: {asks[:3]}")

# 4. Размещаем лимитный ордер на покупку за $1 по низкой цене (не исполнится сразу)
# Минимальный размер на Polymarket = $1 / цена акций
# Ставим цену 0.01 (1 цент) — точно не исполнится, просто тест
print("\n=== Размещаем тестовый ордер ===")
try:
    order_args = OrderArgs(
        token_id=token_id,
        price=0.01,   # 1 цент — не исполнится
        size=1.0,     # $1 номинал
        side="BUY",
    )
    resp = client.create_and_post_order(order_args)
    print(f"Ордер размещён: {resp}")
    
    order_id = resp.get("orderID") or resp.get("id") or resp.get("order_id")
    
    if order_id:
        print(f"\nOrder ID: {order_id}")
        print("Ждём 5 секунд...")
        time.sleep(5)
        
        # 5. Отменяем
        cancel = client.cancel(order_id)
        print(f"Ордер отменён: {cancel}")
    else:
        print("Order ID не найден в ответе, смотри вывод выше")
        
except Exception as e:
    print(f"ОШИБКА: {e}")
    import traceback
    traceback.print_exc()