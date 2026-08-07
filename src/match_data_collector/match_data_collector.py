from __future__ import annotations

import asyncio
import datetime
import glob
import os
import sys
from typing import List, Optional

import pandas as pd

import match_storage
import paths
from match_storage import frame_for_storage as match_data_frame_for_storage
from SmiteProvider import SmiteProvider
from HirezAPI import QueueId, TRANSIENT_ERRORS


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
        attempted = 0
        failed = 0

        for hour in range(0, 24):
            for minute in range(0, 6):
                match_count_batch = 0
                for queue in self.__queues:
                    attempted += 1
                    try:
                        matches = await self.__provider.get_match_ids_by_queue(
                            queue,
                            self.__start_date.strftime("%Y%m%d"),
                            hour,
                            minute * 10,
                        )
                    except TRANSIENT_ERRORS:
                        # One queue-window lost, not the run. This used to name
                        # only aiohttp's ClientConnectionError, which is not
                        # what survives `_make_request` — a spent retry budget
                        # arrives as a builtin ConnectionError or a
                        # TimeoutError, and both went straight past this and
                        # killed the whole day's collection.
                        failed += 1
                        print(
                            f"  ! {queue.name} at {hour:02d}:{minute*10:02d} "
                            "gave up after retries",
                            flush=True,
                        )
                        continue

                    # An error object rather than a page of matches: a lone
                    # dict carrying a ret_msg. Iterating it walks its keys, and
                    # subscripting a key raises TypeError out of the whole job.
                    if not isinstance(matches, list):
                        failed += 1
                        print(
                            f"  ! {queue.name} at {hour:02d}:{minute*10:02d} "
                            f"returned {type(matches).__name__}, not a list",
                            flush=True,
                        )
                        continue

                    match_ids.update(
                        match["Match"]
                        for match in matches
                        if isinstance(match, dict) and match.get("Active_Flag") == "n"
                    )
                    match_count_batch += len(matches)

                print(
                    f"Fetched {match_count_batch} match IDs for {hour:02d}:{minute*10:02d}",
                    flush=True,
                )
        print(f"Fetched {len(match_ids)} match IDs total", flush=True)
        if failed:
            # The per-window line above still prints a plausible-looking count
            # from whichever queues did answer, so without this a day missing
            # an entire queue reads as a normal run.
            print(
                f"WARNING: {failed} of {attempted} queue-window "
                "requests failed; this day is incomplete",
                flush=True,
            )
        return list(match_ids)

    @staticmethod
    def __chunk_matches(matches: List, chunk_size: int = 10) -> list:
        for i in range(0, len(matches), chunk_size):
            yield matches[i : i + chunk_size]

    async def __fetch_chunk(self, id_chunk: List, into: List) -> bool:
        """One batch of match details, or False once the retries are spent."""
        req_count = 0
        while req_count < self.__provider.MAX_RETRIES:
            try:
                match_res = await self.__provider.get_match_details_batch(id_chunk)
                if match_res is not None:
                    into.extend(m for m in match_res if m is not None)
                return True
            except TRANSIENT_ERRORS + (TypeError,):
                pass
            req_count += 1
        return False

    async def __fetch_match_details(self):
        match_ids = await self.__fetch_match_ids()
        match_details = []
        dropped = []
        start = datetime.datetime.utcnow()
        last_log = datetime.datetime.utcnow() - datetime.timedelta(seconds=5)
        processed_count = 0
        total_match_ids = len(match_ids)

        for id_chunk in self.__chunk_matches(match_ids):
            if await self.__fetch_chunk(id_chunk, match_details):
                processed_count += len(id_chunk)
            else:
                # Ten matches used to disappear here with no log line and no
                # counter, so a run that lost a third of the day looked exactly
                # like one that lost nothing.
                dropped.extend(id_chunk)

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

        if dropped:
            # These failed together, in a burst, minutes or hours ago. Asking
            # again now — after the burst has passed — recovers most of them,
            # and costs a tenth of a request per match to try.
            print(
                f"Retrying {len(dropped):,} match IDs that failed the first pass",
                flush=True,
            )
            still_dropped = []
            for id_chunk in self.__chunk_matches(dropped):
                if not await self.__fetch_chunk(id_chunk, match_details):
                    still_dropped.extend(id_chunk)
            recovered = len(dropped) - len(still_dropped)
            print(f"Recovered {recovered:,} of {len(dropped):,}", flush=True)
            if still_dropped:
                print(
                    f"WARNING: {len(still_dropped):,} match IDs "
                    f"({len(still_dropped)/total_match_ids:.1%}) could not be "
                    "fetched and are missing from this day",
                    flush=True,
                )

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

    def existing_corpus_file(self) -> Optional[str]:
        """The corpus file already covering this day, if there is one.

        Both directories are searched, because a backfilled day older than the
        archive cutoff is rotated out by the very run that collected it — so
        the next run would find nothing in `output` and collect it again. The
        glob also matches the `.partNNN` names the JSON conversion produced.
        """
        stem = f"{self.__OUTPUT_FILE_PREFIX}{self.__start_date.strftime(self.__OUTPUT_FILE_DATE_FORMAT)}"
        for directory in (paths.MATCH_DATA_DIR, paths.MATCH_ARCHIVE_DIR):
            found = glob.glob(os.path.join(directory, f"{stem}.*"))
            if found:
                return sorted(found)[0]
        return None

    async def run_job(self, skip_if_collected: bool = False):
        if skip_if_collected:
            existing = self.existing_corpus_file()
            if existing is not None:
                print(
                    f"Skipping {self.__start_date.strftime('%B %d, %Y')}; "
                    f"already collected ({os.path.basename(existing)})",
                    flush=True,
                )
                return

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


