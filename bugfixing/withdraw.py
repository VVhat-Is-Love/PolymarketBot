from web3 import Web3
from eth_account import Account

w3 = Web3(Web3.HTTPProvider("https://polygon.drpc.org"))

PRIVATE_KEY = "3c5cf61e027b09b4605c1c8cefbf489fdb2fa13709725b0b3f784e4d74a78330"  # твой ключ
EOA      = "0x0C8A319FBe87885cB6B1b92E71f1454f00486025"
SAFE     = "0x5817FCb2300Fb88bd36e30e6EC01FA595d466d4F"
USDC_E   = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
AMOUNT   = 10_000_276  # 10.000276 USDC (6 decimals)

account = Account.from_key(PRIVATE_KEY)

# Кодируем вызов transfer() внутри Safe
usdc_abi = [{"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],
             "name":"transfer","outputs":[{"name":"","type":"bool"}],
             "stateMutability":"nonpayable","type":"function"}]
usdc = w3.eth.contract(address=w3.to_checksum_address(USDC_E), abi=usdc_abi)
transfer_data = usdc.encode_abi("transfer", args=[EOA, AMOUNT])
# Safe execTransaction ABI
safe_abi = [{
    "name": "execTransaction",
    "type": "function",
    "inputs": [
        {"name":"to","type":"address"},
        {"name":"value","type":"uint256"},
        {"name":"data","type":"bytes"},
        {"name":"operation","type":"uint8"},
        {"name":"safeTxGas","type":"uint256"},
        {"name":"baseGas","type":"uint256"},
        {"name":"gasPrice","type":"uint256"},
        {"name":"gasToken","type":"address"},
        {"name":"refundReceiver","type":"address"},
        {"name":"signatures","type":"bytes"},
    ],
    "outputs":[{"name":"","type":"bool"}],
    "stateMutability":"payable"
}]

safe = w3.eth.contract(address=w3.to_checksum_address(SAFE), abi=safe_abi)

# Подпись "approved by msg.sender" (v=1) — работает если EOA = owner Safe
owner_padded = bytes.fromhex(EOA[2:].zfill(64).lower())  # r = адрес владельца (32 байта)
s_zero       = b'\x00' * 32                               # s = 0
v_approved   = b'\x01'                                    # v = 1
signature    = owner_padded + s_zero + v_approved

# Нужен POL для газа — проверяем
pol_balance = w3.eth.get_balance(EOA)
print(f"POL на EOA: {w3.from_wei(pol_balance, 'ether')}")

tx = safe.functions.execTransaction(
    w3.to_checksum_address(USDC_E),  # to
    0,                                # value (POL)
    transfer_data,                    # data (USDC transfer)
    0,                                # operation (Call)
    0, 0, 0,                          # safeTxGas, baseGas, gasPrice
    "0x0000000000000000000000000000000000000000",  # gasToken
    "0x0000000000000000000000000000000000000000",  # refundReceiver
    signature,
).build_transaction({
    "from": EOA,
    "nonce": w3.eth.get_transaction_count(EOA),
    "gasPrice": w3.eth.gas_price,
    "gas": 150_000,
})

signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
print(f"TX: https://polygonscan.com/tx/{tx_hash.hex()}")