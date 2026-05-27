"""
Attempt to create a Polymarket API key bound to the deposit wallet (0x5817...).
We need this because orders require: order.signer == API_KEY.owner,
and order.signer = deposit_wallet for POLY_1271 flows.

Strategy: POST /auth/api-key with POLY_ADDRESS=deposit_wallet + ERC-7739 wrapped
ClobAuth signature, so the server can call isValidSignature() on the deposit wallet.

Run: python bugfixing/create_deposit_key.py
"""
import sys, requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

from src.config.settings import settings
from eth_abi import encode as abi_encode
from eth_utils import keccak as _keccak
from eth_account import Account

HOST     = settings.polymarket_host
CHAIN_ID = settings.chain_id
PK       = settings.private_key
if not PK.startswith("0x"):
    PK_HEX = "0x" + PK
else:
    PK_HEX = PK

EOA     = settings.eoa_wallet_address.strip()
DEPOSIT = settings.proxy_wallet_address.strip()

print(f"EOA:     {EOA}")
print(f"DEPOSIT: {DEPOSIT}")
print(f"Host:    {HOST}")

# Verify EOA matches the private key
acct = Account.from_key(PK_HEX)
assert acct.address.lower() == EOA.lower(), f"PK mismatch: {acct.address} != {EOA}"
print(f"PK -> EOA verification: OK")

ts    = int(datetime.now().timestamp())
nonce = 0
MSG   = "This message attests that I control the given wallet"


# ── ClobAuth type info ────────────────────────────────────────────
CLOB_AUTH_TYPE_STRING = (
    "ClobAuth(address address,string timestamp,uint256 nonce,string message)"
)
CLOB_AUTH_TYPE_HASH = _keccak(text=CLOB_AUTH_TYPE_STRING)

# ── ClobAuth struct hash (address field = deposit wallet) ─────────
auth_struct_hash = _keccak(
    primitive=abi_encode(
        ["bytes32", "address", "bytes32", "uint256", "bytes32"],
        [
            CLOB_AUTH_TYPE_HASH,
            DEPOSIT,
            _keccak(text=str(ts)),      # string timestamp hashed
            nonce,
            _keccak(text=MSG),
        ],
    )
)
print(f"\nauth_struct_hash: 0x{auth_struct_hash.hex()}")

# ── ClobAuth EIP-712 domain separator ────────────────────────────
# Domain: EIP712Domain(string name, string version, uint256 chainId)
DOMAIN_TYPE_STRING = "EIP712Domain(string name,string version,uint256 chainId)"
DOMAIN_TYPE_HASH   = _keccak(text=DOMAIN_TYPE_STRING)

clob_domain_sep = _keccak(
    primitive=abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256"],
        [
            DOMAIN_TYPE_HASH,
            _keccak(text="ClobAuthDomain"),
            _keccak(text="1"),
            CHAIN_ID,
        ],
    )
)
print(f"clob_domain_sep: 0x{clob_domain_sep.hex()}")

# ── Standard EIP-712 digest (what a simple ecrecover would verify) ─
simple_digest = _keccak(
    primitive=b"\x19\x01" + clob_domain_sep + auth_struct_hash
)

# ── ERC-7739 Solady wrapper ───────────────────────────────────────
# TypedDataSign wraps the inner app-domain message so it's wallet-specific.
SOLADY_TYPE_STRING = (
    "TypedDataSign(ClobAuth contents,string name,string version,"
    "uint256 chainId,address verifyingContract,bytes32 salt)"
    + CLOB_AUTH_TYPE_STRING
)
SOLADY_TYPE_HASH            = _keccak(text=SOLADY_TYPE_STRING)
DEPOSIT_WALLET_NAME_HASH    = _keccak(text="DepositWallet")
DEPOSIT_WALLET_VERSION_HASH = _keccak(text="1")
DEPOSIT_WALLET_DOMAIN_SALT  = bytes(32)

typed_data_sign_struct_hash = _keccak(
    primitive=abi_encode(
        ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "address", "bytes32"],
        [
            SOLADY_TYPE_HASH,
            auth_struct_hash,           # ClobAuth contents hash
            DEPOSIT_WALLET_NAME_HASH,   # deposit wallet name
            DEPOSIT_WALLET_VERSION_HASH,
            CHAIN_ID,
            DEPOSIT,                    # verifyingContract = deposit wallet
            DEPOSIT_WALLET_DOMAIN_SALT,
        ],
    )
)

erc7739_digest = _keccak(
    primitive=b"\x19\x01" + clob_domain_sep + typed_data_sign_struct_hash
)
print(f"erc7739_digest:   0x{erc7739_digest.hex()}")

