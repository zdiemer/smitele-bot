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

import manifest  # noqa: E402  pylint: disable=wrong-import-position
from game import Game  # noqa: E402  pylint: disable=wrong-import-position
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


class GameConfig:
    """Everything about an aggregate run that depends on which game it is.

    The arithmetic is identical for both — group, count, weight by recency,
    rank — and only the shape of a build and the queue vocabulary differ. This
    keeps that difference in one object instead of threading four arguments
    through six functions.
    """

    def __init__(self, game: Game):
        self.game = game
        self.shape = (
            build_features.SMITE1 if game is Game.SMITE else build_features.SMITE2
        )
        # Smite 1 folds its under-30 queues into their parent modes because
        # /build does. Smite 2 has no such split.
        self.queue_aliases = QUEUE_ALIASES if game is Game.SMITE else {}
        self.corpus_dirs = (
            (paths.MATCH_DATA_DIR, paths.MATCH_ARCHIVE_DIR)
            if game is Game.SMITE
            else (
                paths.game_match_data_dir(game),
                paths.game_match_archive_dir(game),
            )
        )
        self.out_dir = paths.game_model_dir(game)

    @property
    def needed_columns(self) -> List[str]:
        return (
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
            + self.shape.item_columns
            + self.shape.relic_columns
            + ([self.shape.starter_column] if self.shape.starter_column else [])
            + ([self.shape.aspect_column] if self.shape.aspect_column else [])
        )

    async def provider(self):
        if self.game is Game.SMITE:
            made = SmiteProvider(silent=True)
        else:
            from smite2.provider import Smite2Provider  # noqa: PLC0415

            made = Smite2Provider(silent=True)
        await made.create()
        return made


def peak_resident_gib() -> float:
    """High-water mark of this process's resident memory.

    Reported at the end of every run because the job's memory request is the
    thing that decides whether it can be scheduled at all, and it has only ever
    been set from estimates. A 12Gi request that a 5Gi job did not need is not
    conservatism — on this cluster it is the difference between running nightly
    and sitting Pending until a node frees up. Measuring it costs nothing, so
    the next person to size this has a number instead of an argument.

    ru_maxrss is KiB on Linux, bytes on macOS; the run that matters is the one
    in the container, so the Linux reading is the one to trust.
    """
    import resource  # noqa: PLC0415  (Unix-only, and only needed here)

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2


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


def prepare(
    frame: pd.DataFrame,
    items: Dict[int, object],
    config: "GameConfig" = None,
) -> pd.DataFrame:
    frame = frame[frame["GodId"] != 0].copy()
    if not frame.shape[0]:
        return frame

    frame["Win_Status"] = frame["Win_Status"] == "Winner"
    frame["match_queue_id"] = (
        pd.to_numeric(frame["match_queue_id"], errors="coerce")
        .fillna(-1)
        .astype(np.int32)
        .replace(config.queue_aliases if config else QUEUE_ALIASES)
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

    build_features.annotate(
        frame, items, config.shape if config else build_features.SMITE1
    )
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


# Lanes whose meta moves faster than the corpus-wide half-life assumes.
#
# One half-life for the whole corpus was an assumption, not a measurement, and
# holding out days says it is wrong for exactly one lane. Support, at the 180-day
# default, is the only lane where the ranking scores *worse* than simply playing
# the most common build — −1.08% and −1.46% held-out win rate at two independent
# cutoffs. At a 45-day half-life it is positive at both (+2.74%, +0.62%).
#
# Nothing else moves here, deliberately. Mid gets monotonically worse as the
# half-life shortens (+2.08% → +0.43% at one cutoff, +2.81% → +1.18% at the
# other) and wants the deep sample it has; carry, jungle and solo show no
# consistent trend across the two cutoffs, and fitting a constant per lane on
# thirty cells apiece is how a sweep turns into an overfit. Solo in particular
# is left alone on purpose: the role vector says its builds are dominated far
# more often than any other lane's, and the held-out days do not corroborate
# that at all, so the axis is what is ambiguous rather than the picks.
#
# `src/tools/build_eval.py --by-lane --half-life-days N` is what produced these
# numbers and is how to re-check them after a few more months of corpus.
HALF_LIFE_BY_ROLE: Dict[str, int] = {"Support": 45}


def parse_role_half_lives(text: str) -> Dict[str, int]:
    """`"Support=45,Mid=180"` as a mapping, for a sweep or a one-off run.

    `"none"` means no overrides at all, which is how to reproduce the single
    corpus-wide half-life this used to have.
    """
    if not text or text.strip().lower() in ("none", "off"):
        return {}
    out: Dict[str, int] = {}
    for part in text.split(","):
        if not part.strip():
            continue
        role, _, value = part.partition("=")
        role = role.strip().title()
        if role not in ROLE_CATEGORIES:
            raise argparse.ArgumentTypeError(f"{role!r} is not a lane")
        try:
            out[role] = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a number of days"
            ) from error
    return out


