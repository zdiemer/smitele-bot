from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from typing import List

import pandas as pd
from aiohttp.client_exceptions import ClientConnectionError, ContentTypeError

import match_storage
import paths
from match_storage import frame_for_storage as match_data_frame_for_storage
from SmiteProvider import SmiteProvider
from HirezAPI import QueueId


class MatchDataCollector:
    __provider: SmiteProvider
    __start_date: datetime.datetime
    __queues: List[QueueId]

    __OUTPUT_FILE_PREFIX: str = "match_details_"
    __OUTPUT_FILE_DATE_FORMAT: str = "%Y-%m-%d"

    __ARCHIVE_CUTOFF_DAYS: int = 30

    def __init__(
        self,
        smite_provider: SmiteProvider,
        start_date: datetime.datetime,
        queues: List[QueueId] = None,
    ):
        self.__provider = smite_provider
        self.__start_date = start_date
        self.__queues = queues or list(
            filter(
                lambda q: QueueId.is_normal(q) or QueueId.is_ranked(q),
                list(QueueId),
            )
        )

    def __get_output_file_name(self):
        return f"{self.__OUTPUT_FILE_PREFIX}{self.__start_date.strftime(self.__OUTPUT_FILE_DATE_FORMAT)}.parquet"

    async def __fetch_match_ids(self):
        match_ids = set()

        for hour in range(0, 24):
            for minute in range(0, 6):
                match_count_batch = 0
                for queue in self.__queues:
                    try:
                        matches = await self.__provider.get_match_ids_by_queue(
                            queue,
                            self.__start_date.strftime("%Y%m%d"),
                            hour,
                            minute * 10,
                        )
                        match_ids = match_ids.union(
                            [
                                match["Match"]
                                for match in list(
                                    filter(lambda m: m["Active_Flag"] == "n", matches)
                                )
                            ]
                        )
                        match_count_batch += len(matches)
                    except ClientConnectionError:
                        continue

                print(
                    f"Fetched {match_count_batch} match IDs for {hour:02d}:{minute*10:02d}",
                    flush=True,
                )
        print(f"Fetched {len(match_ids)} match IDs total", flush=True)
        return list(match_ids)

    @staticmethod
    def __chunk_matches(matches: List, chunk_size: int = 10) -> list:
        for i in range(0, len(matches), chunk_size):
            yield matches[i : i + chunk_size]

    async def __fetch_match_details(self):
        match_ids = await self.__fetch_match_ids()
        match_details = []
        start = datetime.datetime.utcnow()
        last_log = datetime.datetime.utcnow() - datetime.timedelta(seconds=5)
        processed_count = 0
        total_match_ids = len(match_ids)

        for id_chunk in self.__chunk_matches(match_ids):
            req_count = 0
            while req_count < self.__provider.MAX_RETRIES:
                try:
                    match_res = await self.__provider.get_match_details_batch(id_chunk)
                    if match_res is not None:
                        match_details.extend(
                            list(filter(lambda m: m is not None, match_res))
                        )
                    processed_count += len(id_chunk)
                    break
                except (
                    json.JSONDecodeError,
                    ClientConnectionError,
                    ContentTypeError,
                    TypeError,
                ):
                    pass
                req_count += 1

            chunk_time = datetime.datetime.utcnow()
            elapsed = chunk_time - start
            # Every chunk so far can have exhausted its retries, leaving nothing
            # processed and no rate to extrapolate from.
            if processed_count == 0:
                continue
            estimated_s = (total_match_ids - processed_count) * (
                elapsed.total_seconds() / processed_count
            )
            estimated = datetime.timedelta(seconds=estimated_s)

            if last_log <= (datetime.datetime.utcnow() - datetime.timedelta(seconds=5)):
                print(
                    f"Processed {processed_count}/{total_match_ids} ({(processed_count/total_match_ids)*100:,.2f}%) match IDs. Elapsed: {elapsed}, Estimated: {estimated}",
                    flush=True,
                )
                last_log = datetime.datetime.utcnow()

        return match_details

    async def __fetch_and_save_daily_matches(self):
        match_details = await self.__fetch_match_details()

        if not any(match_details):
            print("No match details fetched; nothing written", flush=True)
            return

        output_path = os.path.join(paths.MATCH_DATA_DIR, self.__get_output_file_name())

        # Parquet, not JSON. A day of raw records is ~142MB as JSON and ~6MB
        # here, and the bot reads it back roughly two orders of magnitude
        # faster because it can project just the columns it wants instead of
        # parsing every field of every row.
        #
        # Every column is kept, not just the ones SmiteProvider currently uses:
        # the saving is already 25x, and a day that has passed cannot be
        # re-collected if the set of interesting columns ever changes.
        frame = pd.DataFrame.from_records(match_details)
        frame = match_data_frame_for_storage(frame)
        frame.to_parquet(output_path, compression="zstd", index=False)

        print(
            f"Wrote {frame.shape[0]:,} player rows x {frame.shape[1]} columns "
            f"({os.path.getsize(output_path)/1e6:,.1f} MB) to {output_path}",
            flush=True,
        )

    def __archive_historical_matches(self):
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(
            days=self.__ARCHIVE_CUTOFF_DAYS
        )

        for root, _, files in os.walk(paths.MATCH_DATA_DIR):
            for name in files:
                file_date = self.__date_from_name(name)
                # Anything that isn't one of our dated files gets left alone.
                # This used to raise straight out of the job on the first
                # unexpected name in the directory.
                if file_date is None or file_date > cutoff:
                    continue

                os.rename(
                    os.path.join(root, name),
                    os.path.join(paths.MATCH_ARCHIVE_DIR, name),
                )

    def __date_from_name(self, name: str) -> datetime.datetime:
        """The day a corpus file covers, or None if it isn't one.

        Both suffixes are recognised: rotation still has to work on the JSON
        days collected before the switch to Parquet.
        """
        for suffix in (match_storage.SUFFIX, ".json"):
            try:
                return datetime.datetime.strptime(
                    name,
                    f"{self.__OUTPUT_FILE_PREFIX}{self.__OUTPUT_FILE_DATE_FORMAT}{suffix}",
                )
            except ValueError:
                continue
        return None

    async def run_job(self):
        print(
            f"Fetching matches for {self.__start_date.strftime('%B %d, %Y')}",
            flush=True,
        )
        await self.__fetch_and_save_daily_matches()
        print(
            f"Archiving match data older than {self.__ARCHIVE_CUTOFF_DAYS} days",
            flush=True,
        )
        self.__archive_historical_matches()


def _start_date() -> datetime.datetime:
    """The day to collect, defaulting to yesterday.

    Hi-Rez only exposes match IDs for completed days, so "yesterday" is the
    newest thing worth asking for and is what the daily schedule wants. An
    explicit YYYY-MM-DD (argument or SMITELE_COLLECT_DATE) is for backfilling a
    day the job missed.
    """
    override = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SMITELE_COLLECT_DATE")
    )
    if override:
        return datetime.datetime.strptime(override, "%Y-%m-%d")
    return datetime.datetime.utcnow() - datetime.timedelta(days=1)


if __name__ == "__main__":
    provider = SmiteProvider(silent=True)
    asyncio.run(provider.create())
    collector = MatchDataCollector(provider, _start_date())
    asyncio.run(collector.run_job())
