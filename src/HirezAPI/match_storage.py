"""On-disk format for the match-detail corpus.

The collector used to dump the raw API response as JSON, which cost ~142MB for
a single day and about ten seconds and a gigabyte of resident memory to read
back — for a frame the bot then trimmed to 23 columns. Parquet stores the same
day in ~6MB and, being columnar, lets the reader ask for only those columns and
skip the rest of the file entirely.

Three shapes therefore exist on disk, and everything here exists to read all of
them so no historical data is stranded:

  *.parquet   what the collector writes now.
  *.json      a flat list of player rows — what the collector used to write.
  *.json      a dict keyed by match ID, each value the ten rows for that match
              — older archives again.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List

import pandas as pd
import pyarrow.parquet as pq
import ujson as json

SUFFIX: str = ".parquet"


def is_corpus_file(name: str) -> bool:
    return name.endswith(SUFFIX) or name.endswith(".json")


def frame_for_storage(frame: pd.DataFrame) -> pd.DataFrame:
    """Make a raw-record frame safe to serialise.

    Hi-Rez returns a handful of fields with inconsistent types across rows —
    an int for most players and a string for some. Arrow infers one type per
    column and raises on the mismatch, so those columns are normalised to text.
    Columns with a clean inferred type are left exactly as they are.
    """
    for column in frame.columns:
        if frame[column].dtype != object:
            continue
        try:
            pd.api.types.infer_dtype(frame[column], skipna=True)
            frame[column] = frame[column].astype(
                "string" if _is_mixed(frame[column]) else frame[column].dtype
            )
        except (TypeError, ValueError):
            frame[column] = frame[column].astype("string")
    return frame


def _is_mixed(series: pd.Series) -> bool:
    inferred = pd.api.types.infer_dtype(series, skipna=True)
    return inferred.startswith("mixed") or inferred == "unknown-array"


def read_frame(path: str, exclude: Iterable[str] = ()) -> pd.DataFrame:
    """Read one corpus file, dropping `exclude` as early as the format allows.

    For Parquet that means never reading those columns off disk at all, which
    is where most of the speedup comes from.
    """
    excluded = set(exclude)

    if path.endswith(SUFFIX):
        wanted = [name for name in pq.read_schema(path).names if name not in excluded]
        return pd.read_parquet(path, columns=wanted)

    with open(path, "r", encoding="utf-8") as file:
        records = json.loads(file.read())

    return pd.DataFrame.from_records(
        _flatten(records),
        exclude=[column for column in excluded if column],
    )


def _flatten(records: Any) -> List[Dict[str, Any]]:
    """Normalise either JSON shape to a flat list of player rows.

    Filtering a dict walks its keys, so the match-ID-keyed shape used to
    produce a frame built from ID strings rather than from matches.
    """
    if isinstance(records, dict):
        rows: List[Dict[str, Any]] = []
        for value in records.values():
            if value is None:
                continue
            rows.extend(row for row in value if row is not None)
        return rows

    return [row for row in records if row is not None]


def corpus_paths(*directories: str) -> List[str]:
    """Every corpus file across the given directories, oldest name first.

    The bot reads its live directory and its archive together, so history that
    has rotated out still feeds builds.
    """
    found: List[str] = []
    for directory in directories:
        if not directory or not os.path.isdir(directory):
            continue
        for root, _, files in os.walk(directory):
            found.extend(
                os.path.join(root, name) for name in files if is_corpus_file(name)
            )
    return sorted(found, key=os.path.basename)
