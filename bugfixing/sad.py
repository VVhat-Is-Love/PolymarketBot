from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

PRIVATE_KEY = "721824e8246312a4d03252fedf4b6cda7f516b8c0fec65f827b51e05e96efecb"
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# Сначала создаём клиент без L2 ключей (только L1)
client = ClobClient(
    host=HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
)

# Деривируем L2 ключи — они генерируются подписью твоего кошелька
# Это нужно сделать ОДИН РАЗ и сохранить результат
creds = client.create_or_derive_api_creds()

print("API_KEY:", creds.api_key)
print("API_SECRET:", creds.api_secret)
print("API_PASSPHRASE:", creds.api_passphrase)