def role_weights(day, newest, half_life_days: int, by_role: Dict[str, int] = None):
    """Per-lane recency weights for one day, or a plain float if they all agree.

    Returning a scalar when nothing is overridden keeps the common path exactly
    as it was — one multiplication over a column, no map, no allocation.
    """
    default = recency_weight(day, newest, half_life_days)
    by_role = by_role if by_role is not None else HALF_LIFE_BY_ROLE
    if not by_role:
        return default
    weights = {
        role: recency_weight(day, newest, half_life)
        for role, half_life in by_role.items()
    }
    # Carried in the mapping rather than passed alongside it, so `_weighted` and
    # `decay_totals` each need one argument instead of two.
    weights["__default__"] = default
    return weights


def _describe(weight) -> str:
    """A weight, or a per-lane set of them, in one short line for the log."""
    if not isinstance(weight, dict):
        return f"{weight:.4f}"
    default = weight.get("__default__", 1.0)
    lanes = ", ".join(
        f"{role} {value:.4f}"
        for role, value in sorted(weight.items())
        if role != "__default__"
    )
    return f"{default:.4f}" + (f" ({lanes})" if lanes else "")


def _weighted(counts: pd.DataFrame, weight):
    """`weight` as something multiplying a column, per lane where it varies."""
    if not isinstance(weight, dict):
        return weight
    default = weight.get("__default__", 1.0)
    return counts["Role"].astype(str).map(weight).fillna(default)


