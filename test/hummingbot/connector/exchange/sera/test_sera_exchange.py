import asyncio
import unittest
from decimal import Decimal
from typing import Awaitable, Dict, List
from unittest.mock import AsyncMock, patch

from hummingbot.connector.exchange.sera import sera_constants as CONSTANTS
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
            sera_private_key=self.private_key,
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
        self.exchange._api_get = AsyncMock()
        self.exchange._api_post = AsyncMock()

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

    @patch.object(SeraAuth, "sign_typed_data", return_value="0xsigned")
    def test_place_order_previews_signs_and_submits_normalized_payload(self, sign_mock):
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
        self.exchange._api_post.side_effect = [preview_response, {"order_id": self.order_id}]

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
        preview_call = self.exchange._api_post.call_args_list[0]
        self.assertEqual(CONSTANTS.PREVIEW_ORDER_PATH_URL, preview_call.kwargs["path_url"])
        preview_payload = preview_call.kwargs["data"]
        self.assertEqual(self.wallet_address.lower(), preview_payload["owner_address"])
        self.assertEqual(CONSTANTS.SIDE_BID, preview_payload["side"])
        self.assertEqual(self.base_address, preview_payload["from_address"])
        self.assertEqual(self.quote_address, preview_payload["to_address"])
        self.assertEqual(self.order_id, preview_payload["order_id"])

        order_call = self.exchange._api_post.call_args_list[1]
        self.assertEqual(CONSTANTS.ORDERS_PATH_URL, order_call.kwargs["path_url"])
        order_payload = order_call.kwargs["data"]
        self.assertEqual("1000", order_payload["amount"])
        self.assertEqual("1.085", order_payload["price"])
        self.assertEqual("0xsigned", order_payload["signature"])
        sign_mock.assert_called_once_with(
            domain=self.eip712_domain,
            message_types=CONSTANTS.ORDER_TYPES,
            message=preview_response["eip712_order"],
        )

    @patch.object(SeraAuth, "sign_typed_data", return_value="0xcancel")
    def test_place_cancel_signs_cancel_order_payload(self, sign_mock):
        tracked_order = self._tracked_order(exchange_order_id=self.order_id)
        uuid_int = self.exchange._encode_standalone_uuid(self.order_id, self.exchange._executor_id)
        self.exchange._order_uuid_ints[self.order_id] = uuid_int
        self.exchange._api_post.return_value = {"status": "ok"}

        cancelled = self.async_run_with_timeout(self.exchange._place_cancel(
            order_id=tracked_order.client_order_id,
            tracked_order=tracked_order,
        ))

        self.assertTrue(cancelled)
        self.exchange._api_get.assert_not_called()
        cancel_call = self.exchange._api_post.call_args
        self.assertEqual(CONSTANTS.CANCEL_ORDER_PATH_URL, cancel_call.kwargs["path_url"])
        self.assertEqual({
            "owner_address": self.wallet_address.lower(),
            "order_id": self.order_id,
            "uuid_int": uuid_int,
            "signature": "0xcancel",
        }, cancel_call.kwargs["data"])
        sign_mock.assert_called_once_with(
            domain=self.eip712_domain,
            message_types=CONSTANTS.CANCEL_ORDER_TYPES,
            message={"owner": self.wallet_address.lower(), "orderId": int(uuid_int)},
        )

    def test_request_order_status_maps_pending_with_fill_to_partially_filled(self):
        tracked_order = self._tracked_order(exchange_order_id=self.order_id)
        uuid_int = self.exchange._encode_standalone_uuid(self.order_id, self.exchange._executor_id)
        self.exchange._api_get.return_value = {
            "trade_id": self.order_id,
            "status": "pending",
            "filled_base_amount": "400.0",
            "updated_at": "2026-04-15T08:01:00+00:00",
            "uuid_int": uuid_int,
        }

        order_update = self.async_run_with_timeout(self.exchange._request_order_status(tracked_order=tracked_order))

        self.assertEqual(OrderState.PARTIALLY_FILLED, order_update.new_state)
        self.assertEqual(self.order_id, order_update.exchange_order_id)
        self.assertEqual(uuid_int, self.exchange._order_uuid_ints[self.order_id])
        self.assertEqual(CONSTANTS.ORDER_PATH_URL, self.exchange._api_get.call_args.kwargs["limit_id"])

    def test_all_trade_updates_for_order_converts_fill_response(self):
        tracked_order = self._tracked_order(exchange_order_id=self.order_id)
        self.exchange._api_get.return_value = {
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
        }

        trade_updates = self.async_run_with_timeout(self.exchange._all_trade_updates_for_order(tracked_order))

        self.assertEqual(1, len(trade_updates))
        trade_update = trade_updates[0]
        self.assertEqual("0xabc", trade_update.trade_id)
        self.assertEqual(Decimal("100.0"), trade_update.fill_base_amount)
        self.assertEqual(Decimal("75.00"), trade_update.fill_quote_amount)
        self.assertEqual(Decimal("0.75"), trade_update.fill_price)
        self.assertEqual("USDC", trade_update.fee.flat_fees[0].token)
        self.assertEqual(Decimal("0.01"), trade_update.fee.flat_fees[0].amount)

    def test_update_balances_converts_raw_vault_available_and_total(self):
        self.exchange._api_get.return_value = {
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
        }

        self.async_run_with_timeout(self.exchange._update_balances())

        self.assertEqual(Decimal("1750"), self.exchange._account_balances[self.base_asset])
        self.assertEqual(Decimal("400"), self.exchange._account_available_balances[self.base_asset])
        self.assertEqual(
            {"owner_address": self.wallet_address.lower()},
            self.exchange._api_get.call_args.kwargs["params"],
        )

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
