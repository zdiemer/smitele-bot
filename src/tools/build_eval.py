"""Measure a build recommender against what actually won, afterwards.

`build_accuracy.py` asks whether a recommender agrees with what people played.
That is a proxy, and it says so in its own docstring. This asks the question the
proxy stands in for:

    hold out the future, recommend a build from the past alone, then go and look
    at what happened to that build in the days that were held out.

    python src/tools/build_eval.py --game smite --cutoff 2026-07-01

Every strategy answers the same cells — a cell is (god, lane, queue) — from the
same train-only aggregate, so the numbers are comparable to each other and not
just to themselves.

The metric is win-rate lift, stratified by skill band
-----------------------------------------------------

For a recommended build B in a cell, the *neighbourhood* is the held-out rows
whose six items overlap B by at least `--overlap` (5 of 6 by default; demanding
all six measures almost nothing, because an exact six-item repeat is rare).
Lift is how much more often the neighbourhood won than the cell did:

    lift(B) = sum over bands of  share_of_neighbourhood(band)
                                 * (winrate(neighbourhood in band)
                                    - winrate(cell in band))

The banding is load-bearing rather than decorative. Better players both win more
and build better, so an unstratified lift partly measures who ran the build.
Comparing each slice of the neighbourhood against the same skill band of the
same cell removes the largest part of that. It does not remove all of it —
within a tier, the player who copies a good build is probably also the player
who plays better — so treat lift as a strong ordering between strategies rather
than as a causal effect size.

The cell baseline includes the neighbourhood rather than excluding it. That is
deliberate: it makes `most_played` score near zero by construction, which is
exactly the property that makes it a usable yardstick. A strategy that cannot
beat "play the most common build" is not earning its complexity.

What else gets reported, and why each of them can sink a good-looking lift
-------------------------------------------------------------------------

coverage  the share of cells a strategy answered at all. /edge scores badly
          here: `MIN_CANDIDATE_SUPPORT` means a thinly-played god has an empty
          candidate pool and gets no answer.
support   the median neighbourhood size. A build nobody ever ran has a lift
          computed from a handful of rows, and a big number over four rows is
          noise wearing a hat.
overlap   `build_accuracy`'s measure, carried along as description. It is not a
          target here and a change that improves lift while lowering overlap is
          a change that worked.

For the model strategy there is also AUC, Brier score and a reliability table.
`/edge` renders its raw sigmoid as "wins **N%** of the time", and nothing has
ever checked whether that number means what it says.

One warning about the model. `--model-dir` points at a model trained on the
whole corpus, which includes the held-out days, so its row is leaking unless
you retrain on the train window and point at that. The output labels it.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime
import json
import os
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), part)
    for part in ("HirezAPI", "SmiteBot", "ml", "match_data_collector")
]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import build_accuracy  # noqa: E402
import build_aggregate  # noqa: E402
import match_storage  # noqa: E402
import paths  # noqa: E402
from build_ranker import BuildStats  # noqa: E402
from game import Game  # noqa: E402
from HirezAPI import PlayerRole  # noqa: E402

ITEM_COLUMNS: List[str] = [f"ItemId{slot}" for slot in range(1, 7)]

EVAL_COLUMNS: List[str] = [
    "GodId",
    "Role",
    "Win_Status",
    "match_queue_id",
    "Conquest_Tier",
] + ITEM_COLUMNS


# ----------------------------------------------------------------- the split


def corpus_files(game: Game) -> List[str]:
    config = build_aggregate.GameConfig(game)
    return match_storage.corpus_paths(*config.corpus_dirs)


def split_corpus(
    files: Sequence[str], cutoff: datetime.date, train_days: int, eval_days: int
) -> Tuple[List[str], List[str]]:
    """Train files strictly before the cutoff, eval files on or after it.

    Days rather than files, because a day is written as however many Parquet
    parts its size needed — `features.recent_days` records the same trap, where
    asking for sixty files got four days.
    """
    by_day: Dict[datetime.date, List[str]] = {}
    for path in files:
        day = build_aggregate.corpus_date(path)
        if day is not None:
            by_day.setdefault(day, []).append(path)

    train_side = sorted(day for day in by_day if day < cutoff)
    eval_side = sorted(day for day in by_day if day >= cutoff)
    if train_days:
        train_side = train_side[-train_days:]
    if eval_days:
        eval_side = eval_side[:eval_days]

    def flatten(days: Sequence[datetime.date]) -> List[str]:
        return [path for day in days for path in sorted(by_day[day])]

    return flatten(train_side), flatten(eval_side)


# ------------------------------------------------------- the past, aggregated


def train_aggregate(
    files: Sequence[str],
    items: Dict[int, object],
    config: "build_aggregate.GameConfig",
    min_plays: int,
    half_life_days: int,
    batch: int,
    verbose: bool,
    role_half_life: Optional[Dict[str, int]] = None,
) -> Optional[BuildStats]:
    """An aggregate over the train window only, in the shape the bot reads.

    Built with `build_aggregate`'s own `prepare` / `reduce_file` / `consolidate`
    rather than a reimplementation, so `current_ranker` here is running exactly
    the code path `/build` runs. A harness that aggregated differently would be
    measuring a bot that does not exist.
    """
    if not files:
        return None

    newest = max(
        (day for day in map(build_aggregate.corpus_date, files) if day is not None),
        default=None,
    )

    builds: List[pd.DataFrame] = []
    item_rows: List[pd.DataFrame] = []
    relics: List[pd.DataFrame] = []
    gods: List[pd.DataFrame] = []
    pending = 0

    build_keys = build_aggregate.GROUP_KEYS + ["BuildHash"]
    relic_keys = build_aggregate.GROUP_KEYS + ["Relics"]

    def fold() -> None:
        nonlocal builds, relics, gods, item_rows, pending
        builds = [
            build_aggregate.consolidate(
                builds, build_keys, build_aggregate.SUM_COLUMNS
            )
        ]
        relics = [build_aggregate.consolidate(relics, relic_keys)]
        gods = [build_aggregate.consolidate(gods, build_aggregate.GROUP_KEYS)]
        item_rows = [
            pd.concat(item_rows, ignore_index=True).drop_duplicates(
                subset=["BuildHash"]
            )
        ]
        pending = 0

    started = time.monotonic()
    for index, path in enumerate(files, start=1):
        frame = match_storage.read_frame_columns(path, config.needed_columns)
        frame = build_aggregate.prepare(frame, items, config)
        if not frame.shape[0]:
            continue

        weight = build_aggregate.role_weights(
            build_aggregate.corpus_date(path),
            newest,
            half_life_days,
            role_half_life,
        )
        # Sliced rather than unpacked. `reduce_file` grew a fifth return value
        # when Smite 2 starters were aggregated, and a four-way unpack here
        # meant this harness died on its first training file — for both games,
        # from any cutoff — until someone tried to run it. Starters are not a
        # thing this measures, so taking the first four is the whole fix, and
        # slicing means the next table added there does not break it again.
        build_counts, day_items, relic_counts, god_counts = build_aggregate.reduce_file(
            frame, weight
        )[:4]
        builds.append(build_counts)
        item_rows.append(day_items)
        relics.append(relic_counts)
        gods.append(god_counts)
        pending += 1

        if pending >= batch:
            fold()
        if verbose:
            print(
                f"  train {index}/{len(files)} {os.path.basename(path)} "
                f"({time.monotonic() - started:.0f}s)",
                file=sys.stderr,
                flush=True,
            )

    fold()
    build_frame, item_frame = builds[0], item_rows[0]
    if not build_frame.shape[0]:
        return None

    # The real pipeline applies min-plays in a separate pass because it cannot
    # hold the full key set in memory. A train window is small enough to do it
    # directly, and the result is the same set of surviving hashes.
    if min_plays > 1:
        totals = build_frame.groupby("BuildHash", observed=True)["plays"].sum()
        keep = totals.index[totals >= min_plays]
        build_frame = build_frame[build_frame["BuildHash"].isin(keep)]
        item_frame = item_frame[item_frame["BuildHash"].isin(keep)]

    if not build_frame.shape[0]:
        return None
    return BuildStats(build_frame, item_frame, relics[0], gods[0])


# ----------------------------------------------------- the future, held back


def load_eval(
    files: Sequence[str], queues: Sequence[int], verbose: bool
) -> pd.DataFrame:
    """Held-out player rows: one per completed six-item build in a lane queue."""
    queue_set = set(int(queue) for queue in queues)
    frames: List[pd.DataFrame] = []

    for index, path in enumerate(files, start=1):
        frame = match_storage.read_frame_columns(path, EVAL_COLUMNS)
        if not frame.shape[0]:
            continue
        frame = frame.copy()
        frame["match_queue_id"] = (
            pd.to_numeric(frame["match_queue_id"], errors="coerce")
            .fillna(-1)
            .astype(np.int32)
            .replace(build_aggregate.QUEUE_ALIASES)
        )
        frame = frame[frame["match_queue_id"].isin(queue_set)]
        if not frame.shape[0]:
            continue

        frame["GodId"] = (
            pd.to_numeric(frame["GodId"], errors="coerce").fillna(0).astype(np.int32)
        )
        frame = frame[frame["GodId"] != 0]
        roles = frame["Role"].astype(str).str.strip().str.title()
        frame["Role"] = roles.where(
            roles.isin(build_aggregate.ROLE_CATEGORIES), "Unknown"
        )
        frame["won"] = (frame["Win_Status"] == "Winner").astype(np.int8)
        # Tier 0 is "unranked or not reported", which is a band of its own
        # rather than a missing value — the players in it are a real and
        # differently-skilled population, not an absence.
        frame["band"] = (
            pd.to_numeric(frame["Conquest_Tier"], errors="coerce")
            .fillna(0)
            .astype(np.int16)
        )
        for column in ITEM_COLUMNS:
            frame[column] = (
                pd.to_numeric(frame[column], errors="coerce").fillna(0).astype(np.int64)
            )

        # Partial builds are dropped for the reason train.py drops them: how
        # many items a player finished is a consequence of winning, and it
        # swamps every other signal in the raw data.
        complete = (frame[ITEM_COLUMNS] > 0).all(axis=1)
        frame = frame[complete]
        if frame.shape[0]:
            frames.append(
                frame[["GodId", "Role", "match_queue_id", "won", "band"] + ITEM_COLUMNS]
            )
        if verbose:
            print(
                f"  eval {index}/{len(files)} {os.path.basename(path)}: "
                f"{frame.shape[0]:,} rows",
                file=sys.stderr,
                flush=True,
            )

    if not frames:
        return pd.DataFrame(
            columns=["GodId", "Role", "match_queue_id", "won", "band"] + ITEM_COLUMNS
        )
    return pd.concat(frames, ignore_index=True)


class Cell:
    """One (god, lane, queue) worth of held-out rows, ready to be scored."""

    def __init__(self, key: Tuple[int, str, int], frame: pd.DataFrame):
        self.god_id, self.role, self.queue_id = key
        self.items = frame[ITEM_COLUMNS].to_numpy(np.int64)
        self.won = frame["won"].to_numpy(np.float64)
        self.band = frame["band"].to_numpy(np.int16)
        self.plays = len(self.won)
        # Baselines are per band and reused across every strategy and every
        # oracle candidate, so they are worth computing once.
        self.baseline = {
            int(band): float(self.won[self.band == band].mean())
            for band in np.unique(self.band)
        }

    def lift(self, build: Sequence[int], overlap: int) -> Optional[Tuple[float, int]]:
        """Skill-stratified win-rate lift for this build, and its support."""
        wanted = np.asarray(sorted(set(int(i) for i in build)), np.int64)
        if not len(wanted):
            return None
        near = np.isin(self.items, wanted).sum(axis=1) >= overlap
        support = int(near.sum())
        if not support:
            return None

        total = 0.0
        bands = self.band[near]
        won = self.won[near]
        for band in np.unique(bands):
            in_band = bands == band
            share = float(in_band.sum()) / support
            total += share * (float(won[in_band].mean()) - self.baseline[int(band)])
        return total, support


# --------------------------------------------------------------- strategies


class Context:
    """Everything a strategy might need, resolved once."""

    def __init__(
        self,
        game: Game,
        stats: BuildStats,
        gods,
        items,
        recommender,
        overlap: int,
        min_support: int,
    ):
        self.game = game
        self.stats = stats
        self.gods = gods
        self.items = items
        self.recommender = recommender
        self.overlap = overlap
        self.min_support = min_support
        self.starter_ids = tuple(
            item.id for item in items.values() if getattr(item, "is_starter", False)
        )
        self.pools: Dict[Tuple[int, str, int], List[Tuple[List[int], float]]] = {}
        self.__optimized: Dict[Tuple[int, str], Optional[List[int]]] = {}
        self.__recommended: Dict[Tuple[int, str], Optional[List[int]]] = {}

    def resolve(self, item_ids) -> Optional[List[object]]:
        """Recorded ids as catalogue items, the way `god_builder` does it."""
        found = [self.items[i] for i in item_ids if i in self.items]
        return found if len(found) == len(item_ids) else None

    def carries_anti_heal(self, items) -> bool:
        """Anti-heal, in whichever game's terms — the seam `god_builder` has."""
        if self.game is Game.SMITE_2:
            import smite2_stats  # noqa: PLC0415

            return any(smite2_stats.carries_anti_heal(item) for item in items)

        from passive_parser import PassiveAttribute  # noqa: PLC0415

        return any(
            PassiveAttribute.ANTIHEAL in (item.passive_properties or [])
            for item in items
        )

    def god(self, god_id: int):
        found = self.gods.get(god_id)
        if found is not None:
            return found
        # Smite 1's catalogue is keyed by GodId enum, Smite 2's by hashed slug.
        return next(
            (g for g in self.gods.values() if int(getattr(g.id, "value", g.id)) == god_id),
            None,
        )

    @staticmethod
    def lane(role: str) -> Optional[PlayerRole]:
        try:
            return PlayerRole(str(role).strip().lower())
        except ValueError:
            return None

    def optimized(
        self, god_id: int, role: str, empirical: Optional[float] = None
    ) -> Optional[List[int]]:
        """The stat model's build, cached: it does not depend on the queue.

        `empirical` sweeps `smite2_optimizer.EMPIRICAL_WEIGHT`, which is the
        question this harness was built to settle. That constant sits at zero
        with a comment saying so in as many words: the item-value table made
        item overlap worse at every weight tried, but overlap measures
        agreement with what people *play* and the table measures what *wins*,
        and nothing existed that could tell which of those to prefer.
        """
        key = (god_id, role, empirical)
        if key not in self.__optimized:
            self.__optimized[key] = self.__optimize(god_id, role, empirical)
        return self.__optimized[key]

    def __optimize(
        self, god_id: int, role: str, empirical: Optional[float] = None
    ) -> Optional[List[int]]:
        god = self.god(god_id)
        if god is None:
            return None
        lane = self.lane(role)
        if self.game is Game.SMITE_2:
            import smite2_optimizer  # noqa: PLC0415

            if empirical is not None:
                # `score` reads the module global per item, so setting it here
                # is what a weight argument would be if the constant had been
                # written as one.
                smite2_optimizer.EMPIRICAL_WEIGHT = empirical
            build = smite2_optimizer.Smite2BuildOptimizer(
                god, self.items, role=lane
            ).optimize()
            return [item.id for item in build] or None

        from build_optimizer import BuildOptimizer  # noqa: PLC0415
        from god_builder import valid_items_for_god  # noqa: PLC0415

        if god.role is None:
            return None
        optimizer = BuildOptimizer(
            god, valid_items_for_god(god, self.items), self.items, role=lane
        )
        builds, _ = asyncio.run(optimizer.optimize())
        if not builds:
            return None
        return [item.id for item in optimizer.rank_builds(builds)[0]]

    def recommended(self, god_id: int, role: str) -> Optional[List[int]]:
        key = (god_id, role)
        if key not in self.__recommended:
            self.__recommended[key] = self.__recommend(god_id, role)
        return self.__recommended[key]

    def __recommend(self, god_id: int, role: str) -> Optional[List[int]]:
        if self.recommender is None:
            return None
        # Matched to how `/edge` calls it today: the direct opponent only, with
        # the enemy, ally and skill inputs left absent. Wiring those up is a
        # separate change and should be measured as one.
        found = self.recommender.recommend(god_id=god_id, role=role, top_n=1)
        if not found:
            return None
        return list(found[0][0]) or None


