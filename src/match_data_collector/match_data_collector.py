import datetime
from typing import List

from SmiteProvider import SmiteProvider
from HirezAPI import QueueId

class MatchDataCollector:
    __provider: SmiteProvider
    __start_date: datetime.datetime
    __queues: List[QueueId]

    def __init__(
            self,
            provider: SmiteProvider,
            start_date: datetime.datetime,
            queues: List[QueueId] = None):
        self.__provider = provider
        self.__start_date = start_date
        self.__queues = queues or list(
            filter(lambda q: q.is_normal() or q.is_ranked(), list(QueueId)))

    async def __fetch_match_ids(self):
        match_ids = []
        date = self.__start_date

        while date < datetime.datetime.utcnow():
            for hour in range(0, 24):
                for minute in range(0, 6):
                    for queue in self.__queues:
                        matches = await self.__provider.get_match_ids_by_queue(
                            queue,
                            date.strftime('%Y%m%d'),
                            hour,
                            minute * 10)
                        match_ids.extend(
                            [match['Match'] for match in \
                                list(filter(lambda m: m['Active_Flag'] == 'n', matches))])
            date = date + datetime.timedelta(days=1)
        return match_ids

    @staticmethod
    def __chunk_matches(matches: List, chunk_size: int = 10):
        for i in range(0, len(matches), chunk_size):
            yield matches[i:i + chunk_size]

    async def __fetch_match_details(self):
        match_ids = self.__fetch_match_ids()

        for id_chunk in self.__chunk_matches(match_ids):
            match_details = await self.__provider.get_match_details_batch(id_chunk)