def reduce_file(frame: pd.DataFrame, weight=1.0, starter_column: str = None):
    """Per-build, per-relic and per-starter counts for one day.

    Weighted counts are carried alongside the raw ones: the ranking uses the
    weighted figures as an effective sample size, while the raw counts are what
    gets shown to a player ("played N times").

    `weight` is a float, or a lane-to-weight mapping when some lane's meta moves
    faster than the rest — see `HALF_LIFE_BY_ROLE`. Every table here is grouped
    on a key that includes Role, so a per-lane weight is a column lookup rather
    than a second pass.
    """
    builds = frame.loc[frame["IsFullBuild"]]
    build_counts = (
        builds.groupby(GROUP_KEYS + ["BuildHash"], dropna=False, observed=True)
        .agg(plays=("Win_Status", "size"), wins=("Win_Status", "sum"))
        .reset_index()
    )
    build_weight = _weighted(build_counts, weight)
    build_counts["wplays"] = build_counts["plays"] * build_weight
    build_counts["wwins"] = build_counts["wins"] * build_weight
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
    relic_weight = _weighted(relic_counts, weight)
    relic_counts["wplays"] = relic_counts["plays"] * relic_weight
    relic_counts["wwins"] = relic_counts["wins"] * relic_weight

    # `aspect_plays` rides along on the god totals rather than getting a table
    # of its own, because a Smite 2 god has exactly one Aspect and the only
    # question worth asking is what share of a lane took it.
    #
    # Not a grouping key, and that is a measurement rather than a shortcut.
    # `tools/aspect_value.py` splits every cell people play both ways and
    # recommends from each half: conditioning on the Aspect wins 6 of 17 decided
    # cells and averages +2.07% held-out lift against the pooled cell's +2.40%.
    # Halving the evidence costs more than knowing the Aspect adds.
    #
    # Reporting it is a different matter. Uptake is 14% across Conquest and
    # reaches 98% in lanes the Aspect is what makes viable at all — Geb carry,
    # Ganesha jungle — so a recommendation there is an Aspect build that has
    # never said so.
    if "Aspect" in frame.columns:
        frame = frame.assign(
            _aspect=pd.to_numeric(frame["Aspect"], errors="coerce").fillna(0) > 0
        )
        god_counts = (
            frame.groupby(GROUP_KEYS, dropna=False, observed=True)
            .agg(
                plays=("Win_Status", "size"),
                wins=("Win_Status", "sum"),
                aspect_plays=("_aspect", "sum"),
            )
            .reset_index()
        )
        god_counts["aspect_plays"] = god_counts["aspect_plays"].astype("int32")
    else:
        god_counts = (
            frame.groupby(GROUP_KEYS, dropna=False, observed=True)
            .agg(plays=("Win_Status", "size"), wins=("Win_Status", "sum"))
            .reset_index()
        )
    god_weight = _weighted(god_counts, weight)
    god_counts["wplays"] = god_counts["plays"] * god_weight
    god_counts["wwins"] = god_counts["wins"] * god_weight

    # Unlike relics there is no fullness gate: a starter is bought in the
    # opening minute, so every row that records one is evidence about it.
    starter_counts = pd.DataFrame()
    if starter_column and starter_column in frame.columns:
        starter_ids = (
            pd.to_numeric(frame[starter_column], errors="coerce")
            .fillna(0)
            .astype("int64")
        )
        chosen = frame.loc[
            (starter_ids > 0).to_numpy(), GROUP_KEYS + ["Win_Status"]
        ].copy()
        if chosen.shape[0]:
            chosen["StarterId"] = starter_ids[starter_ids > 0].astype("int32")
            starter_counts = (
                chosen.groupby(GROUP_KEYS + ["StarterId"], dropna=False, observed=True)
                .agg(plays=("Win_Status", "size"), wins=("Win_Status", "sum"))
                .reset_index()
            )
            starter_weight = _weighted(starter_counts, weight)
            starter_counts["wplays"] = starter_counts["plays"] * starter_weight
            starter_counts["wwins"] = starter_counts["wins"] * starter_weight

    return build_counts, items, relic_counts, god_counts, starter_counts


COUNT_COLUMNS: List[str] = ["plays", "wins", "wplays", "wwins"]

# Summed alongside the god totals where the game has Aspects, and absent where
# it does not — `consolidate` is told about them per call rather than folding
# them into COUNT_COLUMNS, so Smite 1's tables keep exactly the columns they had.
ASPECT_COLUMNS: List[str] = ["aspect_plays"]

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
    "aspect_plays": "int32",
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
    # Empties are dropped rather than concatenated: the running total starts out
    # as a bare frame with no keys on it, and one of those in the list is enough
    # to leave nothing to group by.
    frames = [frame for frame in frames if frame.shape[0]]
    if not frames:
        return pd.DataFrame(columns=keys + columns)
    return narrow(
        pd.concat(frames, ignore_index=True)
        .groupby(keys, dropna=False, observed=True)[columns]
        .sum()
        .reset_index()
    )


BUILD_PLAYS_NAME: str = "build_plays.parquet"
OUTPUT_NAMES = ("build_stats", "build_items", "relic_stats", "god_stats")

# Written like the others but tolerated when absent: the table arrived later
# than the rest, and refusing to load a stored aggregate without it would
# force Smite 1 — which has no starter column at all — into a 3.5 hour
# rebuild for a table that will come out empty.
OPTIONAL_OUTPUT_NAMES = ("starter_stats",)


def load_previous(directory: str):
    """The last run's output plus the manifest describing what is in it.

    Returns None unless everything needed is present: a partial set cannot be
    folded into safely, since a missing manifest would mean re-adding days that
    are already counted.
    """
    plays_path = os.path.join(directory, BUILD_PLAYS_NAME)
    outputs = {
        name: os.path.join(directory, f"{name}{match_storage.SUFFIX}")
        for name in OUTPUT_NAMES
    }
    if not os.path.isfile(plays_path):
        return None
    if not all(os.path.isfile(path) for path in outputs.values()):
        return None

    stored = manifest.read(directory)
    if stored is None:
        return None

    previous = {
        "built": stored.built,
        "manifest": stored,
        "newest": stored.newest,
        "build_plays": pd.read_parquet(plays_path),
    }
    for name, path in outputs.items():
        previous[name] = pd.read_parquet(path)
    for name in OPTIONAL_OUTPUT_NAMES:
        path = os.path.join(directory, f"{name}{match_storage.SUFFIX}")
        previous[name] = pd.read_parquet(path) if os.path.isfile(path) else None
    return previous


