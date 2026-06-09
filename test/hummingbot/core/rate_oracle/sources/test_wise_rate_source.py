from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, patch

from hummingbot.core.rate_oracle.sources.wise_rate_source import WiseRateSource


class WiseRateSourceTest(IsolatedAsyncioWrapperTestCase):
    async def test_get_prices_for_pairs_requests_mapped_quote(self):
        rate_source = WiseRateSource(currency_map="EURC:EUR,USDC:USD")
        with patch.object(rate_source, "_get_quote_rate", new=AsyncMock(return_value=Decimal("1.0875"))) as quote_mock:
            prices = await rate_source.get_prices_for_pairs(["EURC-USDC"])

        self.assertEqual(Decimal("1.0875"), prices["EURC-USDC"])
        quote_mock.assert_awaited_once_with("EUR", "USD")

    async def test_get_prices_uses_configured_pairs_and_quote_filter(self):
        rate_source = WiseRateSource(
            trading_pairs="EURC-USDC,EURC-GBP",
            currency_map="EURC:EUR,USDC:USD",
        )
        with patch.object(rate_source, "_get_quote_rate", new=AsyncMock(return_value=Decimal("1.0875"))) as quote_mock:
            prices = await rate_source.get_prices(quote_token="USD")

        self.assertEqual({"EURC-USDC": Decimal("1.0875")}, prices)
        quote_mock.assert_awaited_once_with("EUR", "USD")
