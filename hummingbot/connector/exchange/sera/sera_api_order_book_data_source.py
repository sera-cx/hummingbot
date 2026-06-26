import time
from typing import Dict, List, Optional

from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource


class SeraAPIOrderBookDataSource(OrderBookTrackerDataSource):
    def __init__(self, trading_pairs: List[str], connector):
        super().__init__(trading_pairs=trading_pairs)
        self._connector = connector

    async def get_last_traded_prices(self, trading_pairs: List[str], domain: Optional[str] = None) -> Dict[str, float]:
        prices = {}
        for trading_pair in trading_pairs:
            prices[trading_pair] = await self._connector._get_last_traded_price(trading_pair=trading_pair)
        return prices

    async def listen_for_subscriptions(self):
        while True:
            await self._sleep(60.0)

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        timestamp = time.time()
        return OrderBookMessage(
            message_type=OrderBookMessageType.SNAPSHOT,
            content={
                "trading_pair": trading_pair,
                "update_id": int(timestamp * 1e6),
                "bids": [],
                "asks": [],
            },
            timestamp=timestamp,
        )

    async def subscribe_to_trading_pair(self, trading_pair: str):
        pass

    async def unsubscribe_from_trading_pair(self, trading_pair: str):
        pass
