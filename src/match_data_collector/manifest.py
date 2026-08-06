"""What the stored aggregate already counted, and whether it is still true.

The aggregate folds new corpus files into a stored total rather than re-reading
250 days every night. That is only sound while "already counted" is a question
the manifest can answer honestly, and for a long time it could not: it recorded
a list of paths and nothing else, so a file that was *rewritten* still matched
its old entry and was skipped.

Two things rewrite corpus files, and both were silently miscounted:

    A Smite 2 crawl merges into the day it is collecting (`store.merge_day`
    read-modify-writes one parquet per match date). A backfill walking twelve
    pages back therefore mutates dozens of days that already had files. One
    such run added 60,415 rows of which the aggregate folded 8,195.

    A Smite 1 day rotates into the archive at thirty days
    (`match_data_collector.__archive_historical_matches`). `corpus_paths` scans
    the live and archive directories together and returns the path it found the
    file at, so the rename changes the string, the old entry no longer matches,
    and the day is folded in a *second* time.

So an entry records a fingerprint — size and mtime — and an identity that
survives a move, which is the basename. The first catches mutation, the second
stops a rename looking like mutation. Both failures were bounded by
`--rebuild-after-days` forcing a full pass weekly, which is why neither was ever
visible as anything worse than drift.

A changed file cannot be folded in incrementally. Its earlier contribution is
already inside the stored totals with no way to subtract it, so the only correct
response is to rebuild from the whole corpus. Detection is therefore a decision
about *whether* the fast path is available, not a way to make it cover more.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

MANIFEST_NAME: str = "aggregate_manifest.parquet"

COLUMNS = ("path", "name", "size", "mtime", "rows", "newest", "built")

# What a fingerprint field holds when we do not know it: a manifest written
# before fingerprints existed, or a parquet footer we could not read. Unknown is
# not the same as unchanged, and `classify` treats it as changed so the first
# run after this ships rebuilds once and records real values.
UNKNOWN: int = -1


@dataclass(frozen=True)
class Entry:
    """One corpus file, as it was when its rows went into the totals."""

    path: str
    name: str
    size: int
    mtime: int
    rows: int

    @property
    def known(self) -> bool:
        return self.size >= 0 and self.mtime >= 0


@dataclass
class Manifest:
    """Every file inside the stored totals, and what those totals mean.

    `newest` is the day the recency weights are relative to; `built` is the date
    of the last full rebuild, carried across incremental runs so that
    `--rebuild-after-days` measures accumulated drift rather than time since the
    last touch.
    """

    entries: List[Entry]
    newest: Optional[datetime.date]
    built: datetime.date


@dataclass
class Classification:
    """What each corpus file is, relative to what was already counted."""

    pending: List[str]
    changed: List[Entry]
    missing: List[Entry]
    carried: List[Entry]

    @property
    def must_rebuild(self) -> bool:
        return bool(self.changed or self.missing)


def fingerprint(path: str) -> Optional[Tuple[int, int]]:
    """`(size, mtime)` for a file, or None if it is gone.

    mtime is whole seconds, deliberately. The corpus lives on an SMB share —
    `match_storage` carries a retry wrapper because of it — and CIFS reports
    timestamps at a different granularity than the ext4 the same file may have
    been written on. Comparing nanoseconds across a remount would report a
    change nobody made and rebuild every night for nothing. A rewrite inside the
    same second is not lost either: `merge_day` only ever concatenates, so the
    size moves too.
    """
    try:
        status = os.stat(path)
    except OSError:
        return None
    return int(status.st_size), int(status.st_mtime)


def row_count(path: str) -> int:
    """Rows in a parquet file, read from the footer, or UNKNOWN.

    Only ever called for files being read anyway. `os.stat` on three thousand
    files over SMB is cheap; opening three thousand footers is not, which is why
    this is recorded rather than used as the guard.
    """
    try:
        import pyarrow.parquet as pq  # noqa: PLC0415

        return int(pq.read_metadata(path).num_rows)
    except Exception:  # noqa: BLE001
        # Legacy JSON days have no footer, and an unreadable one is not worth
        # failing a run over — the field is diagnostic, not load-bearing.
        return UNKNOWN


def entry_for(path: str) -> Entry:
    """Describe a file as it is right now."""
    stamp = fingerprint(path)
    size, mtime = stamp if stamp is not None else (UNKNOWN, UNKNOWN)
    return Entry(
        path=path,
        name=os.path.basename(path),
        size=size,
        mtime=mtime,
        rows=row_count(path) if stamp is not None else UNKNOWN,
    )


def classify(corpus: Sequence[str], manifest: Manifest) -> Classification:
    """Sort the corpus against what the manifest says was counted.

    Matching is by path first and basename second. The fallback is what makes a
    Smite 1 archive rotation a move rather than a new file: `os.rename` within a
    share preserves size and mtime, so only the string changed. Without it every
    day would be counted twice on the night it rotated.

    Basenames are unique across the corpus in practice — the live and archive
    directories hold disjoint days, and every writer emits
    `match_details_<date>[.partNNN]` — but a duplicate would make the fallback
    match the wrong file, so it is checked rather than assumed.
    """
    by_path: Dict[str, Entry] = {entry.path: entry for entry in manifest.entries}
    by_name: Dict[str, Entry] = {}
    if len({entry.name for entry in manifest.entries}) == len(manifest.entries):
        by_name = {entry.name: entry for entry in manifest.entries}

    pending: List[str] = []
    changed: List[Entry] = []
    carried: List[Entry] = []
    matched: set = set()

    for path in corpus:
        entry = by_path.get(path) or by_name.get(os.path.basename(path))
        if entry is None:
            pending.append(path)
            continue
        matched.add(entry.path)
        if not entry.known or fingerprint(path) != (entry.size, entry.mtime):
            changed.append(entry)
        else:
            # Carry the path it is at now, so a rotated file stops being looked
            # up by its old location on every subsequent run.
            carried.append(
                Entry(path, entry.name, entry.size, entry.mtime, entry.rows)
            )

    # Anything counted that the corpus no longer offers. Usually a conversion —
    # `convert_corpus --delete` replaces a JSON day with parquet parts — where
    # folding the parts on top of the original's contribution would double it.
    missing = [
        entry
        for entry in manifest.entries
        if entry.path not in matched and fingerprint(entry.path) is None
    ]
    # A counted file that is absent from this corpus but still on disk is not
    # missing, only out of view: `--days` truncates the corpus, and forgetting
    # those entries would let them be re-added the next time the window widened.
    carried.extend(
        entry
        for entry in manifest.entries
        if entry.path not in matched and fingerprint(entry.path) is not None
    )

    return Classification(
        pending=pending, changed=changed, missing=missing, carried=carried
    )


def read(directory: str) -> Optional[Manifest]:
    """Load the manifest, or None if there is not a usable one."""
    path = os.path.join(directory, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        frame = pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    if "path" not in frame.columns:
        return None

    def scalar(column: str) -> str:
        if column not in frame.columns or not frame.shape[0]:
            return ""
        return str(frame[column].iloc[0] or "")

    def optional(column: str) -> pd.Series:
        # A manifest written before fingerprints existed has no such column.
        # Every entry then reads as UNKNOWN, which classify calls changed, so
        # the first run after this ships rebuilds once and records real values.
        if column in frame.columns:
            return frame[column].fillna(UNKNOWN).astype("int64")
        return pd.Series([UNKNOWN] * frame.shape[0], dtype="int64")

    sizes, mtimes, rows = optional("size"), optional("mtime"), optional("rows")
    names = (
        frame["name"]
        if "name" in frame.columns
        else frame["path"].map(os.path.basename)
    )
    entries = [
        Entry(
            path=str(path_value),
            name=str(name_value),
            size=int(size),
            mtime=int(mtime),
            rows=int(row),
        )
        for path_value, name_value, size, mtime, row in zip(
            frame["path"], names, sizes, mtimes, rows
        )
    ]
    return Manifest(
        entries=entries,
        newest=_as_date(scalar("newest")),
        built=_as_date(scalar("built")) or datetime.date.today(),
    )


def write(directory: str, manifest: Manifest) -> None:
    """Record the manifest, atomically.

    Beside-and-rename for the same reason `store.merge_day` does it: the next
    run reads this to decide what is already counted, and a half-written one
    would either be unreadable or, worse, readable and short.

    Written *after* the tables it describes, so that it is the commit point. A
    manifest landing beside stale totals is the one failure the all-or-nothing
    check in `load_previous` cannot catch.
    """
    newest = manifest.newest.strftime("%Y-%m-%d") if manifest.newest else ""
    built = manifest.built.strftime("%Y-%m-%d")
    entries = sorted(manifest.entries, key=lambda entry: entry.name)
    frame = pd.DataFrame(
        {
            "path": [entry.path for entry in entries],
            "name": [entry.name for entry in entries],
            "size": [entry.size for entry in entries],
            "mtime": [entry.mtime for entry in entries],
            "rows": [entry.rows for entry in entries],
            "newest": [newest] * len(entries),
            "built": [built] * len(entries),
        }
    )
    os.makedirs(directory, exist_ok=True)
    destination = os.path.join(directory, MANIFEST_NAME)
    partial = f"{destination}.partial"
    frame.to_parquet(partial, compression="zstd", index=False)
    os.replace(partial, destination)


def _as_date(value: str) -> Optional[datetime.date]:
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
