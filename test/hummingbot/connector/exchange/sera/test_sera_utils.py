import unittest
from decimal import Decimal

from hummingbot.connector.exchange.sera import sera_utils as utils


class SeraUtilsTests(unittest.TestCase):
    def test_is_exchange_information_valid(self):
        valid_market = {
            "symbol": "EURC/USDC",
            "base_address": "0x0000000000000000000000000000000000000001",
            "quote_address": "0x0000000000000000000000000000000000000002",
        }
        invalid_market = {
            "symbol": "EURC/USDC",
            "base_address": "0x0000000000000000000000000000000000000001",
        }

        self.assertTrue(utils.is_exchange_information_valid(valid_market))
        self.assertFalse(utils.is_exchange_information_valid(invalid_market))

    def test_config_map_and_default_fees(self):
        self.assertEqual("sera", utils.KEYS.connector)
        self.assertEqual("EURC-USDC", utils.EXAMPLE_PAIR)
        self.assertEqual(Decimal("0"), utils.DEFAULT_FEES.maker_percent_fee_decimal)
        self.assertEqual(Decimal("0"), utils.DEFAULT_FEES.taker_percent_fee_decimal)
        self.assertTrue(utils.DEFAULT_FEES.buy_percent_fee_deducted_from_returns)