def decay_totals(frame: pd.DataFrame, factor) -> pd.DataFrame:
    """Age a stored total forward to a newer reference day.

    Weights are relative to the newest day in the corpus, so when a newer day
    arrives everything already counted is worth proportionally less. Because
    the decay is exponential, that is one multiplication over the whole table
    rather than a revisit of the days it came from — which is what makes
    folding in new days possible instead of rebuilding.

    `factor` is per lane wherever the half-life is, for the same reason: a lane
    on a shorter half-life ages faster, and applying one factor to all of them
    would quietly undo the per-lane weighting on every incremental run.
    """
    if factor == 1.0 or not frame.shape[0]:
        return frame
    scale = _weighted(frame, factor)
    for column in ("wplays", "wwins"):
        if column in frame.columns:
            frame[column] = (frame[column] * scale).astype("float32")
    return frame


def empty_build_plays() -> pd.DataFrame:
    """The pass-1 total, before anything has been counted into it.

    It carries its schema rather than being a bare frame, because it is folded
    in like any other partial and there is nothing to group on otherwise. An
    empty fold window is not a rare case: the oldest days in the corpus are
    Smite 2's alpha, a few dozen rows apiece, and a run of them can easily pass
    without yielding a single full build.
    """
    return pd.DataFrame(
        {"BuildHash": pd.Series(dtype="UInt64"), "plays": pd.Series(dtype="int32")}
    )


