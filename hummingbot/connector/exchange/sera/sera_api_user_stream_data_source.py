import asyncio

from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource


class SeraAPIUserStreamDataSource(UserStreamTrackerDataSource):
    async def listen_for_user_stream(self, output: asyncio.Queue):
        while True:
            await self._sleep(60.0)
