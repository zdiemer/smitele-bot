"""Writing crawled rows into the corpus.

Two things make this different from the Smite 1 collector's single write.

Rows are filed by the day the *match* was played, not the day it was collected.
A page of history spans about three calendar days, so a night's crawl always
touches several — discarding the two that are not "yesterday" would throw away
two thirds of what was paid for.

And a day is never finished. Later nights keep finding matches from it, so a
date's file is appended to rather than written once. Deduplication is against a
single running index of match ids rather than by re-reading the accreting
per-date files, which after a hundred nights would dominate the run.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, List, Set

import pandas as pd

import match_storage

SEEN_FILE = "seen_matches.parquet"

# Matches the Smite 1 collector's naming, and `build_aggregate.corpus_date`'s
# expectations, so the aggregate needs no new file discovery.
OUTPUT_PREFIX = "match_details_"


def output_path(directory: str, date: str) -> str:
    return os.path.join(directory, f"{OUTPUT_PREFIX}{date}{match_storage.SUFFIX}")


class SeenMatches:
    """Every match id already in the corpus, and which day it belongs to.

    Loaded once per run. The alternative — checking each new id against the
    per-date parquet files — reads the whole corpus on every crawl.
    """

    def __init__(self, directory: str):
        self.path = os.path.join(directory, SEEN_FILE)
        self.by_id: Dict[str, str] = {}
        self.__added: Dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            frame = match_storage.read_frame(self.path)
        except Exception as error:  # noqa: BLE001
            print(f"store: could not read {self.path}: {error}", flush=True)
            return
        self.by_id = dict(
            zip(frame["match_id"].astype(str), frame["date"].astype(str))
        )

    def __contains__(self, match_id: str) -> bool:
        return match_id in self.by_id

    def add(self, match_id: str, date: str) -> None:
        self.by_id[match_id] = date
        self.__added[match_id] = date

    def save(self) -> None:
        if not self.__added:
            return
        frame = pd.DataFrame(
            {
                "match_id": list(self.by_id.keys()),
                "date": list(self.by_id.values()),
            }
        )
        partial = f"{self.path}.partial"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        frame.to_parquet(partial, compression="zstd", index=False)
        os.replace(partial, self.path)
        self.__added = {}

    def __len__(self) -> int:
        return len(self.by_id)


def merge_day(directory: str, date: str, rows: List[dict]) -> int:
    """Append a day's new rows to its file, atomically.

    Read, concatenate, write beside, rename. The bot may be reading the corpus
    at the same time, and a rename is the only way it never sees a half-written
    file. Reads go through `match_storage`'s retry because the share is SMB and
    reads there flake mid-stream.
    """
    if not rows:
        return 0

    destination = output_path(directory, date)
    frame = pd.DataFrame.from_records(rows)

    if os.path.isfile(destination):
        try:
            existing = match_storage.read_frame(destination)
            frame = pd.concat([existing, frame], ignore_index=True)
        except Exception as error:  # noqa: BLE001
            # Refusing to write is worse than the alternative here: the day's
            # existing rows are already safe on disk, and losing this crawl's
            # additions costs one night.
            print(
                f"store: could not read {destination} to append ({error}); "
                "leaving it alone",
                flush=True,
            )
            return 0

    frame = match_storage.frame_for_storage(frame)
    os.makedirs(directory, exist_ok=True)
    partial = f"{destination}.partial"
    frame.to_parquet(partial, compression="zstd", index=False)
    os.replace(partial, destination)
    return len(rows)


class RowBuffer:
    """Rows waiting to be written, grouped by the day they belong to.

    Flushed periodically rather than at the end so a run that is killed —
    by the CronJob deadline, say — has still contributed most of its work.
    """

    def __init__(self, directory: str, flush_every: int = 50_000):
        self.directory = directory
        self.flush_every = flush_every
        self.__pending: Dict[str, List[dict]] = {}
        self.__count = 0
        self.written = 0

    def add(self, date: str, row: dict) -> None:
        self.__pending.setdefault(date, []).append(row)
        self.__count += 1

    @property
    def pending(self) -> int:
        return self.__count

    def maybe_flush(self) -> int:
        if self.__count < self.flush_every:
            return 0
        return self.flush()

    def flush(self) -> int:
        written = 0
        for date, rows in sorted(self.__pending.items()):
            written += merge_day(self.directory, date, rows)
        self.__pending = {}
        self.__count = 0
        self.written += written
        return written
