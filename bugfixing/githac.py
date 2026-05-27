from web3 import Web3
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
code = w3.eth.get_code(Web3.to_checksum_address(FUNDER_ADDRESS))
print("proxy deployed:", code not in (b"", b"0x"))