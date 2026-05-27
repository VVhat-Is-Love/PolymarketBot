# bugfixing/create_new_eoa.py
from eth_account import Account

acc = Account.create()
print("="*60)
print("НОВЫЙ КОШЕЛЁК — сохрани в надёжное место!")
print("="*60)
print(f"Address:     {acc.address}")
print(f"Private key: {acc.key.hex()}")
print("="*60)
print("ЗАПИШИ ЭТИ ДАННЫЕ ПРЯМО СЕЙЧАС")