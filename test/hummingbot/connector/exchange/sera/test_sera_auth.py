import asyncio
from typing import Awaitable
from unittest import TestCase

from eth_account import Account
from eth_account.messages import encode_typed_data

from hummingbot.connector.exchange.sera import sera_constants as CONSTANTS
from hummingbot.connector.exchange.sera.sera_auth import SeraAuth
from hummingbot.connector.utils import to_0x_hex
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class SeraAuthTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.api_key = "seraApiKey"
        self.api_secret = "seraApiSecret"
        self.private_key = "13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930"  # noqa: mock
        self.wallet = Account.from_key(self.private_key)
        self.auth = SeraAuth(
            api_key=self.api_key,
            api_secret=self.api_secret,
            wallet_address=self.wallet.address,
            private_key=self.private_key,
        )

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: int = 1):
        return asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coroutine, timeout))

    def test_rest_authenticate_adds_bearer_header(self):
        request = RESTRequest(
            method=RESTMethod.GET,
            url="https://api.testnet.sera.cx/api/v1/orders",
            is_auth_required=True,
            headers={"Content-Type": "application/json"},
        )

        configured_request = self.async_run_with_timeout(self.auth.rest_authenticate(request))

        self.assertEqual("application/json", configured_request.headers["Content-Type"])
        self.assertEqual(
            f"Bearer {self.api_key}:{self.api_secret}",
            configured_request.headers["Authorization"],
        )

    def test_sign_typed_data_matches_eth_account_signature(self):
        domain = {
            "name": "Sera",
            "version": "1",
            "chainId": 11155111,
            "verifyingContract": "0x83475A1bD98a8DC2DCd507A747e4DC85da241D6e",  # noqa: mock
        }
        message = {
            "owner": self.wallet.address.lower(),
            "orderId": 6427948336465191935941739505432058208337171677044006212075520,
        }

        signature = self.auth.sign_typed_data(
            domain=domain,
            message_types={"EIP712Domain": [], **CONSTANTS.CANCEL_ORDER_TYPES},
            message=message,
        )
        expected_signable = encode_typed_data(
            domain_data=domain,
            message_types=CONSTANTS.CANCEL_ORDER_TYPES,
            message_data=message,
        )
        expected_signature = to_0x_hex(self.wallet.sign_message(expected_signable).signature)

        self.assertEqual(expected_signature, signature)