def most_played(cell: Cell, context: Context) -> Optional[List[int]]:
    """The modal build in the train window — the yardstick."""
    pool = candidate_pool(cell, context)
    if not pool:
        return None
    return max(pool, key=lambda entry: entry[1])[0]


def ranked_by(name, min_plays: float = 0.0):
    """A strategy that asks `best_build` for one of `build_ranker.RANKINGS`.

    `min_plays` defaults to nothing rather than to the shipped floor, so the
    named rankings here are measured in isolation from it — otherwise a change
    to the floor would silently move every row in the table.
    """

    def strategy(cell: Cell, context: Context) -> Optional[List[int]]:
        best = context.stats.best_build(
            god_id=cell.god_id,
            queue_id=cell.queue_id,
            role=cell.role,
            require_starter=True,
            starter_ids=context.starter_ids,
            ranking=name,
            min_plays=min_plays,
        )
        if not best or not best.get("items"):
            return None
        return list(best["items"])

    return strategy


# The shipped ranking before the holdout was built: argmax of a fixed-kappa
# lower bound, uncorrected for the thousands of candidates it chose among.
current_ranker = ranked_by("lower_bound")


def ranked_pool(cell: Cell, context: Context) -> List[Dict]:
    """The shipped ranking's candidates for a cell, in order."""
    return context.stats.ranked_builds(
        god_id=cell.god_id,
        queue_id=cell.queue_id,
        role=cell.role,
        require_starter=True,
        starter_ids=context.starter_ids,
    )


