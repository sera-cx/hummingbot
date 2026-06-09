import json
import os
import sys
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

API = os.environ.get("SERA_API_URL", "https://api.testnet.sera.cx/api/v1")
PRIVATE_KEY = os.environ.get("SERA_WALLET_PRIVATE_KEY")
LABEL = os.environ.get("SERA_API_KEY_LABEL", "Trading bot")

DOMAIN = {
    "name": "Sera",
    "version": "1",
    "chainId": 11155111,
    "verifyingContract": "0x83475A1bD98a8DC2DCd507A747e4DC85da241D6e",
}
MANAGE_API_KEY_TYPES = {
    "ManageApiKey": [
        {"name": "owner", "type": "address"},
        {"name": "action", "type": "string"},
        {"name": "timestamp", "type": "uint256"},
    ]
}


def main() -> int:
    if not PRIVATE_KEY:
        print("Set SERA_PRIVATE_KEY or SERA_WALLET_PRIVATE_KEY before running sera_setup.py.", file=sys.stderr)
        return 1

    wallet = Account.from_key(PRIVATE_KEY).address
    timestamp = int(time.time())
    message = {"owner": wallet, "action": "create", "timestamp": timestamp}
    signable = encode_typed_data(DOMAIN, MANAGE_API_KEY_TYPES, message)
    signature = Account.from_key(PRIVATE_KEY).sign_message(signable).signature.hex()

    response = requests.post(
        f"{API}/api-keys",
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "owner_address": wallet,
            "action": "create",
            "timestamp": timestamp,
            "signature": "0x" + signature.lstrip("0x"),
            "label": LABEL,
        }),
        timeout=10,
    )
    response.raise_for_status()
    body = response.json()

    print(f"wallet={wallet}")
    print(f"api_key={body['api_key']}")
    print(f"api_secret={body['api_secret']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
