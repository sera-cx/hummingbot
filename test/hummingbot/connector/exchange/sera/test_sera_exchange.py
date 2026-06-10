import asyncio
import json
import re
import unittest
from decimal import Decimal
from typing import Awaitable, Dict, List
from unittest.mock import patch

from aioresponses import aioresponses

from hummingbot.connector.exchange.sera import sera_constants as CONSTANTS, sera_web_utils as web_utils
from hummingbot.connector.exchange.sera.sera_auth import SeraAuth
from hummingbot.connector.exchange.sera.sera_exchange import SeraExchange
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState


class SeraExchangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "EURC"
        cls.quote_asset = "USDC"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.private_key = "13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930"  # noqa: mock
        cls.wallet_address = "0x1dD6A2730b4f5C154511dBf92de1dC9D8B905Bb6"  # noqa: mock
        cls.base_address = "0xef64d15ed6c371545eb6dcd6c026c17dfb6c440f"  # noqa: mock
        cls.quote_address = "0xDcaEcdd8Db64f4316A11917Ad0162DEBD935285b"  # noqa: mock
        cls.order_id = "00000000-0000-4000-8000-000000000001"

    def setUp(self) -> None:
        super().setUp()
        self.exchange = SeraExchange(
            sera_api_key="apiKey",
            sera_api_secret="apiSecret",
            sera_wallet_address=self.wallet_address,
            sera_wallet_private_key=self.private_key,
            trading_pairs=[self.trading_pair],
        )
        self.exchange._set_current_timestamp(1234567890)
        self.exchange._executor_id = 1
        self.exchange._eip712_domain = self.eip712_domain
        self.exchange._market_info[self.trading_pair] = self.market_info
        self.exchange._token_info_by_symbol = {
            self.base_asset: {"symbol": self.base_asset, "currency": "EUR"},
            self.quote_asset: {"symbol": self.quote_asset, "currency": "USD"},
        }

    def tearDown(self) -> None:
        self.exchange.order_book_tracker.stop()
        super().tearDown()

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: int = 1):
        return asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coroutine, timeout))

    @property
    def eip712_domain(self) -> Dict:
        return {
            "name": "Sera",
            "version": "1",
            "chainId": 11155111,
            "verifyingContract": "0x83475A1bD98a8DC2DCd507A747e4DC85da241D6e",  # noqa: mock
        }

    @property
    def market_info(self) -> Dict:
        return {
            "symbol": "EURC/USDC",
            "base_symbol": self.base_asset,
            "quote_symbol": self.quote_asset,
            "base_address": self.base_address,
            "quote_address": self.quote_address,
            "amount_step": "0.01",
            "price_step": "0.0001",
            "min_ask_amount": "0",
            "min_bid_quote_amount": "8.800000",
            "base_decimals": 6,
            "quote_decimals": 6,
        }

    @property
    def markets_response(self) -> Dict[str, List[Dict]]:
        return {"markets": [self.market_info]}

    def test_supported_order_types(self):
        self.assertEqual([OrderType.LIMIT, OrderType.LIMIT_MAKER], self.exchange.supported_order_types())

    def test_format_trading_rules_and_symbol_map(self):
        rules = self.async_run_with_timeout(self.exchange._format_trading_rules(self.markets_response))

        self.assertEqual(1, len(rules))
        rule = rules[0]
        self.assertEqual(self.trading_pair, rule.trading_pair)
        self.assertEqual(Decimal("0.01"), rule.min_order_size)
        self.assertEqual(Decimal("0.0001"), rule.min_price_increment)
        self.assertEqual(Decimal("0.01"), rule.min_base_amount_increment)
        self.assertEqual(Decimal("8.800000"), rule.min_notional_size)

        self.exchange._initialize_trading_pair_symbols_from_exchange_info(self.markets_response)

        self.assertEqual("EURC/USDC", self.async_run_with_timeout(
            self.exchange.exchange_symbol_associated_to_pair(self.trading_pair)
        ))

    def test_encode_standalone_uuid_matches_docs_example(self):
        uuid_int = self.exchange._encode_standalone_uuid(order_id=self.order_id, executor_id=0)

        self.assertEqual(
            "6427948336465191935941739505432058208337171677044006212075520",
            uuid_int,
        )

    def test_validate_eip712_domain_accepts_expected_contract_address(self):
        self.exchange._validate_eip712_domain({
            "name": CONSTANTS.EIP712_DOMAIN_NAME,
            "version": CONSTANTS.EIP712_DOMAIN_VERSION,
            "chainId": str(CONSTANTS.EIP712_CHAIN_ID),
            "verifyingContract": CONSTANTS.EIP712_VERIFYING_CONTRACT.lower(),
        })

    def test_validate_eip712_domain_rejects_contract_address_mismatch(self):
        with self.assertRaisesRegex(ValueError, "EIP-712 domain mismatch for verifyingContract"):
            self.exchange._validate_eip712_domain({
                "name": CONSTANTS.EIP712_DOMAIN_NAME,
                "version": CONSTANTS.EIP712_DOMAIN_VERSION,
                "chainId": CONSTANTS.EIP712_CHAIN_ID,
                "verifyingContract": "0x0000000000000000000000000000000000000001",
            })

    @aioresponses()
    @patch.object(SeraAuth, "sign_typed_data", return_value="0xsigned")
    def test_place_order_previews_signs_and_submits_normalized_payload(self, mock_api, sign_mock):
        preview_response = {
            "normalized_amount": "1000",
            "normalized_price": "1.085",
            "eip712_order": {
                "user": self.wallet_address,
                "expiration": "1713254400",
                "feeBps": "0",
                "recipient": CONSTANTS.ZERO_ADDRESS,
                "fromToken": self.quote_address,
                "toToken": self.base_address,
                "fromAmount": "1085000000",
                "toAmount": "1000000000",
                "initialDepositAmount": "0",
                "uuid": self.exchange._encode_standalone_uuid(self.order_id, self.exchange._executor_id),
            },
            "eip712_types": CONSTANTS.ORDER_TYPES,
        }
        preview_url = web_utils.public_rest_url(CONSTANTS.PREVIEW_ORDER_PATH_URL)
        order_url = web_utils.public_rest_url(CONSTANTS.ORDERS_PATH_URL)
        mock_api.post(preview_url, body=json.dumps(preview_response))
        mock_api.post(order_url, body=json.dumps({"order_id": self.order_id}))

        exchange_order_id, timestamp = self.async_run_with_timeout(self.exchange._place_order(
            order_id=self.order_id,
            trading_pair=self.trading_pair,
            amount=Decimal("1000"),
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("1.085"),
            expiration=1713254400,
        ))

        self.assertEqual(self.order_id, exchange_order_id)
        self.assertEqual(1234567890, timestamp)
        preview_request = self._all_executed_requests(mock_api, preview_url)[0]
        preview_payload = json.loads(preview_request.kwargs["data"])
        self.assertEqual(self.wallet_address.lower(), preview_payload["owner_address"])
        self.assertEqual(CONSTANTS.SIDE_BID, preview_payload["side"])
        self.assertEqual(self.base_address, preview_payload["from_address"])
        self.assertEqual(self.quote_address, preview_payload["to_address"])
        self.assertEqual(self.order_id, preview_payload["order_id"])

        order_request = self._all_executed_requests(mock_api, order_url)[0]
        order_payload = json.loads(order_request.kwargs["data"])
        self.assertEqual("1000", order_payload["amount"])
        self.assertEqual("1.085", order_payload["price"])
        self.assertEqual("0xsigned", order_payload["signature"])
        sign_mock.assert_called_once_with(
            domain=self.eip712_domain,
            message_types=CONSTANTS.ORDER_TYPES,
            message=preview_response["eip712_order"],
        )

    def test_validate_previewed_eip712_order_accepts_intended_buy_order(self):
        preview_response = {
            "normalized_amount": "1000",
            "normalized_price": "1.085",
            "eip712_order": {
                "user": self.wallet_address,
                "expiration": "1713254400",
                "feeBps": "0",
                "recipient": CONSTANTS.ZERO_ADDRESS,
                "fromToken": self.quote_address,
                "toToken": self.base_address,
                "fromAmount": "1085000000",
                "toAmount": "1000000000",
                "initialDepositAmount": "0",
                "uuid": self.exchange._encode_standalone_uuid(self.order_id, self.exchange._executor_id),
            },
            "eip712_types": CONSTANTS.ORDER_TYPES,
        }
        preview_payload = {
            "owner_address": self.wallet_address.lower(),
            "amount": "1000",
            "price": "1.085",
            "uuid_int": self.exchange._encode_standalone_uuid(self.order_id, self.exchange._executor_id),
            "expiration": 1713254400,
        }

        self.exchange._validate_previewed_eip712_order(
            preview_payload=preview_payload,
            preview=preview_response,
            market=self.market_info,
            trade_type=TradeType.BUY,
            amount=Decimal("1000"),
            price=Decimal("1.085"),
        )

    def test_validate_previewed_eip712_order_rejects_mismatch(self):
        preview_response = {
            "normalized_amount": "1000",
            "normalized_price": "1.085",
            "eip712_order": {
                "user": "0x0000000000000000000000000000000000000002",
                "expiration": "1713254400",
                "feeBps": "0",
                "recipient": CONSTANTS.ZERO_ADDRESS,
                "fromToken": self.quote_address,
                "toToken": self.base_address,
                "fromAmount": "1085000000",
                "toAmount": "1000000000",
                "initialDepositAmount": "0",
                "uuid": self.exchange._encode_standalone_uuid(self.order_id, self.exchange._executor_id),
            },
            "eip712_types": CONSTANTS.ORDER_TYPES,
        }
        preview_payload = {
            "owner_address": self.wallet_address.lower(),
            "amount": "1000",
            "price": "1.085",
            "uuid_int": self.exchange._encode_standalone_uuid(self.order_id, self.exchange._executor_id),
            "expiration": 1713254400,
        }

        with self.assertRaisesRegex(ValueError, "previewed EIP-712 order mismatch for user"):
            self.exchange._validate_previewed_eip712_order(
                preview_payload=preview_payload,
                preview=preview_response,
                market=self.market_info,
                trade_type=TradeType.BUY,
                amount=Decimal("1000"),
                price=Decimal("1.085"),
            )

    @aioresponses()
    @patch.object(SeraAuth, "sign_typed_data", return_value="0xcancel")
    def test_place_cancel_signs_cancel_order_payload(self, mock_api, sign_mock):
        tracked_order = self._tracked_order(exchange_order_id=self.order_id)
        uuid_int = self.exchange._encode_standalone_uuid(self.order_id, self.exchange._executor_id)
        self.exchange._order_uuid_ints[self.order_id] = uuid_int
        cancel_url = web_utils.public_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        order_status_url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL.format(order_id=self.order_id))
        mock_api.post(cancel_url, body=json.dumps({"status": "ok"}))

        cancelled = self.async_run_with_timeout(self.exchange._place_cancel(
            order_id=tracked_order.client_order_id,
            tracked_order=tracked_order,
        ))

        self.assertTrue(cancelled)
        self.assertEqual(0, len(self._all_executed_requests(mock_api, order_status_url)))
        cancel_request = self._all_executed_requests(mock_api, cancel_url)[0]
        self.assertEqual({
            "owner_address": self.wallet_address.lower(),
            "order_id": self.order_id,
            "uuid_int": uuid_int,
            "signature": "0xcancel",
        }, json.loads(cancel_request.kwargs["data"]))
        sign_mock.assert_called_once_with(
            domain=self.eip712_domain,
            message_types=CONSTANTS.CANCEL_ORDER_TYPES,
            message={"owner": self.wallet_address.lower(), "orderId": int(uuid_int)},
        )

    @aioresponses()
    def test_request_order_status_maps_pending_with_fill_to_partially_filled(self, mock_api):
        tracked_order = self._tracked_order(exchange_order_id=self.order_id)
        uuid_int = self.exchange._encode_standalone_uuid(self.order_id, self.exchange._executor_id)
        order_status_url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL.format(order_id=self.order_id))
        mock_api.get(order_status_url, body=json.dumps({
            "trade_id": self.order_id,
            "status": "pending",
            "filled_base_amount": "400.0",
            "updated_at": "2026-04-15T08:01:00+00:00",
            "uuid_int": uuid_int,
        }))

        order_update = self.async_run_with_timeout(self.exchange._request_order_status(tracked_order=tracked_order))

        self.assertEqual(OrderState.PARTIALLY_FILLED, order_update.new_state)
        self.assertEqual(self.order_id, order_update.exchange_order_id)
        self.assertEqual(uuid_int, self.exchange._order_uuid_ints[self.order_id])
        request = self._all_executed_requests(mock_api, order_status_url)[0]
        self.assertEqual(f"Bearer {self.exchange.api_key}:{self.exchange.api_secret}",
                         request.kwargs["headers"]["Authorization"])

    @aioresponses()
    def test_all_trade_updates_for_order_converts_fill_response(self, mock_api):
        tracked_order = self._tracked_order(exchange_order_id=self.order_id)
        fills_url = web_utils.private_rest_url(CONSTANTS.FILLS_PATH_URL.format(order_id=self.order_id))
        fills_regex_url = self._regex_url(fills_url)
        mock_api.get(fills_regex_url, body=json.dumps({
            "items": [
                {
                    "maker_order_id": "maker-order-id",
                    "taker_order_id": self.order_id,
                    "quantity": "100.0",
                    "price": "0.75",
                    "settlement_status": "settled",
                    "tx_hash": "0xabc",
                    "timestamp": "2026-04-15T08:00:00+00:00",
                    "settlement_economics": {
                        "fees_paid": [
                            {"token": "USDC", "amount": "0.01", "amount_raw": "10000"},
                        ],
                    },
                },
            ],
        }))

        trade_updates = self.async_run_with_timeout(self.exchange._all_trade_updates_for_order(tracked_order))

        self.assertEqual(1, len(trade_updates))
        trade_update = trade_updates[0]
        self.assertEqual("0xabc", trade_update.trade_id)
        self.assertEqual(Decimal("100.0"), trade_update.fill_base_amount)
        self.assertEqual(Decimal("75.00"), trade_update.fill_quote_amount)
        self.assertEqual(Decimal("0.75"), trade_update.fill_price)
        self.assertEqual("USDC", trade_update.fee.flat_fees[0].token)
        self.assertEqual(Decimal("0.01"), trade_update.fee.flat_fees[0].amount)
        request = self._all_executed_requests(mock_api, fills_url)[0]
        self.assertEqual({"limit": 500, "offset": 0}, request.kwargs["params"])

    @aioresponses()
    def test_update_balances_converts_raw_vault_available_and_total(self, mock_api):
        balances_url = web_utils.private_rest_url(CONSTANTS.BALANCES_PATH_URL)
        balances_regex_url = self._regex_url(balances_url)
        mock_api.get(balances_regex_url, body=json.dumps({
            "balances": [
                {
                    "token": self.base_address,
                    "symbol": self.base_asset,
                    "decimals": 6,
                    "wallet_balance": "1250000000",
                    "vault_available": "400000000",
                    "vault_frozen": "100000000",
                    "vault_total": "500000000",
                    "total": "1750000000",
                },
            ],
        }))

        self.async_run_with_timeout(self.exchange._update_balances())

        self.assertEqual(Decimal("1750"), self.exchange._account_balances[self.base_asset])
        self.assertEqual(Decimal("400"), self.exchange._account_available_balances[self.base_asset])
        request = self._all_executed_requests(mock_api, balances_url)[0]
        self.assertEqual(
            {"owner_address": self.wallet_address.lower()},
            request.kwargs["params"],
        )

    @staticmethod
    def _regex_url(url: str):
        return re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

    @staticmethod
    def _all_executed_requests(api_mock: aioresponses, url):
        request_calls = []
        for key, value in api_mock.requests.items():
            req_url = key[1].human_repr()
            its_a_match = (
                url.search(req_url)
                if isinstance(url, re.Pattern)
                else req_url == url or req_url.startswith(f"{url}?")
            )
            if its_a_match:
                request_calls.extend(value)
        return request_calls

    def _tracked_order(self, exchange_order_id: str) -> InFlightOrder:
        return InFlightOrder(
            client_order_id="client-order-id",
            exchange_order_id=exchange_order_id,
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1000"),
            price=Decimal("1.085"),
            creation_timestamp=1234567890,
        )