# ── Sign with EOA private key ─────────────────────────────────────
signed_erc7739 = Account._sign_hash(erc7739_digest, private_key=PK_HEX)
inner_sig_hex = signed_erc7739.signature.hex()
if inner_sig_hex.startswith("0x"):
    inner_sig_hex = inner_sig_hex[2:]
print(f"inner_sig:        0x{inner_sig_hex[:20]}... ({len(inner_sig_hex)//2} bytes)")

# ERC-7739 extended signature:
# 65B inner_sig + 32B appDomainSeparator + 32B contentsHash + NB contentsType + 2B len
contents_type     = CLOB_AUTH_TYPE_STRING.encode("utf-8").hex()
contents_type_len = len(CLOB_AUTH_TYPE_STRING).to_bytes(2, "big").hex()

erc7739_sig = (
    "0x"
    + inner_sig_hex
    + clob_domain_sep.hex()
    + auth_struct_hash.hex()
    + contents_type
    + contents_type_len
)
print(f"erc7739_sig:      0x...({len(erc7739_sig)//2 - 1} bytes total)")

# ── Sign simple EIP-712 (for comparison) ─────────────────────────
signed_simple = Account._sign_hash(simple_digest, private_key=PK_HEX)
simple_sig_hex = "0x" + signed_simple.signature.hex()


print(f"\n{'='*60}")
print("TESTING API KEY CREATION FOR DEPOSIT WALLET")
print(f"{'='*60}")

base_headers = {
    "POLY_TIMESTAMP": str(ts),
    "POLY_NONCE":     str(nonce),
    "Content-Type":   "application/json",
}


def _try(label, method, url, extra_headers):
    h = {**base_headers, **extra_headers}
    print(f"\n  [{label}] {method} {url.replace(HOST, '')}")
    for k, v in extra_headers.items():
        display = v[:40] + "..." if len(str(v)) > 43 else v
        print(f"      {k}: {display}")
    if method == "POST":
        r = requests.post(url, headers=h)
    else:
        r = requests.get(url, headers=h)
    print(f"      -> {r.status_code}: {r.text[:200]}")
    return r


# Test 1: ERC-7739 wrapped ClobAuth sig, POLY_ADDRESS=deposit, POST create
_try("1", "POST", f"{HOST}/auth/api-key", {
    "POLY_ADDRESS":   DEPOSIT,
    "POLY_SIGNATURE": erc7739_sig,
})

# Test 2: ERC-7739 wrapped ClobAuth sig, POLY_ADDRESS=deposit, GET derive
_try("2", "GET", f"{HOST}/auth/derive-api-key", {
    "POLY_ADDRESS":   DEPOSIT,
    "POLY_SIGNATURE": erc7739_sig,
})

# Test 3: Simple EOA sig, ClobAuth.address=deposit, POLY_ADDRESS=deposit, POST
_try("3", "POST", f"{HOST}/auth/api-key", {
    "POLY_ADDRESS":   DEPOSIT,
    "POLY_SIGNATURE": simple_sig_hex,
})

# Test 4: Simple EOA sig, ClobAuth.address=deposit, POLY_ADDRESS=deposit, GET derive
_try("4", "GET", f"{HOST}/auth/derive-api-key", {
    "POLY_ADDRESS":   DEPOSIT,
    "POLY_SIGNATURE": simple_sig_hex,
})

# Test 5: Standard EOA key (ClobAuth.address=EOA), POLY_ADDRESS=EOA (reference - should work)
ts2    = ts + 1
nonce2 = 0
auth_struct_eoa = _keccak(
    primitive=abi_encode(
        ["bytes32", "address", "bytes32", "uint256", "bytes32"],
        [CLOB_AUTH_TYPE_HASH, EOA, _keccak(text=str(ts2)), nonce2, _keccak(text=MSG)],
    )
)
simple_digest_eoa = _keccak(primitive=b"\x19\x01" + clob_domain_sep + auth_struct_eoa)
sig_eoa = "0x" + Account._sign_hash(simple_digest_eoa, private_key=PK_HEX).signature.hex()

_try("5-ref", "GET", f"{HOST}/auth/derive-api-key", {
    "POLY_ADDRESS":   EOA,
    "POLY_SIGNATURE": sig_eoa,
    "POLY_TIMESTAMP": str(ts2),
    "POLY_NONCE":     str(nonce2),
})

print(f"\n{'='*60}")
print("DONE")
print(f"{'='*60}\n")