def count_build_plays(
    corpus: List[str],
    items: Dict[int, object],
    min_plays: int,
    every: int,
    previous: pd.DataFrame = None,
    config: "GameConfig" = None,
):
    """First pass: which builds are played often enough to be worth keeping.

    The full grouping is keyed on god, queue, role, MMR *and* build, which on
    this corpus projects to ~45M groups — far more memory than the job has, and
    min-plays cannot be applied early because it needs complete counts.

    Counting by build alone first sidesteps that. It covers the same builds but
    with two columns instead of fifteen, so it fits comfortably, and it yields
    the set of builds that could possibly survive the filter. The second pass
    carries only those, which is where the size comes off: builds seen once or
    twice are the overwhelming majority of the groups and none of the signal.
    """
    print(f"Pass 1/2: counting plays per build across {len(corpus)} file(s)", flush=True)
    shape = config.shape if config else build_features.SMITE1
    columns = ["GodId"] + shape.item_columns + shape.relic_columns
    parts: List[pd.DataFrame] = []
    total = previous if previous is not None else empty_build_plays()
    start = time.time()

    def fold(parts, total):
        frames = [frame for frame in parts + [total] if frame.shape[0]]
        if not frames:
            return total
        merged = (
            pd.concat(frames, ignore_index=True)
            .groupby("BuildHash", observed=True)["plays"]
            .sum()
            .reset_index()
        )
        merged["plays"] = merged["plays"].astype("int32")
        return merged

    for index, path in enumerate(corpus, start=1):
        frame = match_storage.read_frame_columns(path, columns)
        frame = frame[frame["GodId"] != 0].copy()
        if not frame.shape[0]:
            continue
        build_features.annotate(frame, items, shape)
        builds = frame.loc[frame["IsFullBuild"], ["BuildHash"]]
        if builds.shape[0]:
            counts = builds.groupby("BuildHash", observed=True).size()
            parts.append(counts.rename("plays").reset_index())
        del frame, builds

        if index % every == 0:
            total = fold(parts, total)
            parts = []
            print(
                f"  {index}/{len(corpus)} files, {total.shape[0]:,} distinct builds, "
                f"{time.time() - start:.0f}s",
                flush=True,
            )

    total = fold(parts, total)
    # A numpy array rather than a Python set. Pass 2 tests every row of every
    # file against this, and on a full Smite 1 corpus it holds ~2.4M hashes: as
    # boxed ints in a set that is ~150MB of the run's peak, against ~19MB here.
    # It also keeps the membership test on plain uint64 — see the call site.
    keep = (
        total.loc[total["plays"] >= min_plays, "BuildHash"]
        .to_numpy(dtype="uint64", na_value=0)
    )
    print(
        f"Pass 1 done: {total.shape[0]:,} distinct builds, {len(keep):,} with "
        f">= {min_plays} plays ({time.time() - start:.0f}s)",
        flush=True,
    )
    # The unfiltered counts are kept, not just the surviving set: a build below
    # the threshold today can cross it next week, and without its history that
    # crossing would be invisible.
    return keep, total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument(
        "--min-plays",
        type=int,
        default=3,
        help="drop builds seen fewer than this many times across the corpus",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="where to write the tables; defaults to the game's model dir",
    )
    parser.add_argument(
        "--game",
        default=Game.SMITE.value,
        choices=[g.value for g in Game],
        help="which game's corpus to aggregate",
    )
    # Reducing after every file would rewrite the running total constantly;
    # every N keeps the intermediate list small without that churn.
    parser.add_argument("--consolidate-every", type=int, default=10)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="ignore any stored aggregate and recompute from the whole corpus",
    )
    parser.add_argument(
        "--rebuild-after-days",
        type=int,
        default=7,
        help="force a full rebuild when the stored aggregate is older than this",
    )
    parser.add_argument(
        "--half-life-days",
        type=int,
        default=180,
        help="how fast older matches stop counting; 0 weights every day equally",
    )
    parser.add_argument(
        "--role-half-life",
        type=parse_role_half_lives,
        default=None,
        help=(
            "per-lane overrides, e.g. 'Support=45,Mid=180'; defaults to "
            "HALF_LIFE_BY_ROLE, and 'none' turns every override off"
        ),
    )
    args = parser.parse_args()

    import asyncio

    config = GameConfig(Game(args.game))
    provider = asyncio.run(config.provider())
    print(
        f"{config.game.display_name}: {len(provider.items):,} items, "
        f"{len(provider.gods)} gods",
        flush=True,
    )
    out_dir = args.out or config.out_dir

    corpus = match_storage.corpus_paths(*config.corpus_dirs)
    if args.days:
        corpus = corpus[-args.days :]
    if not corpus:
        print("No corpus files found.", file=sys.stderr)
        return 1

    dates = {path: corpus_date(path) for path in corpus}
    newest = max((d for d in dates.values() if d), default=None)
    print(
        f"{len(corpus)} corpus file(s); newest day {newest}, "
        f"half-life {args.half_life_days}d",
        flush=True,
    )

    # Fold new days into the stored total rather than re-reading the corpus.
    # A full pass is ~3.5 hours over 3,300 files; a day's worth of new files is
    # seconds. The stored total is aged forward first — weights are relative to
    # the newest day, and exponential decay means that is one multiplication.
    previous = None if args.rebuild else load_previous(out_dir)

    # A stored aggregate from before the starter table cannot be folded into:
    # the already-counted days' starters are not in it and cannot be added
    # without re-reading those days. Only bites where the game has a starter
    # column, and that corpus (Smite 2's) rebuilds in about a minute.
    if (
        previous is not None
        and config.shape.starter_column
        and previous.get("starter_stats") is None
    ):
        print(
            "Stored aggregate predates the starter table; rebuilding from the "
            "whole corpus to count starters.",
            flush=True,
        )
        previous = None

    # Incremental runs drift, in one direction and for one reason: a build only
    # starts accumulating per-matchup rows once it crosses min-plays, so a
    # build that crosses later is missing the history from before it did.
    # Measured on a 19-day base plus 2 days, that was 633 groups and 0.95% of
    # plays — small, one-directional, and cumulative, so it is corrected by
    # rebuilding on a schedule rather than by pretending it isn't there.
    if previous is not None and args.rebuild_after_days > 0:
        age = (datetime.date.today() - previous["built"]).days
        if age >= args.rebuild_after_days:
            print(
                f"Stored aggregate is {age} day(s) old; rebuilding from the "
                "whole corpus to clear incremental drift.",
                flush=True,
            )
            previous = None

    # A corpus file that was rewritten since it was counted cannot be folded in
    # again: its earlier rows are already inside the stored totals and there is
    # no way to subtract them. Detection therefore decides whether the fast path
    # is available at all, rather than widening what it covers.
    #
    # Smite 2 hits this every night, because its crawl merges into the day it is
    # collecting; Smite 1 does not, because it writes each day once. See
    # `manifest` for why identity is the basename rather than the path.
    plan = manifest.classify(corpus, previous["manifest"]) if previous else None
    if plan is not None and plan.must_rebuild:
        print(
            f"{len(plan.changed):,} counted file(s) changed and "
            f"{len(plan.missing):,} vanished; their contribution cannot be "
            "subtracted, so rebuilding from the whole corpus.",
            flush=True,
        )
        for entry in plan.changed[:5]:
            current = manifest.fingerprint(entry.path)
            print(
                f"    {entry.name}: counted at {entry.size:,}B"
                + (f"/{entry.rows:,} rows" if entry.rows >= 0 else "")
                + (f", now {current[0]:,}B" if current else ", now gone"),
                flush=True,
            )
        previous, plan = None, None

    pending = corpus
    decay = 1.0

    if previous is not None:
        pending = plan.pending
        decay = role_weights(
            previous["newest"], newest, args.half_life_days, args.role_half_life
        )
        print(
            f"Incremental: {len(plan.carried):,} file(s) already counted, "
            f"{len(pending):,} new; ageing stored totals by {_describe(decay)}",
            flush=True,
        )
        if not pending:
            print("Nothing new to aggregate.")
            return 0

    keep_builds, build_plays = count_build_plays(
        pending,
        provider.items,
        args.min_plays,
        args.consolidate_every,
        previous=previous["build_plays"] if previous is not None else None,
        config=config,
    )

    build_parts: List[pd.DataFrame] = []
    relic_parts: List[pd.DataFrame] = []
    god_parts: List[pd.DataFrame] = []
    item_parts: List[pd.DataFrame] = []
    starter_parts: List[pd.DataFrame] = []
    builds_total = relics_total = gods_total = starters_total = pd.DataFrame()
    # Aspect counts only exist where the game has Aspects, and `consolidate`
    # would raise on a column that is not there.
    aspect_columns = ASPECT_COLUMNS if config.shape.aspect_column else []
    rows_seen = 0
    start = time.time()

    for index, path in enumerate(pending, start=1):
        frame = prepare(
            match_storage.read_frame_columns(path, config.needed_columns),
            provider.items,
            config,
        )
        if not frame.shape[0]:
            continue

        rows_seen += frame.shape[0]
        # Builds that cannot survive min-plays are dropped before grouping;
        # carrying them is what made the full key too large to hold.
        #
        # Tested on the raw uint64 rather than through Series.isin, which for a
        # nullable column dispatches to pandas' masked implementation. A full
        # Smite 1 rebuild segfaulted inside that on 2026-08-10 — not reproduced
        # since, so this is not presented as the fix, but the plain numpy path
        # is the one that has never faulted and it is cheaper besides. NA rows
        # map to 0 and are already IsFullBuild False, since build_features nulls
        # the hash for exactly those rows, so clearing them again is a no-op.
        hashes = frame["BuildHash"].to_numpy(dtype="uint64", na_value=0)
        frame.loc[~np.isin(hashes, keep_builds), "IsFullBuild"] = False
        build_counts, items, relic_counts, god_counts, starter_counts = reduce_file(
            frame,
            role_weights(
                dates.get(path), newest, args.half_life_days, args.role_half_life
            ),
            starter_column=config.shape.starter_column,
        )
        build_parts.append(build_counts)
        relic_parts.append(relic_counts)
        god_parts.append(god_counts)
        item_parts.append(items)
        starter_parts.append(starter_counts)
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
            gods_total = consolidate(god_parts + [gods_total], GROUP_KEYS, aspect_columns)
            god_parts = []
            starters_total = consolidate(
                starter_parts + [starters_total], GROUP_KEYS + ["StarterId"]
            )
            starter_parts = []
            item_parts = [
                pd.concat(item_parts, ignore_index=True).drop_duplicates(
                    subset=["BuildHash"]
                )
            ]
            print(
                f"  {index}/{len(pending)} files, {rows_seen:,} rows, "
                f"{builds_total.shape[0]:,} build groups, "
                f"{time.time() - start:.0f}s",
                flush=True,
            )

    builds = consolidate(
        build_parts + [builds_total], GROUP_KEYS + ["BuildHash"], SUM_COLUMNS
    )
    relics = consolidate(relic_parts + [relics_total], GROUP_KEYS + ["Relics"])
    gods = consolidate(god_parts + [gods_total], GROUP_KEYS, aspect_columns)
    starters = consolidate(
        starter_parts + [starters_total], GROUP_KEYS + ["StarterId"]
    )
    items = pd.concat(item_parts, ignore_index=True).drop_duplicates(
        subset=["BuildHash"]
    )

    if previous is not None:
        # Age the stored totals to the new reference day, then add the new days
        # to them. Raw plays/wins are counts and carry over untouched; only the
        # weighted columns move.
        builds = consolidate(
            [decay_totals(previous["build_stats"], decay), builds],
            GROUP_KEYS + ["BuildHash"],
            SUM_COLUMNS,
        )
        relics = consolidate(
            [decay_totals(previous["relic_stats"], decay), relics],
            GROUP_KEYS + ["Relics"],
        )
        # A stored aggregate written before Aspects were counted has no such
        # column, and dropping it here would mean the new one never survived an
        # incremental run — the count would silently stay absent until the next
        # scheduled rebuild. Backfilling it as zero keeps the column and makes
        # the share it feeds understate rather than vanish, which then corrects
        # itself as new days are folded in.
        stored_gods = decay_totals(previous["god_stats"], decay)
        for column in aspect_columns:
            if column not in stored_gods.columns:
                stored_gods[column] = 0
        gods = consolidate([stored_gods, gods], GROUP_KEYS, aspect_columns)
        if previous.get("starter_stats") is not None:
            starters = consolidate(
                [decay_totals(previous["starter_stats"], decay), starters],
                GROUP_KEYS + ["StarterId"],
            )
        items = pd.concat(
            [previous["build_items"], items], ignore_index=True
        ).drop_duplicates(subset=["BuildHash"])

    # No min-plays filter here: pass 1 already excluded everything below the
    # threshold, before it could cost anything.
    items = items[items["BuildHash"].isin(set(builds["BuildHash"]))]

    os.makedirs(out_dir, exist_ok=True)

    written = []
    for name, frame in (
        ("build_stats", builds),
        ("build_items", items),
        ("relic_stats", relics),
        ("god_stats", gods),
        ("starter_stats", starters),
    ):
        destination = os.path.join(out_dir, f"{name}{match_storage.SUFFIX}")
        partial = f"{destination}.partial"
        frame.to_parquet(partial, compression="zstd", index=False)
        os.replace(partial, destination)
        written.append((destination, frame.shape[0], os.path.getsize(destination)))

    plays_path = os.path.join(out_dir, BUILD_PLAYS_NAME)
    build_plays.to_parquet(f"{plays_path}.partial", compression="zstd", index=False)
    os.replace(f"{plays_path}.partial", plays_path)

    # The manifest is what makes the next run incremental: it records exactly
    # which files are already counted, and the day the weights are relative to.
    # Written last, after everything it describes, so that it is the commit
    # point — `load_previous` checks that all six files exist, but it cannot
    # catch a fresh manifest sitting beside stale totals.
    #
    # Carried entries are kept even when `--days` hid them from this run: they
    # are still inside the totals, and forgetting them would let the same day be
    # folded in a second time the next time the window widened.
    # "built" is the date of the last *full* rebuild, carried forward across
    # incremental runs, so age measures accumulated drift rather than time
    # since the last touch.
    built = (previous["built"] if previous is not None else datetime.date.today())
    manifest.write(
        out_dir,
        manifest.Manifest(
            entries=(plan.carried if plan is not None else [])
            + [manifest.entry_for(path) for path in pending],
            newest=newest,
            built=built,
        ),
    )

    print(f"Aggregated {rows_seen:,} player rows in {time.time() - start:.0f}s")
    for destination, rows, size in written:
        print(f"  {os.path.basename(destination)}: {rows:,} rows, {size/1e6:,.1f} MB")
    print(f"Peak resident: {peak_resident_gib():.2f} GiB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
