"""Roll the match corpus into per-build win counts.

The bot used to hold every player row in memory to answer /build. At 250 days
that is ~132M rows and ~36GB, which no pod here can hold, and it was always
wasteful: /build never looks at individual rows, only at how often a build was
played and how often it won.

This precomputes exactly that. One row per
(god, queue, role, high-mmr, build), with plays and wins — plus the items each
build hash stands for, and the same treatment for relics. The result is small
enough for the bot to load in seconds and stays flat as the corpus grows.

    python src/match_data_collector/build_aggregate.py [--min-plays N] [--days N]

Corpus files are processed one at a time and reduced immediately, so peak
memory is one day rather than the whole history.

Written to the root of the match-data share as build_stats.parquet,
build_items.parquet and relic_stats.parquet.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "HirezAPI"))

import build_features  # noqa: E402  pylint: disable=wrong-import-position
import match_storage  # noqa: E402  pylint: disable=wrong-import-position
import paths  # noqa: E402  pylint: disable=wrong-import-position
from HirezAPI import QueueId  # noqa: E402  pylint: disable=wrong-import-position
from SmiteProvider import (  # noqa: E402  pylint: disable=wrong-import-position
    SmiteProvider,
)

HIGH_MMR: int = 2000

_DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")

GROUP_KEYS: List[str] = ["GodId", "match_queue_id", "Role", "HighMmr"]

# Role is the expensive key by a wide margin. As Python strings across tens of
# millions of groups it costs more than every count column put together; as a
# category with a fixed set it is one int8 per row. The category list must be
# fixed rather than inferred, or concatenating two partials with different
# observed roles silently falls back to object.
ROLE_CATEGORIES: List[str] = [
    "Solo",
    "Jungle",
    "Mid",
    "Support",
    "Carry",
    "ADC",
    "Unknown",
]
ROLE_DTYPE = pd.CategoricalDtype(ROLE_CATEGORIES)

# Under-30 queues are the same mode with a different id; /build folds them
# together, so the aggregate has to as well or the two would never match.
QUEUE_ALIASES: Dict[int, int] = {
    QueueId.UNDER_30_ARENA.value: QueueId.ARENA.value,
    QueueId.UNDER_30_CONQUEST.value: QueueId.CONQUEST.value,
    QueueId.UNDER_30_JOUST.value: QueueId.JOUST.value,
}

# Per-build performance shown in /build's description. Summed over winning rows
# only, matching what the raw-frame version reported, and divided by the win
# count on read. Means rather than medians: a median cannot be combined across
# files without keeping every value, which is the thing this exists to avoid.
STAT_COLUMNS: List[str] = ["Kills_Player", "Deaths", "Assists", "Damage_Player"]

NEEDED_COLUMNS: List[str] = (
    [
        "GodId",
        "Role",
        "Win_Status",
        "match_queue_id",
        "Rank_Stat_Conquest",
        "Rank_Stat_Duel",
        "Rank_Stat_Joust",
        "Conquest_Tier",
        "Duel_Tier",
        "Joust_Tier",
    ]
    + STAT_COLUMNS
    + build_features.ITEM_COLUMNS
    + build_features.RELIC_COLUMNS
)

SUM_COLUMNS: List[str] = [f"sum_{name}" for name in STAT_COLUMNS] + [
    "sum_rating",
    "sum_tier",
    "rated_wins",
]


def queue_rating(frame: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    """The rating and tier appropriate to each row's queue."""
    queue = pd.to_numeric(frame["match_queue_id"], errors="coerce").fillna(-1)

    def pick(conquest: str, duel: str, joust: str) -> pd.Series:
        value = pd.to_numeric(frame[conquest], errors="coerce").fillna(0.0)
        value = value.where(
            queue != QueueId.RANKED_DUEL.value,
            pd.to_numeric(frame[duel], errors="coerce").fillna(0.0),
        )
        return value.where(
            queue != QueueId.RANKED_JOUST.value,
            pd.to_numeric(frame[joust], errors="coerce").fillna(0.0),
        )

    return (
        pick("Rank_Stat_Conquest", "Rank_Stat_Duel", "Rank_Stat_Joust"),
        pick("Conquest_Tier", "Duel_Tier", "Joust_Tier"),
    )


def prepare(frame: pd.DataFrame, items: Dict[int, object]) -> pd.DataFrame:
    frame = frame[frame["GodId"] != 0].copy()
    if not frame.shape[0]:
        return frame

    frame["Win_Status"] = frame["Win_Status"] == "Winner"
    frame["match_queue_id"] = (
        pd.to_numeric(frame["match_queue_id"], errors="coerce")
        .fillna(-1)
        .astype(np.int32)
        .replace(QUEUE_ALIASES)
    )
    frame["GodId"] = (
        pd.to_numeric(frame["GodId"], errors="coerce").fillna(0).astype(np.int32)
    )
    roles = frame["Role"].astype(str).str.strip().str.title()
    frame["Role"] = roles.where(roles.isin(ROLE_CATEGORIES), "Unknown").astype(
        ROLE_DTYPE
    )

    rating, tier = queue_rating(frame)
    frame["HighMmr"] = rating >= HIGH_MMR
    frame["_rating"] = rating
    frame["_tier"] = tier
    for column in STAT_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)

    build_features.annotate(frame, items)
    return frame


