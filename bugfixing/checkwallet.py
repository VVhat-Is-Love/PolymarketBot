from src.config.settings import settings
from eth_account import Account

eoa = Account.from_key(settings.private_key).address
print(f'PRIVATE_KEY → адрес: {eoa}')
print(f'PROXY_WALLET_ADDRESS: {settings.proxy_wallet_address}')
print(f'EOA_WALLET_ADDRESS: {settings.eoa_wallet_address}')
print(f'API_KEY: {settings.polymarket_api_key[:12]}...')
print()
print('Эти адреса совпадают с одним аккаунтом?')
print(f'EOA == proxy owner: {eoa.lower() == settings.proxy_wallet_address.lower()}')

