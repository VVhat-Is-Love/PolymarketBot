"""
On-chain redemption of won Polymarket positions (G4-17).

Positions are held by the Polymarket proxy wallet (funder, signature_type=3).
Redeeming a resolved winning position is an on-chain call that must be executed
*through* the proxy wallet (the token holder); the EOA (PRIVATE_KEY) signs.

Weather "highest temperature" bin markets are NegRisk (mutually-exclusive
multi-outcome) → redeemed via the NegRiskAdapter. Plain binary markets use the
ConditionalTokens (CTF) contract.

SAFETY: every broadcast is preflight-simulated with eth_call from the proxy.
A malformed/incorrect call reverts in simulation and is skipped WITHOUT spending
gas — worst case is the position stays pending_redeem (status quo), never a
wasted transaction.

Log on success: [redeem] {market} redeemed payout=${X}
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

# ── Minimal ABIs ────────────────────────────────────────────────────────────

_NEG_RISK_ADAPTER_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "_conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "_amounts", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

_CTF_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# Polymarket proxy wallet exec method: proxy(Call[]) where
# Call = (uint8 typeCode, address to, uint256 value, bytes data); typeCode 0 = CALL.
_PROXY_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "uint8", "name": "typeCode", "type": "uint8"},
                    {"internalType": "address", "name": "to", "type": "address"},
                    {"internalType": "uint256", "name": "value", "type": "uint256"},
                    {"internalType": "bytes", "name": "data", "type": "bytes"},
                ],
                "internalType": "struct ProxyWallet.Call[]",
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "proxy",
        "outputs": [{"internalType": "bytes[]", "name": "", "type": "bytes[]"}],
        "stateMutability": "payable",
        "type": "function",
    }
]

_ZERO_BYTES32 = "0x" + "00" * 32


def _to_bytes32(condition_id: str):
    from web3 import Web3
    cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id
    return Web3.to_bytes(hexstr=cid)


def _build_redeem_calldata(w3, *, neg_risk: bool, condition_id: str,
                           no_shares_6dec: int, settings) -> tuple[str, str]:
    """
    Return (target_contract_address, redeem_calldata_hex) for the inner redeem
    call (to be wrapped by the proxy). We hold NO; for NegRisk amounts=[yes,no],
    for CTF indexSets=[2] (bit 1 = NO outcome).
    """
    from web3 import Web3

    cid = _to_bytes32(condition_id)
    if neg_risk:
        target = Web3.to_checksum_address(settings.neg_risk_adapter_address)
        contract = w3.eth.contract(address=target, abi=_NEG_RISK_ADAPTER_ABI)
        # amounts ordered [YES, NO]; we hold only NO
        data = contract.encode_abi("redeemPositions", args=[cid, [0, no_shares_6dec]])
    else:
        target = Web3.to_checksum_address(settings.ctf_address)
        contract = w3.eth.contract(address=target, abi=_CTF_ABI)
        collateral = Web3.to_checksum_address(settings.usdc_collateral_address)
        # indexSets: bit0=YES(1), bit1=NO(2). We hold NO → [2].
        data = contract.encode_abi(
            "redeemPositions",
            args=[collateral, _to_bytes32(_ZERO_BYTES32), cid, [2]],
        )
    return target, data


def redeem_position(
    *,
    condition_id: str,
    size_shares: float,
    neg_risk: bool,
    market_label: str = "",
) -> Optional[str]:
    """
    Attempt on-chain redemption of a won NO position via the proxy wallet.

    Returns the tx hash on success, None if skipped (preflight revert, low gas,
    missing config). NEVER raises — caller treats None as "retry next cycle".
    """
    from src.config.settings import settings
    from src.blockchain.balance import _get_web3, get_matic_balance

    if not settings.auto_redeem_enabled:
        logger.info(f"[redeem] disabled (auto_redeem_enabled=False) — skip {market_label}")
        return None
    if not settings.private_key or not settings.proxy_wallet_address:
        logger.warning("[redeem] PRIVATE_KEY / PROXY_WALLET_ADDRESS not set — skip")
        return None

    try:
        from web3 import Web3
        from eth_account import Account

        w3 = _get_web3()
        eoa = Account.from_key(settings.private_key)
        eoa_addr = Web3.to_checksum_address(eoa.address)
        proxy_addr = Web3.to_checksum_address(settings.proxy_wallet_address)

        # Gas preflight
        matic = get_matic_balance(eoa_addr)
        if matic < settings.redeem_min_matic:
            logger.warning(
                f"[redeem] {market_label}: MATIC {matic:.4f} < "
                f"{settings.redeem_min_matic} — skip (top up gas)"
            )
            return None

        no_shares_6dec = int(round(size_shares * 1_000_000))

        # Inner redeem call
        target, inner_data = _build_redeem_calldata(
            w3, neg_risk=neg_risk, condition_id=condition_id,
            no_shares_6dec=no_shares_6dec, settings=settings,
        )

        # Wrap in proxy([ (CALL, target, 0, inner_data) ])
        proxy = w3.eth.contract(address=proxy_addr, abi=_PROXY_ABI)
        calls = [(0, Web3.to_checksum_address(target), 0, Web3.to_bytes(hexstr=inner_data))]

        # ── PREFLIGHT SIMULATION (no gas) ──────────────────────────────────
        try:
            proxy.functions.proxy(calls).call({"from": eoa_addr})
        except Exception as sim_exc:
            exc_str = str(sim_exc)
            # Known: neg_risk markets may revert with 0x3c10b94e (NegRiskAdapter
            # custom error). Possible causes: condition not yet resolved on-chain,
            # wrong conditionId format, or amounts mismatch.
            if neg_risk and "0x3c10b94e" in exc_str:
                logger.warning(
                    f"[redeem] {market_label}: KNOWN neg_risk revert 0x3c10b94e "
                    f"condition_id={condition_id[:16]}… shares={size_shares:.4f} "
                    f"— market may not be settled on-chain yet, or conditionId mismatch. "
                    f"Skipping (will retry next cycle)."
                )
            else:
                logger.warning(
                    f"[redeem] {market_label}: preflight reverted "
                    f"(neg_risk={neg_risk}) — skip, no gas spent: "
                    f"{type(sim_exc).__name__}: {exc_str[:300]}"
                )
            return None

        # ── BROADCAST ──────────────────────────────────────────────────────
        nonce = w3.eth.get_transaction_count(eoa_addr)
        gas_est = proxy.functions.proxy(calls).estimate_gas({"from": eoa_addr})
        tx = proxy.functions.proxy(calls).build_transaction({
            "from": eoa_addr,
            "nonce": nonce,
            "gas": int(gas_est * 1.3),
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
            "chainId": settings.chain_id,
        })
        signed = eoa.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex = tx_hash.hex()
        logger.info(f"[redeem] {market_label}: tx sent {tx_hex} — waiting for receipt")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.get("status") == 1:
            payout = round(size_shares * 1.0, 4)  # winning NO redeems at $1/share
            logger.info(f"[redeem] {market_label} redeemed payout=${payout:.4f} tx={tx_hex}")
            return tx_hex
        logger.error(f"[redeem] {market_label}: tx {tx_hex} reverted on-chain")
        return None

    except Exception as exc:
        logger.error(f"[redeem] {market_label}: failed — {type(exc).__name__}: {str(exc)[:250]}")
        return None