def stat_sums(frame: pd.DataFrame, keys: List[str]) -> pd.DataFrame:
    """Per-group sums of the winners' performance stats."""
    winners = frame.loc[frame["Win_Status"]].copy()
    for column in STAT_COLUMNS:
        winners[f"sum_{column}"] = winners[column]
    # Unranked rows report a rating of zero, which would drag the average down
    # rather than being absent; they are counted separately and excluded.
    rated = winners["_rating"] > 0
    winners["sum_rating"] = winners["_rating"].where(rated, 0.0)
    winners["sum_tier"] = winners["_tier"].where(rated, 0.0)
    winners["rated_wins"] = rated.astype(int)
    return winners.groupby(keys, dropna=False, observed=True)[SUM_COLUMNS].sum().reset_index()


def corpus_date(path: str) -> "datetime.date":
    """The day a corpus file covers, from its name."""
    found = _DATE_IN_NAME.search(os.path.basename(path))
    if not found:
        return None
    return datetime.datetime.strptime(found.group(1), "%Y-%m-%d").date()


def recency_weight(day, newest, half_life_days: int) -> float:
    """How much a day's matches count, halving every `half_life_days`.

    Items get reworked and removed, so a win from three years ago describes a
    game that no longer exists — but it still carries information, especially
    for gods that are rarely picked now, so it is discounted rather than
    dropped.

    Measured against the newest day in the corpus rather than against today, so
    the weights depend only on the data and a re-run produces the same numbers.
    """
    if day is None or newest is None or half_life_days <= 0:
        return 1.0
    return 0.5 ** (max((newest - day).days, 0) / half_life_days)


def reduce_file(frame: pd.DataFrame, weight: float = 1.0):
    """Per-build and per-relic counts for one day.

    Weighted counts are carried alongside the raw ones: the ranking uses the
    weighted figures as an effective sample size, while the raw counts are what
    gets shown to a player ("played N times").
    """
    builds = frame.loc[frame["IsFullBuild"]]
    build_counts = (
        builds.groupby(GROUP_KEYS + ["BuildHash"], dropna=False, observed=True)
        .agg(plays=("Win_Status", "size"), wins=("Win_Status", "sum"))
        .reset_index()
    )
    build_counts["wplays"] = build_counts["plays"] * weight
    build_counts["wwins"] = build_counts["wins"] * weight
    build_counts = build_counts.merge(
        stat_sums(builds, GROUP_KEYS + ["BuildHash"]),
        on=GROUP_KEYS + ["BuildHash"],
        how="left",
    )
    for column in SUM_COLUMNS:
        build_counts[column] = build_counts[column].fillna(0.0)

    # One representative item set per hash. The hash is order-independent, so
    # any row carrying it describes the same six items.
    items = builds.drop_duplicates(subset=["BuildHash"])[
        ["BuildHash"] + build_features.ITEM_COLUMNS
    ]

    relics = frame.loc[frame["IsFullRelics"]]
    relic_counts = (
        relics.groupby(GROUP_KEYS + ["Relics"], dropna=False, observed=True)
        .agg(plays=("Win_Status", "size"), wins=("Win_Status", "sum"))
        .reset_index()
    )
    relic_counts["wplays"] = relic_counts["plays"] * weight
    relic_counts["wwins"] = relic_counts["wins"] * weight

    god_counts = (
        frame.groupby(GROUP_KEYS, dropna=False, observed=True)
        .agg(plays=("Win_Status", "size"), wins=("Win_Status", "sum"))
        .reset_index()
    )
    god_counts["wplays"] = god_counts["plays"] * weight
    god_counts["wwins"] = god_counts["wins"] * weight

    return build_counts, items, relic_counts, god_counts


COUNT_COLUMNS: List[str] = ["plays", "wins", "wplays", "wwins"]

# The running total dominates memory — a full corpus reaches tens of millions of
# groups before the min-plays filter can be applied, and that filter needs the
# complete count so it cannot be applied early. Counts fit comfortably in 32
# bits (no single build is played four billion times) and the weighted and
# averaged columns do not need double precision for what they are used for, so
# narrowing them halves the resident size of the thing there is most of.
NARROW_DTYPES: Dict[str, str] = {
    "plays": "int32",
    "wins": "int32",
    "wplays": "float32",
    "wwins": "float32",
}


def narrow(frame: pd.DataFrame) -> pd.DataFrame:
    for column, dtype in NARROW_DTYPES.items():
        if column in frame.columns:
            frame[column] = frame[column].astype(dtype)
    for column in frame.columns:
        if column.startswith("sum_") or column == "rated_wins":
            frame[column] = frame[column].astype("float32")
    return frame


