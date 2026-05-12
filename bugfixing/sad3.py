import os
from web3 import Web3
from eth_account import Account
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# Пробуем несколько публичных RPC по очереди
RPC_URLS = [
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
]

w3 = None
for rpc in RPC_URLS:
    try:
        _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        if _w3.is_connected():
            w3 = _w3
            print(f"✅ RPC подключён: {rpc}")
            break
    except Exception as e:
        print(f"❌ {rpc} — {e}")

if not w3:
    raise RuntimeError("Ни один RPC не подключился. Нужен Alchemy/QuickNode.")

USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]

wallet_address = Account.from_key(PRIVATE_KEY).address
print("Кошелёк:", wallet_address)

usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI)
raw_balance = usdc.functions.balanceOf(wallet_address).call()
decimals = usdc.functions.decimals().call()
balance_usd = raw_balance / (10 ** decimals)

print(f"✅ Баланс USDC.e: ${balance_usd:.2f}")

matic_wei = w3.eth.get_balance(wallet_address)
print(f"✅ Баланс MATIC (газ): {w3.from_wei(matic_wei, 'ether'):.4f}")