def _parse_date(value: str) -> datetime.datetime:
    return datetime.datetime.strptime(value, "%Y-%m-%d")


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
        return _parse_date(override)
    return datetime.datetime.utcnow() - datetime.timedelta(days=1)


def _end_date() -> Optional[datetime.datetime]:
    """The last day of a range, for backfilling more than one day.

    `getmatchidsbyqueue` serves a rolling window — 180 days, measured in August
    2026 — so a gap is only recoverable for as long as it stays inside it. The
    range walks oldest first for that reason: the old edge expires a day every
    day, and the newest missing day will still be there next week.
    """
    override = (
        sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SMITELE_COLLECT_UNTIL")
    )
    return _parse_date(override) if override else None


def _max_days() -> Optional[int]:
    """How many days one invocation may collect before stopping.

    A day costs roughly 2,500 requests against a 75,000/day quota that the
    nightly run and the bot also draw on, so a long backfill has to be paced
    across calendar days rather than run to completion in one go. Days already
    on disk are skipped without spending anything and do not count.
    """
    value = os.environ.get("SMITELE_COLLECT_MAX_DAYS")
    return int(value) if value else None


def _dates_to_collect() -> List[datetime.datetime]:
    start, end = _start_date(), _end_date()
    if end is None or end <= start:
        return [start]
    span = (end - start).days
    return [start + datetime.timedelta(days=n) for n in range(span + 1)]


async def _main():
    provider = SmiteProvider(silent=True)
    await provider.create()

    dates = _dates_to_collect()
    ranged = len(dates) > 1
    budget = _max_days()
    collected = 0

    for index, date in enumerate(dates):
        if budget is not None and collected >= budget:
            print(
                f"Reached the {budget}-day limit for this run; "
                f"{len(dates) - index} day(s) still to do",
                flush=True,
            )
            break

        collector = MatchDataCollector(provider, date)
        # Only a range skips: asking for a single day by hand means wanting it
        # re-collected, which is how a day that failed halfway gets repaired.
        if ranged and collector.existing_corpus_file() is not None:
            await collector.run_job(skip_if_collected=True)
            continue

        await collector.run_job()
        collected += 1


if __name__ == "__main__":
    asyncio.run(_main())