def consolidate(
    frames: List[pd.DataFrame], keys: List[str], extra: List[str] = ()
) -> pd.DataFrame:
    """Sum counts across per-file partials, collapsing to one row per key."""
    columns = COUNT_COLUMNS + list(extra)
    if not frames:
        return pd.DataFrame(columns=keys + columns)
    return narrow(
        pd.concat(frames, ignore_index=True)
        .groupby(keys, dropna=False, observed=True)[columns]
        .sum()
        .reset_index()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument(
        "--min-plays",
        type=int,
        default=3,
        help="drop builds seen fewer than this many times across the corpus",
    )
    parser.add_argument("--out", default=paths.MODEL_DIR)
    # Reducing after every file would rewrite the running total constantly;
    # every N keeps the intermediate list small without that churn.
    parser.add_argument("--consolidate-every", type=int, default=10)
    parser.add_argument(
        "--half-life-days",
        type=int,
        default=180,
        help="how fast older matches stop counting; 0 weights every day equally",
    )
    args = parser.parse_args()

    provider = SmiteProvider(silent=True)
    import asyncio

    asyncio.run(provider.create())
    print(f"{len(provider.items):,} items, {len(provider.gods)} gods", flush=True)

    corpus = match_storage.corpus_paths(paths.MATCH_DATA_DIR, paths.MATCH_ARCHIVE_DIR)
    if args.days:
        corpus = corpus[-args.days :]
    if not corpus:
        print("No corpus files found.", file=sys.stderr)
        return 1

    dates = {path: corpus_date(path) for path in corpus}
    newest = max((d for d in dates.values() if d), default=None)
    print(
        f"Aggregating {len(corpus)} corpus file(s); newest day {newest}, "
        f"half-life {args.half_life_days}d",
        flush=True,
    )

    build_parts: List[pd.DataFrame] = []
    relic_parts: List[pd.DataFrame] = []
    god_parts: List[pd.DataFrame] = []
    item_parts: List[pd.DataFrame] = []
    builds_total = relics_total = gods_total = pd.DataFrame()
    rows_seen = 0
    start = time.time()

    for index, path in enumerate(corpus, start=1):
        frame = prepare(
            match_storage.read_frame_columns(path, NEEDED_COLUMNS), provider.items
        )
        if not frame.shape[0]:
            continue

        rows_seen += frame.shape[0]
        build_counts, items, relic_counts, god_counts = reduce_file(
            frame, recency_weight(dates.get(path), newest, args.half_life_days)
        )
        build_parts.append(build_counts)
        relic_parts.append(relic_counts)
        god_parts.append(god_counts)
        item_parts.append(items)
        del frame

        if index % args.consolidate_every == 0:
            builds_total = consolidate(
                build_parts + [builds_total], GROUP_KEYS + ["BuildHash"], SUM_COLUMNS
            )
            build_parts = []
            relics_total = consolidate(
                relic_parts + [relics_total], GROUP_KEYS + ["Relics"]
            )
            relic_parts = []
            gods_total = consolidate(god_parts + [gods_total], GROUP_KEYS)
            god_parts = []
            item_parts = [
                pd.concat(item_parts, ignore_index=True).drop_duplicates(
                    subset=["BuildHash"]
                )
            ]
            print(
                f"  {index}/{len(corpus)} files, {rows_seen:,} rows, "
                f"{builds_total.shape[0]:,} build groups, "
                f"{time.time() - start:.0f}s",
                flush=True,
            )

    builds = consolidate(
        build_parts + [builds_total], GROUP_KEYS + ["BuildHash"], SUM_COLUMNS
    )
    relics = consolidate(relic_parts + [relics_total], GROUP_KEYS + ["Relics"])
    gods = consolidate(god_parts + [gods_total], GROUP_KEYS)
    items = pd.concat(item_parts, ignore_index=True).drop_duplicates(
        subset=["BuildHash"]
    )

    if args.min_plays > 1:
        before = builds.shape[0]
        # A build seen once carries no information — the ranking's confidence
        # interval already puts it near zero — and they are the bulk of the
        # rows, so dropping them is most of what keeps this small.
        builds = builds[builds["plays"] >= args.min_plays]
        print(
            f"Dropped {before - builds.shape[0]:,} build groups below "
            f"{args.min_plays} plays ({builds.shape[0]:,} kept)",
            flush=True,
        )
        items = items[items["BuildHash"].isin(set(builds["BuildHash"]))]

    os.makedirs(args.out, exist_ok=True)
    written = []
    for name, frame in (
        ("build_stats", builds),
        ("build_items", items),
        ("relic_stats", relics),
        ("god_stats", gods),
    ):
        destination = os.path.join(args.out, f"{name}{match_storage.SUFFIX}")
        partial = f"{destination}.partial"
        frame.to_parquet(partial, compression="zstd", index=False)
        os.replace(partial, destination)
        written.append((destination, frame.shape[0], os.path.getsize(destination)))

    print(f"Aggregated {rows_seen:,} player rows in {time.time() - start:.0f}s")
    for destination, rows, size in written:
        print(f"  {os.path.basename(destination)}: {rows:,} rows, {size/1e6:,.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
