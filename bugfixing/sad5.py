from web3 import Web3
from dotenv import load_dotenv
import os

load_dotenv(override=True)

w3 = Web3(Web3.HTTPProvider("https://rpc-mainnet.matic.quiknode.pro"))

USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
ABI  = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],
         "name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]

usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI)

for name, addr in [
    ("EOA   (MetaMask)", os.getenv("EOA_WALLET_ADDRESS")),
    ("PROXY (Polymarket)", os.getenv("PROXY_WALLET_ADDRESS")),
]:
    bal = usdc.functions.balanceOf(Web3.to_checksum_address(addr)).call() / 1e6
    print(f"{name}: ${bal:.2f} USDC")