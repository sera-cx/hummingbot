import asyncio
from decimal import Decimal
from typing import Dict, List, Optional, Union

import aiohttp

from hummingbot.connector.utils import combine_to_hb_trading_pair, split_hb_trading_pair
from hummingbot.core.rate_oracle.sources.rate_source_base import RateSourceBase
from hummingbot.core.utils import async_ttl_cache
from hummingbot.core.utils.async_utils import safe_gather


class WiseRateSource(RateSourceBase):
    BASE_URL = "https://api.wise.com"
    QUOTES_PATH = "/v3/quotes"
    DEFAULT_SOURCE_AMOUNT = Decimal("100")
    DEFAULT_CURRENCY_MAP = {
        "EURC": "EUR",
        "USDC": "USD",
        "USDT": "USD",
        "DAI": "USD",
    }

    def __init__(
        self,
        trading_pairs: Optional[Union[str, List[str]]] = None,
        currency_map: Optional[Union[str, Dict[str, str]]] = None,
        source_amount: Decimal = DEFAULT_SOURCE_AMOUNT,
    ):
        super().__init__()
        self._trading_pairs = self._normalize_trading_pairs(trading_pairs)
        self._currency_map = {
            **self.DEFAULT_CURRENCY_MAP,
            **self._normalize_currency_map(currency_map),
        }
        self._source_amount = Decimal(str(source_amount))

    @property
    def name(self) -> str:
        return "wise"

    @async_ttl_cache(ttl=30, maxsize=100)
    async def _get_quote_rate(self, source_currency: str, target_currency: str) -> Decimal:
        url = f"{self.BASE_URL}{self.QUOTES_PATH}"
        payload = {
            "sourceCurrency": source_currency,
            "targetCurrency": target_currency,
            "sourceAmount": float(self._source_amount),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()
                data = await response.json()
        return Decimal(str(data["rate"]))

    async def get_prices_for_pairs(self, trading_pairs: List[str]) -> Dict[str, Decimal]:
        results = {}
        tasks = []
        task_pairs = []
        for trading_pair in trading_pairs:
            source_currency, target_currency = self._wise_currencies_for_pair(trading_pair)
            tasks.append(self._get_quote_rate(source_currency, target_currency))
            task_pairs.append(trading_pair)

        task_results = await safe_gather(*tasks, return_exceptions=True)
        for trading_pair, task_result in zip(task_pairs, task_results):
            if isinstance(task_result, Exception):
                self.logger().error(
                    msg=f"Unexpected error while retrieving Wise rate for {trading_pair}.",
                    exc_info=task_result,
                )
            elif task_result > Decimal("0"):
                results[trading_pair] = task_result
        return results

    async def get_prices(self, quote_token: Optional[str] = None) -> Dict[str, Decimal]:
        trading_pairs = list(self._trading_pairs)
        if quote_token is not None and not trading_pairs:
            trading_pairs = [
                pair for pair in trading_pairs
                if self._map_currency(split_hb_trading_pair(pair)[1]) == quote_token.upper()
            ]
        return await self.get_prices_for_pairs(trading_pairs)

    def _wise_currencies_for_pair(self, trading_pair: str):
        base, quote = split_hb_trading_pair(trading_pair)
        return self._map_currency(base), self._map_currency(quote)

    def _map_currency(self, currency: str) -> str:
        currency = currency.upper()
        return self._currency_map.get(currency, currency)

    @classmethod
    def _normalize_trading_pairs(cls, trading_pairs: Optional[Union[str, List[str]]]) -> List[str]:
        if trading_pairs is None:
            return []
        if isinstance(trading_pairs, str):
            trading_pairs = trading_pairs.split(",")
        return [
            combine_to_hb_trading_pair(*split_hb_trading_pair(pair.strip().upper()))
            for pair in trading_pairs
            if pair.strip()
        ]

    @classmethod
    def _normalize_currency_map(cls, currency_map: Optional[Union[str, Dict[str, str]]]) -> Dict[str, str]:
        if currency_map is None:
            return {}
        if isinstance(currency_map, str):
            if not currency_map:
                return {}
            entries = [entry.split(":") for entry in currency_map.split(",") if entry.strip()]
            return {key.strip().upper(): value.strip().upper() for key, value in entries}
        return {key.upper(): value.upper() for key, value in currency_map.items()}

    async def _sleep(self, delay: float):
        await asyncio.sleep(delay)