def anti_heal_within(tolerance: float):
    """`anti_heal_always`, but only when it costs less than `tolerance`.

    This is the sweep that sets `build_engine.ANTI_HEAL_TOLERANCE`. Read the
    result as an upper bound on the cost rather than as the cost: the harness
    has no lobby, so it applies the constraint to every matchup, while the bot
    applies it only against a healer — and only in that matchup does the 25%
    healing reduction it buys have anything to reduce.
    """

    def strategy(cell: Cell, context: Context) -> Optional[List[int]]:
        import build_engine  # noqa: PLC0415

        pool = ranked_pool(cell, context)
        if not pool:
            return None
        promoted = build_engine.promote_anti_heal(
            pool, context.resolve, context.carries_anti_heal, tolerance=tolerance
        )
        return list(promoted[0]["items"])

    return strategy


def anti_heal_always(cell: Cell, context: Context) -> Optional[List[int]]:
    """The best build carrying anti-heal, whatever it costs in ranking score.

    Not a policy anyone would ship — the harness has no lobby, so it cannot know
    whether a healer was on the other team — but it is the right way to price
    the constraint. `build_engine.promote_anti_heal` only fires against a
    healer, and the question it has to answer first is what happens on the days
    it fires: the median god gives up 3.1 points of shrunk win rate to reach its
    first anti-heal build, and that number is meaningless until something says
    whether those points were real.
    """
    import build_engine  # noqa: PLC0415

    pool = ranked_pool(cell, context)
    if not pool:
        return None
    promoted = build_engine.promote_anti_heal(
        pool, context.resolve, context.carries_anti_heal, tolerance=float("inf")
    )
    return list(promoted[0]["items"])


