"""Convert JSON corpus files to Parquet.

The collector writes Parquet now, but historical archives are JSON — either a
flat list of player rows or a dict keyed by match id. Both are readable, but a
day is roughly 25x larger as JSON and reads two orders of magnitude slower.

The files are streamed, not loaded. Parsing a day whole costs about eight times
its own size in Python objects, and archived days reach 3GB, which would need
~24GB of memory for one file and would not fit on any node here. ijson's C
backend walks the document incrementally instead, so peak memory is set by
--batch-rows rather than by the size of the input.

Each batch is written as its own Parquet part. Sharing one schema across
batches would mean guessing the type of a column that happens to be entirely
null in the first batch and populated later; separate parts each describe
themselves, and the corpus reader already tolerates files whose columns differ.

    python src/match_data_collector/convert_corpus.py [--delete] [DIR ...]

Without --delete the JSON is left in place, so the day would be loaded twice —
once from each format. The normal sequence is convert, confirm, then re-run
with --delete.

Shardable for use as an Indexed Job: each worker takes every Nth file from the
same sorted list, so no coordination is needed and no two workers touch the
same file.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Iterator, List

import ijson
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "HirezAPI"))

import match_storage  # noqa: E402  pylint: disable=wrong-import-position
import paths  # noqa: E402  pylint: disable=wrong-import-position

_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# Peak memory is roughly this many rows held as Python dicts, plus the frame
# built from them. 50k keeps a worker inside ~1.5GB, which fits the small nodes
# and so allows real parallelism rather than two big-memory pods.
DEFAULT_BATCH_ROWS: int = 50_000


def output_stem(json_path: str) -> str:
    """Where a JSON corpus file's Parquet parts should be written.

    Keyed on the date in the filename so archives written as
    matchDetails_2024-01-09.json land beside collector output rather than under
    a second naming convention.
    """
    directory, name = os.path.split(json_path)
    found = _DATE.search(name)
    stem = f"match_details_{found.group(1)}" if found else os.path.splitext(name)[0]
    return os.path.join(directory, stem)


def stream_rows(path: str) -> Iterator[dict]:
    """Yield player rows from either JSON shape without holding the document.

    The top-level shape is decided by the first non-whitespace byte: a list of
    player rows, or a mapping of match id to that match's rows.
    """
    with open(path, "rb") as probe:
        head = probe.read(64).lstrip()
    is_mapping = head[:1] == b"{"

    with open(path, "rb") as handle:
        if is_mapping:
            for _, rows in ijson.kvitems(handle, "", use_float=True):
                for row in rows or []:
                    if row is not None:
                        yield row
        else:
            for row in ijson.items(handle, "item", use_float=True):
                if row is not None:
                    yield row


def parts_for(stem: str) -> List[str]:
    directory, prefix = os.path.split(stem)
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory or ".")
        if name.startswith(f"{prefix}.part") and name.endswith(match_storage.SUFFIX)
    )


def convert(json_path: str, delete: bool, batch_rows: int) -> bool:
    stem = output_stem(json_path)
    marker = f"{stem}.complete"

    # A day is only finished when the marker says so. Parts are written as they
    # are produced, so a worker killed mid-file leaves some of them behind —
    # treating those as "already converted" would silently truncate the day to
    # however far it got, which is worse than not converting it at all.
    if os.path.exists(f"{stem}{match_storage.SUFFIX}") or os.path.exists(marker):
        print(f"  skip {os.path.basename(json_path)}: already converted", flush=True)
        return False

    stale = parts_for(stem)
    if stale:
        print(
            f"  {os.path.basename(json_path)}: discarding {len(stale)} part(s) "
            "from an interrupted run",
            flush=True,
        )
        for path in stale:
            os.remove(path)

    start = time.time()
    before = os.path.getsize(json_path)
    written: List[str] = []
    batch: List[dict] = []
    rows = 0

    def flush() -> None:
        if not batch:
            return
        # Parts are numbered so a day's files sort in the order they were
        # produced; the suffix is only cosmetic, the reader treats each as an
        # independent file.
        part = f"{stem}.part{len(written):03d}{match_storage.SUFFIX}"
        partial = f"{part}.partial"
        frame = match_storage.frame_for_storage(pd.DataFrame.from_records(batch))
        frame.to_parquet(partial, compression="zstd", index=False)
        os.replace(partial, part)
        written.append(part)
        batch.clear()

    for row in stream_rows(json_path):
        batch.append(row)
        rows += 1
        if len(batch) >= batch_rows:
            flush()
    flush()

    if not written:
        print(f"  skip {os.path.basename(json_path)}: no rows", flush=True)
        return False

    # A single batch needs no part suffix; keep the tidy one-file-per-day name.
    if len(written) == 1:
        single = f"{stem}{match_storage.SUFFIX}"
        os.replace(written[0], single)
        written = [single]
    else:
        # Only now is the day complete. Written after every part, so an
        # interrupted run leaves no marker and the day is redone rather than
        # being mistaken for finished.
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(f"{len(written)}\n")

    after = sum(os.path.getsize(path) for path in written)
    if delete:
        os.remove(json_path)

    print(
        f"  {os.path.basename(json_path)}: {before/1e9:,.2f} GB -> "
        f"{after/1e6:,.1f} MB ({before/after:.0f}x, {rows:,} rows, "
        f"{len(written)} part(s), {time.time() - start:.0f}s)"
        f"{' [json removed]' if delete else ''}",
        flush=True,
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directories", nargs="*")
    parser.add_argument(
        "--delete",
        action="store_true",
        help="remove each JSON file once its Parquet parts are written",
    )
    parser.add_argument("--batch-rows", type=int, default=DEFAULT_BATCH_ROWS)
    # Sharding, for running this as an Indexed Job.
    parser.add_argument(
        "--shards", type=int, default=int(os.environ.get("SMITELE_CONVERT_SHARDS", 1))
    )
    parser.add_argument(
        "--shard", type=int, default=int(os.environ.get("JOB_COMPLETION_INDEX", 0))
    )
    args = parser.parse_args()

    directories = args.directories or [paths.MATCH_DATA_DIR, paths.MATCH_ARCHIVE_DIR]

    everything: List[str] = sorted(
        path
        for directory in directories
        if directory and os.path.isdir(directory)
        for root, _, files in os.walk(directory)
        for path in (os.path.join(root, name) for name in files)
        if path.endswith(".json")
    )
    pending = everything[args.shard :: args.shards] if args.shards > 1 else everything

    if not pending:
        print(f"Nothing to convert (shard {args.shard}/{args.shards}).")
        return 0

    total = sum(os.path.getsize(path) for path in pending)
    print(
        f"shard {args.shard}/{args.shards}: converting {len(pending)} of "
        f"{len(everything)} JSON file(s), {total/1e9:,.2f} GB",
        flush=True,
    )

    converted = sum(convert(path, args.delete, args.batch_rows) for path in pending)
    print(f"Converted {converted}/{len(pending)} (shard {args.shard})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
