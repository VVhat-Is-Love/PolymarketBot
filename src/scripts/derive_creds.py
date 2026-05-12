"""
One-time script to derive Polymarket L2 API credentials from your private key.

Usage:
    python -m src.scripts.derive_creds

Prints POLYMARKET_API_KEY / _SECRET / _PASSPHRASE.
Copy them into .env, then run health check to verify.
"""
import sys


if __name__ == "__main__":
    from src.blockchain.polymarket_auth import derive_api_credentials
    try:
        derive_api_credentials()
    except Exception as exc:
        print(f"\n❌ Failed: {exc}\n")
        sys.exit(1)