def current_optimizer(cell: Cell, context: Context) -> Optional[List[int]]:
    return context.optimized(cell.god_id, cell.role)


def current_ml(cell: Cell, context: Context) -> Optional[List[int]]:
    return context.recommended(cell.god_id, cell.role)


ORACLE_POOL: int = 200


def oracle(cell: Cell, context: Context) -> Optional[List[int]]:
    """The best the train window's builds could have done. A ceiling, not a bot.

    Chosen on the held-out data, so it is cheating by construction — that is the
    point. It says how much of the gap between `most_played` and perfection is
    reachable at all by picking among builds people actually ran.

    The support floor is what makes the number mean anything. Without it the
    oracle maximises over neighbourhoods of two or three rows and reports
    something near +40%, which is not a ceiling on build quality — it is the
    largest coin-flip streak in the cell. Requiring a neighbourhood big enough
    to have a win rate at all asks the intended question: of the builds we could
    actually have recommended and then observed, how good was the best one.
    """
    pool = candidate_pool(cell, context)
    if not pool:
        return None
    ranked = sorted(pool, key=lambda entry: -entry[1])[:ORACLE_POOL]

    best, best_lift = None, None
    for build, _ in ranked:
        measured = cell.lift(build, context.overlap)
        if measured is None or measured[1] < context.min_support:
            continue
        if best_lift is None or measured[0] > best_lift:
            best, best_lift = build, measured[0]
    return best


def candidate_pool(cell: Cell, context: Context) -> List[Tuple[List[int], float]]:
    """Every train-window build for this cell, with its play count."""
    key = (cell.god_id, cell.role, cell.queue_id)
    cached = context.pools.get(key)
    if cached is not None:
        return cached

    frame = context.stats.builds
    selected = frame[
        (frame["GodId"] == cell.god_id)
        & (frame["match_queue_id"] == cell.queue_id)
        & (frame["Role"].astype(str).str.lower() == cell.role.lower())
    ]
    pool: List[Tuple[List[int], float]] = []
    if selected.shape[0]:
        grouped = selected.groupby("BuildHash", observed=True)["plays"].sum()
        # One reindex rather than a `.loc` per hash. The per-hash version is
        # fine on a fortnight and quietly quadratic on a real aggregate: a
        # single-row lookup into a million-row index, thousands of times per
        # cell, over a hundred and seventy-five cells. It did not fail, it just
        # never finished, which is the worse way for a harness to be wrong.
        rows = context.stats.items.reindex(grouped.index)
        matrix = rows[ITEM_COLUMNS].to_numpy()
        complete = ~pd.isna(matrix).any(axis=1)
        plays = grouped.to_numpy(dtype=float)
        for index in np.nonzero(complete)[0]:
            pool.append(
                ([int(value) for value in matrix[index]], float(plays[index]))
            )
    context.pools[key] = pool
    return pool


STRATEGIES = {
    "most_played": most_played,
    "current_ranker": current_ranker,
    "corrected_ranker": ranked_by("corrected_bound"),
    "shrunk_ranker": ranked_by("shrunk"),
    "additive_ranker": ranked_by("additive"),
    "anti_heal_always": anti_heal_always,
    "current_optimizer": current_optimizer,
    "current_ml": current_ml,
    "oracle": oracle,
}


