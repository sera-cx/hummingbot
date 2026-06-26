import unittest
from decimal import Decimal
from unittest.mock import AsyncMock

from hummingbot.connector.exchange.sera.sera_exchange import SeraExchange
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.clock import Clock, ClockMode
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_row import OrderBookRow
from hummingbot.core.rate_oracle.rate_oracle import RateOracle
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple
from hummingbot.strategy.pure_market_making.pure_market_making import PureMarketMakingStrategy


class SeraPureMarketMakingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "EURC"
        cls.quote_asset = "USDC"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.private_key = "13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930"  # noqa: mock
        cls.wallet_address = "0x1dD6A2730b4f5C154511dBf92de1dC9D8B905Bb6"

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
        self.exchange._market_info[self.trading_pair] = {
            "symbol": "EURC/USDC",
            "base_symbol": self.base_asset,
            "quote_symbol": self.quote_asset,
            "base_address": "0x1",
            "quote_address": "0x2",
            "amount_step": "0.01",
            "price_step": "0.0001",
            "min_ask_amount": "0",
            "min_bid_quote_amount": "8.800000",
            "base_decimals": 6,
            "quote_decimals": 6,
        }
        self.exchange._trading_rules = {
            self.trading_pair: TradingRule(
                trading_pair=self.trading_pair,
                min_order_size=Decimal("0.01"),
                min_price_increment=Decimal("0.0001"),
                min_base_amount_increment=Decimal("0.01"),
                min_notional_size=Decimal("8.8"),
            )
        }
        # Mock API methods to prevent network calls
        self.exchange._api_get = AsyncMock()
        self.exchange._api_post = AsyncMock()

        # Add a fake orderbook to satisfy mid-price checks before manual price is set
        order_book = OrderBook()
        order_book.apply_diffs([OrderBookRow(99.0, 10.0, 1)], [OrderBookRow(101.0, 10.0, 1)], 1)
        self.exchange.order_book_tracker.order_books[self.trading_pair] = order_book

        # Set fake balances to allow orders to be created
        self.exchange._account_balances[self.base_asset] = Decimal("1000")
        self.exchange._account_available_balances[self.base_asset] = Decimal("1000")
        self.exchange._account_balances[self.quote_asset] = Decimal("100000")
        self.exchange._account_available_balances[self.quote_asset] = Decimal("100000")

        self.market_info = MarketTradingPairTuple(self.exchange, self.trading_pair, self.base_asset, self.quote_asset)

        self.clock_tick_size = 1
        self.clock = Clock(ClockMode.BACKTEST, self.clock_tick_size, 1640000000.0, 1640001000.0)
        self.clock.add_iterator(self.exchange)

        self.strategy = PureMarketMakingStrategy()
        self.strategy.init_params(
            self.market_info,
            bid_spread=Decimal("0.01"),
            ask_spread=Decimal("0.01"),
            order_amount=Decimal("10"),
            order_refresh_time=5.0,
            filled_order_delay=5.0,
            order_refresh_tolerance_pct=-1,
            minimum_spread=-1,
            price_type="mid_price",
        )
        self.clock.add_iterator(self.strategy)

        # Mock the exchange readiness so the strategy proceeds
        from unittest.mock import PropertyMock, patch
        self.ready_patch = patch('hummingbot.connector.exchange.sera.sera_exchange.SeraExchange.ready', new_callable=PropertyMock)
        self.ready_mock = self.ready_patch.start()
        self.ready_mock.return_value = True

    def tearDown(self) -> None:
        self.ready_patch.stop()
        self.exchange.order_book_tracker.stop()
        RateOracle._shared_instance = None
        super().tearDown()

    def test_strategy_initializes_with_sera(self):
        # Ensure strategy correctly identifies the mid price from the orderbook when manual price isn't set
        self.assertEqual(Decimal("100.0"), self.exchange.get_mid_price(self.trading_pair))

        self.clock.backtest_til(self.clock.current_timestamp + 1)
        # Verify strategy places an order around the orderbook mid price (100.0)
        # 1% spread means bid at 99.0 and ask at 101.0
        self.assertEqual(1, len(self.strategy.active_buys))
        self.assertEqual(1, len(self.strategy.active_sells))
        self.assertEqual(Decimal("99.0"), self.strategy.active_buys[0].price)
        self.assertEqual(Decimal("101.0"), self.strategy.active_sells[0].price)

    def test_strategy_uses_manual_mid_price(self):
        # Set manual mid price to 150.0
        self.exchange.set_manual_mid_price(self.trading_pair, Decimal("150.0"))
        # Adjust orderbook so the new orders don't cross the book
        order_book = OrderBook()
        order_book.apply_diffs([OrderBookRow(149.0, 10.0, 2)], [OrderBookRow(151.0, 10.0, 2)], 2)
        self.exchange.order_book_tracker.order_books[self.trading_pair] = order_book

        self.assertEqual(Decimal("150.0"), self.exchange.get_mid_price(self.trading_pair))

        self.clock.backtest_til(self.clock.current_timestamp + 1)

        # Verify strategy places orders around the manual mid price (150.0)
        # 1% spread means bid at 148.5 and ask at 151.5
        self.assertEqual(1, len(self.strategy.active_buys))
        self.assertEqual(1, len(self.strategy.active_sells))
        self.assertAlmostEqual(Decimal("148.5"), self.strategy.active_buys[0].price, places=2)
        self.assertAlmostEqual(Decimal("151.5"), self.strategy.active_sells[0].price, places=2)

    def test_strategy_uses_oracle_mid_price_when_order_book_is_empty(self):
        RateOracle.get_instance().set_price(self.trading_pair, Decimal("100"))
        self.exchange.order_book_tracker.order_books[self.trading_pair] = OrderBook()

        self.assertEqual(Decimal("100"), self.exchange.get_mid_price(self.trading_pair))

        self.clock.backtest_til(self.clock.current_timestamp + 1)

        self.assertEqual(1, len(self.strategy.active_buys))
        self.assertEqual(1, len(self.strategy.active_sells))
        self.assertEqual(Decimal("99.0"), self.strategy.active_buys[0].price)
        self.assertEqual(Decimal("101.0"), self.strategy.active_sells[0].price)
