"""
health_check.py — полная проверка всех подключений перед запуском бота
Запуск: python health_check.py
"""

import os
import sys
import requests
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

load_dotenv()

# ─── Адреса ───────────────────────────────────────────────────────────────────
PRIVATE_KEY   = os.getenv("PRIVATE_KEY")
EOA_WALLET    = os.getenv("EOA_WALLET_ADDRESS")
PROXY_WALLET  = os.getenv("PROXY_WALLET_ADDRESS")

# ─── Результаты проверок ──────────────────────────────────────────────────────
results = []
errors  = []

def ok(msg):
    results.append(f"  ✅  {msg}")

def fail(msg):
    results.append(f"  ❌  {msg}")
    errors.append(msg)

def section(title):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print('─'*50)

# ══════════════════════════════════════════════════════════════════════════════
section("1. Переменные окружения")
# ══════════════════════════════════════════════════════════════════════════════

required_vars = [
    "PRIVATE_KEY",
    "EOA_WALLET_ADDRESS",
    "PROXY_WALLET_ADDRESS",
    "POLYGON_RPC_URL",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
]

for var in required_vars:
    val = os.getenv(var)
    if val:
        masked = val[:6] + "..." + val[-4:] if len(val) > 12 else "***"
        ok(f"{var} = {masked}")
    else:
        fail(f"{var} — НЕ ЗАДАНА")

# ══════════════════════════════════════════════════════════════════════════════
section("2. Polygon RPC + балансы кошелька (on-chain)")
# ══════════════════════════════════════════════════════════════════════════════

RPC_FALLBACK = [
    os.getenv("POLYGON_RPC_URL"),
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
]

w3 = None
for rpc in filter(None, RPC_FALLBACK):
    try:
        _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        if _w3.is_connected():
            w3 = _w3
            ok(f"RPC подключён: {rpc}")
            break
    except Exception as e:
        fail(f"RPC недоступен {rpc}: {e}")

if w3:
    try:
        # Проверяем адрес из приватного ключа совпадает с EOA_WALLET
        derived = Account.from_key(PRIVATE_KEY).address.lower()
        if EOA_WALLET and derived == EOA_WALLET.lower():
            ok(f"Приватный ключ совпадает с EOA_WALLET: {derived[:10]}...")
        else:
            fail(f"ПРИВАТНЫЙ КЛЮЧ НЕ СОВПАДАЕТ С EOA_WALLET!\n"
                 f"      Из ключа: {derived}\n"
                 f"      В .env:   {EOA_WALLET}")

        # USDC баланс EOA
        USDC_ABI = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],
                     "name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"),
            abi=USDC_ABI
        )
        balance_usdc = usdc.functions.balanceOf(Account.from_key(PRIVATE_KEY).address).call() / 1e6
        ok(f"USDC в кошельке (EOA): ${balance_usdc:.2f}")

        # MATIC баланс
        matic = w3.from_wei(w3.eth.get_balance(Account.from_key(PRIVATE_KEY).address), "ether")
        if float(matic) < 0.1:
            fail(f"MATIC критически мало: {matic:.4f} — транзакции могут не пройти")
        else:
            ok(f"MATIC (газ): {matic:.4f}")

    except Exception as e:
        fail(f"Ошибка при чтении on-chain баланса: {e}")
else:
    fail("Ни один RPC не ответил — on-chain проверки пропущены")

# ══════════════════════════════════════════════════════════════════════════════
section("3. Polymarket CLOB API (L2)")
# ══════════════════════════════════════════════════════════════════════════════

clob_client = None
try:
    creds = ApiCreds(
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
    )
    clob_client = ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=137,
        creds=creds,
    )
    orders = clob_client.get_orders()
    ok(f"CLOB L2 авторизация работает | Открытых ордеров: {len(orders)}")

    trades = clob_client.get_trades()
    ok(f"История трейдов доступна | Всего трейдов: {len(trades)}")

except Exception as e:
    fail(f"CLOB API ошибка: {e}")

# ══════════════════════════════════════════════════════════════════════════════
section("4. Polymarket портфель (Proxy кошелёк)")
# ══════════════════════════════════════════════════════════════════════════════

if PROXY_WALLET:
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/value",
            params={"user": PROXY_WALLET},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            item = data[0] if isinstance(data, list) and data else data
            value = float(item.get("value", item.get("cashBalance", 0)))
            ok(f"Портфель Polymarket (proxy): ${value:.2f} USDC")
        else:
            fail(f"data-api вернул {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        fail(f"Ошибка получения портфеля: {e}")

    try:
        resp2 = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": PROXY_WALLET},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp2.status_code == 200:
            positions = resp2.json()
            if positions:
                ok(f"Открытых позиций: {len(positions)}")
                for p in positions[:3]:  # показываем максимум 3
                    title = p.get("title", "Unknown")[:40]
                    outcome = p.get("outcome", "?")
                    size = float(p.get("size", 0))
                    price = float(p.get("currentPrice", 0))
                    print(f"       • {title} | {outcome} | {size:.2f} шт × ${price:.3f}")
            else:
                ok("Открытых позиций нет")
        else:
            fail(f"positions API вернул {resp2.status_code}")
    except Exception as e:
        fail(f"Ошибка получения позиций: {e}")     
else:
    fail("PROXY_WALLET_ADDRESS не задан в .env")

# ══════════════════════════════════════════════════════════════════════════════
section("ИТОГ")
# ══════════════════════════════════════════════════════════════════════════════

for r in results:
    print(r)

if errors:
    print(f"\n⚠️  Найдено проблем: {len(errors)}")
    print("   Исправь выделенные ❌ перед запуском бота\n")
    sys.exit(1)
else:
    print(f"\n🟢 Всё готово — бот можно запускать\n")
    sys.exit(0)