def resolve(name: str):
    """A strategy by name, including the parameterised `shrunk:<strength>` form.

    The sweep has to run against one aggregate and one set of cells — rebuilding
    those per point would cost more than the sweep — so a strength is spelled
    into the strategy name rather than passed as a flag.
    """
    if name in STRATEGIES:
        return STRATEGIES[name]
    if name.startswith("shrunk:"):
        # shrunk:<prior_strength>[:<pessimism>]
        import build_ranker  # noqa: PLC0415

        parts = name.split(":")[1:]
        strength = float(parts[0])
        pessimism = float(parts[1]) if len(parts) > 1 else build_ranker.PESSIMISM
        return ranked_by(
            lambda plays, wins: build_ranker.shrunk_rate(
                plays, wins, strength, pessimism
            )
        )
    if name.startswith("anti_heal:"):
        # anti_heal:<tolerance in shrunk win rate>
        return anti_heal_within(float(name.split(":", 1)[1]))
    if name.startswith("optimizer:"):
        # optimizer:<empirical_weight>
        weight = float(name.split(":", 1)[1])

        def strategy(cell: Cell, context: Context) -> Optional[List[int]]:
            return context.optimized(cell.god_id, cell.role, empirical=weight)

        return strategy
    if name.startswith("additive:"):
        # additive:<own_record_weight>[:<min_plays>]
        parts = name.split(":")[1:]
        own = float(parts[0])
        floor = float(parts[1]) if len(parts) > 1 else None
        return additive_with(own, floor)
    return None


def additive_with(own_weight: float, min_plays: Optional[float]):
    """The item-level ranking at a chosen blend, for sweeping.

    It needs the item table, which a scoring callable over two count columns
    never sees, so its knobs go through `ranking_options` instead.
    """
    import build_ranker  # noqa: PLC0415

    options = {"own_weight": own_weight}
    floor = (
        min_plays if min_plays is not None else build_ranker.MIN_CANDIDATE_PLAYS
    )

    def strategy(cell: Cell, context: Context) -> Optional[List[int]]:
        best = context.stats.best_build(
            god_id=cell.god_id,
            queue_id=cell.queue_id,
            role=cell.role,
            require_starter=True,
            starter_ids=context.starter_ids,
            ranking="additive",
            ranking_options=options,
            min_plays=floor,
        )
        if not best or not best.get("items"):
            return None
        return list(best["items"])

    return strategy


# ------------------------------------------------------------- model metrics


def reliability(scores: np.ndarray, labels: np.ndarray, bins: int = 10):
    """Predicted probability against realised win rate, in equal-width bins.

    `/edge` prints its score as a win probability. Either these two columns
    track each other or that sentence is false, and until now nothing checked.
    """
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows = []
    for low, high in zip(edges[:-1], edges[1:]):
        inside = (scores >= low) & (scores < high if high < 1.0 else scores <= high)
        if not inside.any():
            continue
        rows.append(
            {
                "bin": f"{low:.1f}-{high:.1f}",
                "n": int(inside.sum()),
                "predicted": float(scores[inside].mean()),
                "actual": float(labels[inside].mean()),
            }
        )
    return rows


def model_metrics(files: Sequence[str], queues: Sequence[int], recommender):
    """AUC, Brier and calibration for the win-probability model on held-out days.

    The files are read one by one rather than through `features.load_corpus`,
    which walks whole directories: reading a directory is not the same as
    reading the split, and a calibration measured partly on training days would
    be the flattering answer rather than the true one.
    """
    import features  # noqa: PLC0415
    import train as train_module  # noqa: PLC0415

    columns = list(
        dict.fromkeys(
            ["Match", "TaskForce", "Winning_TaskForce", "GodId", "Role",
             "match_queue_id"]
            + features.ITEM_SLOTS
            + features.RELIC_SLOTS
            + features.SKILL_FEATURES
        )
    )
    queue_set = set(int(queue) for queue in queues)

    frames = []
    for path in files:
        frame = match_storage.read_frame_columns(path, columns)
        if not frame.shape[0]:
            continue
        frame = frame[
            pd.to_numeric(frame["match_queue_id"], errors="coerce").isin(queue_set)
        ]
        if frame.shape[0]:
            frames.append(frame)
    if not frames:
        return None

    frame = features.build_matchup_frame(pd.concat(frames, ignore_index=True))
    if not frame.shape[0]:
        return None
    frame = train_module.full_builds_only(frame)
    if not frame.shape[0]:
        return None

    meta = recommender.scorer.meta
    encoded, labels, _ = features.encode(
        frame,
        recommender.gods,
        recommender.items,
        recommender.roles,
        skill_stats=meta.get("skill_stats"),
    )
    scores = recommender.scorer(encoded)
    return {
        "rows": int(len(labels)),
        "auc": train_module.roc_auc(scores, labels),
        "brier": float(np.mean((scores - labels) ** 2)),
        "reliability": reliability(scores, labels),
    }


# --------------------------------------------------------------------- main


def evaluate(cells: List[Cell], context: Context, names: Sequence[str], verbose: bool):
    """Run every strategy over every cell and collect the per-cell results."""
    results: Dict[str, List[Dict]] = {name: [] for name in names}
    strategies = {name: resolve(name) for name in names}
    started = time.monotonic()

    for index, cell in enumerate(cells, start=1):
        for name in names:
            build = strategies[name](cell, context)
            if not build:
                results[name].append(
                    {"answered": False, "weight": cell.plays, "role": cell.role}
                )
                continue
            measured = cell.lift(build, context.overlap)
            results[name].append(
                {
                    "answered": True,
                    "weight": cell.plays,
                    # Carried so the summary can be cut by lane. The lanes are
                    # not one population: a solo build is scored on effective
                    # health times damage and a mid build on rotation burst, and
                    # an average over the five hides a lane that has stopped
                    # working.
                    "role": cell.role,
                    "god_id": cell.god_id,
                    "lift": measured[0] if measured else None,
                    "support": measured[1] if measured else 0,
                    "overlap": overlap_with_meta(build, cell),
                }
            )
        if verbose and (index % 20 == 0 or index == len(cells)):
            print(
                f"  scored {index}/{len(cells)} cells "
                f"({time.monotonic() - started:.0f}s)",
                file=sys.stderr,
                flush=True,
            )
    return results


