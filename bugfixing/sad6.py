# bugfixing/create_key_manual.py
import requests
from datetime import datetime
from eth_utils import keccak
from py_order_utils.utils import prepend_zx
from py_clob_client_v2.signing.model import ClobAuth
from py_clob_client_v2.signing.eip712 import get_clob_auth_domain, MSG_TO_SIGN
from py_clob_client_v2.signer import Signer

NEW_PRIVATE_KEY = "3c5cf61e027b09b4605c1c8cefbf489fdb2fa13709725b0b3f784e4d74a78330"
DEPOSIT_WALLET = "0x5817FCb2300Fb88bd36e30e6EC01FA595d466d4F"
CHAIN_ID = 137

signer = Signer(NEW_PRIVATE_KEY, CHAIN_ID)
ts = int(datetime.now().timestamp())
nonce = 0

# Подписываем от имени DEPOSIT WALLET (не EOA)
clob_auth_msg = ClobAuth(
    address=DEPOSIT_WALLET,  # ← deposit wallet вместо EOA
    timestamp=str(ts),
    nonce=nonce,
    message=MSG_TO_SIGN,
)

domain = get_clob_auth_domain(CHAIN_ID)
auth_hash = prepend_zx(keccak(clob_auth_msg.signable_bytes(domain)).hex())
signature = prepend_zx(signer.sign(auth_hash))

headers = {
    "POLY_ADDRESS":   DEPOSIT_WALLET,  # ← deposit wallet
    "POLY_SIGNATURE": signature,
    "POLY_TIMESTAMP": str(ts),
    "POLY_NONCE":     str(nonce),
}

print(f"EOA подписал:     {signer.address()}")
print(f"POLY_ADDRESS:     {DEPOSIT_WALLET}")

# Удаляем старый ключ
print("\n=== Удаляем старый ключ ===")
r = requests.delete(
    "https://clob.polymarket.com/auth/api-key",
    headers={
        **headers,
        "POLY_API_KEY": "cc383e31-707c-967f-079f-f53c32037d06",
    }
)
print(f"DELETE: {r.status_code} {r.text}")

# Пересчитываем timestamp для нового запроса
ts2 = int(datetime.now().timestamp())
clob_auth_msg2 = ClobAuth(
    address=DEPOSIT_WALLET,
    timestamp=str(ts2),
    nonce=nonce,
    message=MSG_TO_SIGN,
)
auth_hash2 = prepend_zx(keccak(clob_auth_msg2.signable_bytes(domain)).hex())
signature2 = prepend_zx(signer.sign(auth_hash2))

headers2 = {
    "POLY_ADDRESS":   DEPOSIT_WALLET,
    "POLY_SIGNATURE": signature2,
    "POLY_TIMESTAMP": str(ts2),
    "POLY_NONCE":     str(nonce),
}

# Создаём новый ключ привязанный к deposit wallet
print("\n=== Создаём ключ для deposit wallet ===")
r2 = requests.post(
    "https://clob.polymarket.com/auth/api-key",
    headers=headers2,
)
print(f"POST: {r2.status_code} {r2.text}")