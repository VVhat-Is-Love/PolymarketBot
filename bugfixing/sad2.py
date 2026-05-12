import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# ШАГ 1: Проверяем базовое подключение (без авторизации)
client_l1 = ClobClient(host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
print("L1 клиент создан. Адрес кошелька:", client_l1.get_address())

# ШАГ 2: Деривируем L2 ключи (подписывает твой кошелёк)
print("\nДеривируем L2 API ключи...")
creds = client_l1.create_or_derive_api_creds()
print("API_KEY:        ", creds.api_key)
print("API_SECRET:     ", creds.api_secret)
print("API_PASSPHRASE: ", creds.api_passphrase)

# ШАГ 3: Создаём полноценный L2 клиент
client_l2 = ClobClient(
    host=HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    creds=creds,
)

# ШАГ 4: Проверяем что L2 работает (правильные методы)
print("\nПроверяем L2 авторизацию...")

try:
    # Этот метод требует L2 — если 401, значит ключи неверные
    orders = client_l2.get_orders()
    print("✅ L2 авторизация работает! Открытых ордеров:", len(orders))
except Exception as e:
    print("❌ L2 ошибка:", e)

try:
    # Проверка открытых позиций
    trades = client_l2.get_trades()
    print("✅ Трейды доступны. Всего трейдов:", len(trades))
except Exception as e:
    print("❌ Трейды недоступны:", e)

# ШАГ 5: Баланс USDC — через REST напрямую (нет метода в SDK)
import requests
wallet = client_l2.get_address()
resp = requests.get(
    f"https://clob.polymarket.com/balance",
    headers={
        "POLY_ADDRESS": wallet,
    }
)
print("\nБаланс ответ:", resp.status_code, resp.text[:200])