def overlap_with_meta(build: Sequence[int], cell: Cell) -> int:
    """How many of the six are in the cell's six most-bought items.

    `build_accuracy`'s number, recomputed here on the held-out days so it sits
    in the same table as the lift it is meant to be a proxy for.
    """
    counts = collections.Counter()
    for column in range(cell.items.shape[1]):
        counts.update(cell.items[:, column].tolist())
    meta = {item for item, _ in counts.most_common(6)}
    return len(set(int(i) for i in build) & meta)


def summarise(name: str, rows: List[Dict]) -> Dict:
    answered = [row for row in rows if row["answered"] and row.get("lift") is not None]
    # Weighted by neighbourhood support rather than by cell size. A build that
    # only four held-out players ran has a lift, but it is four coin flips, and
    # weighting by the cell would let it count as much as a build with a
    # thousand observations in the same cell.
    weight = sum(row["support"] for row in answered)
    return {
        "strategy": name,
        "lift": (
            sum(row["lift"] * row["support"] for row in answered) / weight
            if weight
            else float("nan")
        ),
        "coverage": len([r for r in rows if r["answered"]]) / max(len(rows), 1),
        "measured": len(answered),
        "support": (
            float(np.median([row["support"] for row in answered])) if answered else 0.0
        ),
        "overlap": (
            float(np.mean([row["overlap"] for row in answered])) if answered else 0.0
        ),
    }


def lane_breakdown(
    results: Dict[str, List[Dict]], names: Sequence[str], baseline: str
) -> Dict[str, List[Dict]]:
    """`summarise`, cut by lane, paired against the baseline within each lane.

    The headline number is an average over five lanes that are scored on
    different axes and built from different item pools, so a lane can go wrong
    without moving it. Support is the one this found: at the corpus-wide
    half-life it was the only lane scoring *below* the most-played build, at
    both cutoffs, and nothing in the overall figure showed it.

    Paired per lane rather than only overall, because the count of cells where
    the two strategies actually disagreed is what says whether a lane's figure
    is worth quoting at all — `ranker_lift.MIN_DECIDED` reads exactly that.
    """
    lanes = sorted(
        {
            str(row.get("role") or "Unknown")
            for rows in results.values()
            for row in rows
        }
    )
    out: Dict[str, List[Dict]] = {name: [] for name in names}
    for lane in lanes:

        def in_lane(rows: List[Dict], _lane=lane) -> List[Dict]:
            return [
                row
                for row in rows
                if str(row.get("role") or "Unknown") == _lane
            ]

        sliced = {name: in_lane(results[name]) for name in names}
        summary = []
        for name in names:
            row = summarise(name, sliced[name])
            row["role"] = lane
            row["cells"] = len(sliced[name])
            summary.append(row)
        add_paired_comparison(summary, sliced, baseline)
        for row in summary:
            out[row["strategy"]].append(row)
    return out


def load_catalogue(game: Game, args):
    """The gods and items for a game, from a cache if there is one.

    Falls back to building the game's own provider, which fetches. That is what
    lets this run in the nightly job at all: the aggregate's `/data` is an
    emptyDir, because the real one is a ReadWriteOnce claim the bot already
    holds — so there is no cached catalogue in the pod to read, and an
    unconditional file read failed with a FileNotFoundError that said nothing
    about why.

    Prefer the cache when a path is given. A holdout is measured against a
    catalogue, and re-fetching it mid-experiment is one more thing that can
    differ between two runs you meant to compare.
    """
    if game is Game.SMITE_2:
        if args.wiki_cache and os.path.isfile(args.wiki_cache):
            return asyncio.run(build_accuracy.smite2_catalogue(args.wiki_cache))
    else:
        data_dir = os.environ.get("SMITELE_DATA_DIR", ".")
        gods_json = args.gods_json or os.path.join(data_dir, "gods.json")
        items_json = args.items_json or os.path.join(data_dir, "items.json")
        if os.path.isfile(gods_json) and os.path.isfile(items_json):
            return build_accuracy.smite1_catalogue(gods_json, items_json)

    print(
        f"No cached {game.display_name} catalogue; fetching one.", flush=True
    )
    provider = asyncio.run(build_aggregate.GameConfig(game).provider())
    if not provider.gods or not provider.items:
        raise SystemExit(
            f"Could not load a {game.display_name} catalogue. For Smite 1 that "
            "is usually a missing Hi-Rez credential; for Smite 2, the wiki."
        )
    return provider.gods, provider.items


LIFT_FILE: str = "ranker_lift.json"


