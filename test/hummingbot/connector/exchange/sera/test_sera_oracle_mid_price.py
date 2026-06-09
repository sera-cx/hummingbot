import unittest
from decimal import Decimal

from hummingbot.connector.exchange.sera.sera_exchange import SeraExchange
from hummingbot.core.data_type.common import PriceType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_row import OrderBookRow
from hummingbot.core.rate_oracle.rate_oracle import RateOracle


class SeraOracleMidPriceTest(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        RateOracle._shared_instance = None
        self.trading_pair = "EURC-USDC"
        self.exchange = SeraExchange(
            sera_api_key="apiKey",
            sera_api_secret="apiSecret",
            sera_wallet_address="0x0000000000000000000000000000000000000001",
            sera_wallet_private_key="13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930",  # noqa: mock
            trading_pairs=[self.trading_pair],
        )
        self.exchange.order_book_tracker.order_books[self.trading_pair] = OrderBook()

    def tearDown(self) -> None:
        self.exchange.order_book_tracker.stop()
        RateOracle._shared_instance = None
        super().tearDown()

    def test_get_mid_price_falls_back_to_rate_oracle_when_order_book_is_empty(self):
        RateOracle.get_instance().set_price(self.trading_pair, Decimal("1.0875"))

        self.assertEqual(Decimal("1.0875"), self.exchange.get_mid_price(self.trading_pair))
        self.assertEqual(
            Decimal("1.0875"),
            self.exchange.get_price_by_type(self.trading_pair, PriceType.MidPrice),
        )

    def test_get_mid_price_uses_order_book_when_available(self):
        RateOracle.get_instance().set_price(self.trading_pair, Decimal("1.0875"))
        order_book = OrderBook()
        order_book.apply_diffs(
            [OrderBookRow(Decimal("1.08"), Decimal("10"), 1)],
            [OrderBookRow(Decimal("1.12"), Decimal("10"), 1)],
            1,
        )
        self.exchange.order_book_tracker.order_books[self.trading_pair] = order_book

        self.assertEqual(Decimal("1.10"), self.exchange.get_mid_price(self.trading_pair))
        self.assertEqual(
            Decimal("1.10"),
            self.exchange.get_price_by_type(self.trading_pair, PriceType.MidPrice),
        )

    def test_set_mid_price_from_price_oracle_sets_manual_mid_price(self):
        RateOracle.get_instance().set_price(self.trading_pair, Decimal("1.0875"))

        self.assertEqual(Decimal("1.0875"), self.exchange.set_mid_price_from_price_oracle(self.trading_pair))

        RateOracle.get_instance().set_price(self.trading_pair, Decimal("1.1000"))
        self.assertEqual(Decimal("1.0875"), self.exchange.get_mid_price(self.trading_pair))
