# bugfixing/check_allowances.py
from web3 import Web3
from src.blockchain.balance import _get_web3
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import ApiCreds
from src.config.settings import settings

REAL_PROXY = "0x9Ce3E9a1e408B2c635674020db6Ab685294BAC2B"
w3 = _get_web3()

# Адреса контрактов V2
PUSD = "0x4Fabb145d64652a948d72533023f6E7A623C7C53"  # pUSD
CTF_V2 = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

erc20_abi = [{"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]

print("=== Allowances от proxy к контрактам V2 ===")
for label, addr in [("CTF Exchange V2", CTF_V2), ("Neg Risk Adapter", NEG_RISK)]:
    try:
        for token_label, token_addr in [("pUSD", PUSD)]:
            c = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=erc20_abi)
            allowance = c.functions.allowance(
                Web3.to_checksum_address(REAL_PROXY),
                Web3.to_checksum_address(addr)
            ).call()
            status = "✅" if allowance > 0 else "❌"
            print(f"  {status} {token_label} → {label}: {allowance / 1e6:.2f}")
    except Exception as e:
        print(f"  ⚠️  {label}: {str(e)[:60]}")

# Важный вопрос — пробуем ещё раз с fresh derive
print("\n=== Пересоздаём creds и пробуем ордер ===")
client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=settings.private_key,
    signature_type=3,
    funder=REAL_PROXY,
)
creds = client.create_or_derive_api_key()
print(f"API key: {creds.api_key[:12]}...")
client.set_api_creds(creds)

from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client_v2.order_utils.model.side import Side

markets = client.get_sampling_markets(next_cursor="")
token_id = markets["data"][0]["tokens"][0]["token_id"]

try:
    resp = client.create_and_post_order(
        order_args=OrderArgs(token_id=token_id, price=0.02, side=Side.BUY, size=5),
        options=PartialCreateOrderOptions(tick_size="0.01"),
        order_type=OrderType.GTC,
    )
    print(f"✅ ОРДЕР ПРОШЁЛ: {resp}")
except Exception as e:
    print(f"❌ {str(e)[:150]}")