def emit_lift(
    path,
    game: Game,
    cutoff,
    summary: List[Dict],
    by_lane: Optional[Dict[str, List[Dict]]],
    strategy: str,
    baseline: str,
    cells: int,
) -> Optional[str]:
    """Write the one number `/build` is allowed to quote about itself.

    The embed can compute a build's edge over its lane for free, because that is
    the ranker's own arithmetic over tables the bot already holds. It cannot
    compute *held-out* lift at all: that needs a second aggregate built over a
    train window and then scored against days deliberately excluded from it, and
    the bot holds the aggregate rather than the days behind it. So it is
    produced here, nightly, and read back as a fact rather than recomputed.

    Deliberately tiny and deliberately self-describing. The bot refuses to quote
    a figure whose strategy is not the one it ships, or whose file is older than
    it is willing to vouch for, and both of those checks need the metadata more
    than they need the numbers.
    """
    row = next((r for r in summary if r["strategy"] == strategy), None)
    if row is None or row["lift"] != row["lift"]:  # NaN: nothing was measured
        print(
            f"No measured lift for {strategy}; not writing {LIFT_FILE}.",
            file=sys.stderr,
        )
        return None

    def shape(entry: Dict) -> Dict:
        return {
            "lift": float(entry["lift"]),
            "beats_baseline": (
                float(entry["beats_baseline"])
                # NaN when nothing was decided, absent when the caller never
                # asked for the pairing. Both mean "no answer" rather than zero.
                if entry.get("beats_baseline") == entry.get("beats_baseline")
                and entry.get("beats_baseline") is not None
                else None
            ),
            "decided": int(entry.get("decided", 0)),
            "support": float(entry.get("support", 0.0)),
        }

    payload = {
        "game": game.value,
        "strategy": strategy,
        "baseline": baseline,
        "cutoff": str(cutoff),
        "cells": cells,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "overall": shape(row),
        "by_lane": {
            entry["role"]: shape(entry)
            for entry in (by_lane or {}).get(strategy, [])
            if entry["lift"] == entry["lift"]
        },
    }

    if path is True:
        path = os.path.join(paths.game_model_dir(game), LIFT_FILE)
    # Written whole and moved into place. The bot reads this file on a timer and
    # a half-written one is a crash in a command rather than a bad number.
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(temporary, path)
    print(
        f"Wrote {path}: {strategy} {payload['overall']['lift']:+.2%} over "
        f"{baseline} across {cells} cells.",
        flush=True,
    )
    return path


BASELINE: str = "most_played"


def add_paired_comparison(summary, results: Dict[str, List[Dict]], baseline: str):
    """How often a strategy beat the baseline *in the same cell*.

    The headline lift is an average over cells weighted by how much held-out
    evidence each answer has, which means two strategies are not necessarily
    being compared on the same cells or with the same weights. A paired count
    is immune to that: same god, same lane, same queue, same held-out days,
    which of the two builds did better. A strategy that wins on aggregate but
    only in half the cells is riding a handful of them.
    """
    reference = results.get(baseline)
    if reference is None:
        return

    for row in summary:
        rows = results[row["strategy"]]
        wins = losses = ties = 0
        for mine, theirs in zip(rows, reference):
            if mine.get("lift") is None or theirs.get("lift") is None:
                continue
            if mine["lift"] == theirs["lift"]:
                # Overwhelmingly this is the two strategies returning the same
                # build. Counting a tie as a loss made every row read below 50%
                # and made two identical rankings look like one beating the
                # other, which is how this was caught.
                ties += 1
            elif mine["lift"] > theirs["lift"]:
                wins += 1
            else:
                losses += 1
        decided = wins + losses
        row["beats_baseline"] = wins / decided if decided else float("nan")
        row["same_as_baseline"] = ties / max(wins + losses + ties, 1)
        row["decided"] = decided


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=("smite", "smite2"), default="smite")
    parser.add_argument(
        "--cutoff",
        required=True,
        help="YYYY-MM-DD; days before it train, days on or after it are held out",
    )
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--eval-days", type=int, default=14)
    parser.add_argument("--min-plays", type=int, default=3)
    parser.add_argument("--half-life-days", type=int, default=180)
    parser.add_argument(
        "--role-half-life",
        type=build_aggregate.parse_role_half_lives,
        default=None,
        help=(
            "per-lane half-life overrides, e.g. 'Support=45'; defaults to the "
            "shipped HALF_LIFE_BY_ROLE, and 'none' turns them off"
        ),
    )
    parser.add_argument(
        "--min-eval-plays",
        type=int,
        default=200,
        help="skip cells with fewer held-out rows than this",
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=5,
        help="items a held-out build must share to count as running the build",
    )
    parser.add_argument(
        "--min-support",
        type=int,
        default=20,
        help="held-out rows the oracle needs before it will consider a build",
    )
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument(
        "--strategies",
        default=",".join(STRATEGIES),
        help="comma-separated; current_optimizer is minutes on Smite 1",
    )
    parser.add_argument(
        "--baseline",
        default=BASELINE,
        help="the strategy the paired 'beats' column compares against",
    )
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--wiki-cache", default=None)
    parser.add_argument("--gods-json", default=None)
    parser.add_argument("--items-json", default=None)
    parser.add_argument("--out", default=None, help="write the results as JSON here")
    parser.add_argument(
        "--emit-lift",
        nargs="?",
        const=True,
        default=None,
        help=(
            "write the small file /build reads to quote its own validated lift; "
            "defaults to ranker_lift.json in the game's model directory"
        ),
    )
    parser.add_argument(
        "--lift-strategy",
        default="shrunk_ranker",
        help="which strategy's lift to emit; must be the one the bot ships",
    )
    parser.add_argument(
        "--by-lane",
        action="store_true",
        help="also break every strategy down by lane, which the headline hides",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    verbose = not args.quiet
    game = Game.SMITE if args.game == "smite" else Game.SMITE_2
    cutoff = datetime.datetime.strptime(args.cutoff, "%Y-%m-%d").date()
    names = [name for name in args.strategies.split(",") if name.strip()]
    unknown = [name for name in names if resolve(name) is None]
    if unknown:
        print(f"Unknown strategies: {', '.join(unknown)}", file=sys.stderr)
        return 1

    train_files, eval_files = split_corpus(
        corpus_files(game), cutoff, args.train_days, args.eval_days
    )
    if not train_files or not eval_files:
        print(
            f"Need days on both sides of {cutoff}: "
            f"{len(train_files)} train, {len(eval_files)} eval.",
            file=sys.stderr,
        )
        return 1
    print(
        f"{len(train_files)} train files before {cutoff}, "
        f"{len(eval_files)} held out from it.",
        flush=True,
    )

    gods, items = load_catalogue(game, args)
    queues = (
        build_accuracy.SMITE2_CONQUEST
        if game is Game.SMITE_2
        else build_accuracy.smite1_conquest_queues()
    )
    print(f"{len(gods)} gods, {len(items):,} items.", flush=True)

    print("Aggregating the train window…", flush=True)
    stats = train_aggregate(
        train_files,
        items,
        build_aggregate.GameConfig(game),
        args.min_plays,
        args.half_life_days,
        args.batch,
        verbose,
        role_half_life=args.role_half_life,
    )
    if stats is None:
        print("No builds survived the train window.", file=sys.stderr)
        return 1

    print("Loading the held-out days…", flush=True)
    evaluation = load_eval(eval_files, queues, verbose)
    if not evaluation.shape[0]:
        print("No held-out rows in a lane queue.", file=sys.stderr)
        return 1

    grouped = evaluation.groupby(["GodId", "Role", "match_queue_id"], observed=True)
    cells = [
        Cell((int(key[0]), str(key[1]), int(key[2])), frame)
        for key, frame in grouped
        if frame.shape[0] >= args.min_eval_plays and str(key[1]) != "Unknown"
    ]
    if not cells:
        print(
            f"No cell reached {args.min_eval_plays} held-out rows.", file=sys.stderr
        )
        return 1
    print(
        f"{len(cells)} cells, {sum(cell.plays for cell in cells):,} held-out rows.",
        flush=True,
    )

    model_dir = args.model_dir or paths.game_model_dir(game)
    recommender = None
    if "current_ml" in names:
        from recommend import BuildRecommender  # noqa: PLC0415

        recommender = BuildRecommender.load(model_dir)
        if recommender is None:
            print(f"No model in {model_dir}; current_ml will not answer.", flush=True)
        elif not args.model_dir:
            print(
                "current_ml is reading the deployed model, which was trained on "
                "the whole corpus — its row leaks the held-out days.",
                flush=True,
            )

    context = Context(
        game, stats, gods, items, recommender, args.overlap, args.min_support
    )

    results = evaluate(cells, context, names, verbose)
    summary = [summarise(name, results[name]) for name in names]
    add_paired_comparison(summary, results, args.baseline)
    # A strategy that answered nothing has a NaN lift; it sorts last rather
    # than wherever NaN comparisons happen to put it.
    summary.sort(key=lambda row: (row["lift"] != row["lift"], -row["lift"]))

    print(f"\ngame: {game.display_name}   cutoff: {cutoff}")
    print(
        f"{'strategy':<20}{'lift':>9}{'beats':>8}{'same':>7}{'n':>6}"
        f"{'cover':>8}{'support':>9}{'overlap':>9}"
    )
    print("-" * 76)
    for row in summary:
        print(
            f"{row['strategy']:<20}"
            f"{row['lift']:>+8.2%}"
            f"{row.get('beats_baseline', float('nan')):>8.0%}"
            f"{row.get('same_as_baseline', float('nan')):>7.0%}"
            f"{row.get('decided', 0):>6}"
            f"{row['coverage']:>8.0%}"
            f"{row['support']:>9.0f}"
            f"{row['overlap']:>9.2f}"
        )
    print(
        f"\n'beats' is wins/(wins+losses) against {args.baseline} in the same cell, "
        f"ties excluded;\n'same' is how often the two picked the same build, "
        f"and 'n' how many cells actually decided it."
    )

    by_lane = None
    if args.by_lane:
        by_lane = lane_breakdown(results, names, args.baseline)
        print(
            f"\n{'strategy':<20}{'lane':<10}{'lift':>9}{'cells':>7}"
            f"{'decided':>9}{'support':>9}"
        )
        print("-" * 64)
        for name in names:
            for row in by_lane[name]:
                print(
                    f"{name:<20}{row['role']:<10}{row['lift']:>+8.2%}"
                    f"{row['cells']:>7}{row.get('decided', 0):>9}"
                    f"{row['support']:>9.0f}"
                )

    payload = {
        "game": game.value,
        "cutoff": str(cutoff),
        "train_files": len(train_files),
        "eval_files": len(eval_files),
        "cells": len(cells),
        "overlap": args.overlap,
        "min_eval_plays": args.min_eval_plays,
        "min_support": args.min_support,
        "strategies": summary,
    }
    if by_lane is not None:
        payload["by_lane"] = by_lane

    if "current_ml" in names and recommender is not None:
        print("\nModel calibration on the held-out days…", flush=True)
        metrics = model_metrics(eval_files, queues, recommender)
        if metrics:
            payload["model"] = metrics
            print(
                f"  rows {metrics['rows']:,}   AUC {metrics['auc']:.4f}   "
                f"Brier {metrics['brier']:.4f}"
            )
            print(f"  {'bin':<12}{'n':>10}{'predicted':>12}{'actual':>10}")
            for row in metrics["reliability"]:
                print(
                    f"  {row['bin']:<12}{row['n']:>10,}"
                    f"{row['predicted']:>12.3f}{row['actual']:>10.3f}"
                )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        print(f"\nWrote {args.out}", flush=True)

    if args.emit_lift is not None:
        emit_lift(
            args.emit_lift,
            game,
            cutoff,
            summary,
            by_lane,
            args.lift_strategy,
            args.baseline,
            len(cells),
        )

    # Same reason build_aggregate reports it: this now runs as a nightly job,
    # and a job's memory request is what decides whether it can be scheduled at
    # all. The first Smite 1 and Smite 2 runs were sized from estimates, and an
    # estimate is what this replaces.
    print(
        f"Peak resident: {build_aggregate.peak_resident_gib():.2f} GiB",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
