"""Choosing a build from the precomputed aggregate.

/build used to scan every player row. It now reads the tables written by
build_aggregate, which changes what is possible as much as it changes the cost:

Every candidate gets scored. The old code, facing an O(n^2) ranking, kept only
the top ~10% of builds *by frequency* whenever there were more than a thousand
— a popularity filter applied before the quality ranking, so a strong but
uncommon build was discarded before it was ever considered. Scoring is now a
vectorised pass over precomputed counts, so nothing is dropped for being rare.

Relics are ranked the same way as items. They were previously picked by raw
frequency alone, which answers "what do people bring" rather than "what wins".

How a candidate is ranked is the whole of what /build is, and the first answer
here was wrong in a way that took a holdout to see.

The original ranking was the lower bound of a 95% confidence interval on the win
rate. Read one pair at a time that behaves, and the docstring it replaced was
right about that much. But `best_build` does not read one pair at a time — it
takes the argmax over every distinct build a god has, which on a real corpus is
thousands. A 95% interval is wrong one time in twenty by construction, so
maximising over thousands of them returns whichever build got lucky rather than
whichever build is good.

Note what the defect is not. Twenty-five wins in thirty games is not a fluke; it
is significant against a 58% baseline, and an estimator that ranked it below a
58%-over-2000 build on a pairwise reading would be broken. The problem is only
ever the pool: among enough thin candidates, some reach that record on luck
alone, and the argmax finds exactly those.

`src/tools/build_eval.py` settled it by holding out days. Against the shipped
ranking, the replacement below wins the cells where the two disagree 69% and 75%
of the time on two 2023 cutoffs, and 61% of 74 decided cells on a 150-day 2026
window — the largest of the three samples and the one that matches what the bot
actually serves. So: this changes roughly one recommendation in two to six, and
is right about six or seven times out of ten when it does.

Worth knowing what the same run says about the alternative. Ranking builds by
their items rather than by their own record scores a higher lift still (+5.6%
against +5.0%), on the same 58% of decided cells — but the builds it picks have
a median of *three* held-out plays against this one's twelve. A lift measured
over three games is not a measurement, and a recommendation nobody has run is
not obviously a recommendation, so `ADDITIVE` stays available and unused.

What replaced it is the posterior mean of a Beta-Binomial, shrunk toward the win
rate of the god-and-lane it was drawn from. That is the Bayes-optimal choice
under squared loss, and it degrades in the right direction at both ends: a build
with little evidence is pulled to the cell average instead of being credited
with its luck, while a build with plenty keeps its own rate.

Three things this does *not* claim, because the holdout would not support them.
Sweeping `PRIOR_STRENGTH` moved nothing that survives the noise — 60, 200 and
400 came out at 61%, 61% and 62% of decided cells on the 2026 window — so the
value below is a reasonable middle rather than an optimum. Beating the
most-played build is established only on the recent corpus, where it loses to
both rankings; on the 2023 sample that comparison came out 52/48 and 36/64,
which is no result at all. And at this prior
strength the pool problem above is improved, not solved — `tests/
test_build_ranker.py` constructs a crowd this still gets wrong, and a much
stronger prior that gets it right but has never been measured at production
scale. The established claim is the ordering against what shipped, nothing more.

Counts are recency-weighted throughout, so a build whose evidence is mostly old
carries a smaller effective sample and gets shrunk harder toward the average.

Relics are ranked the same way as items. They were previously picked by raw
frequency alone, which answers "what do people bring" rather than "what wins".
"""

from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

GROUP_KEYS: List[str] = ["GodId", "match_queue_id", "Role", "HighMmr"]
ITEM_COLUMNS: List[str] = [f"ItemId{slot}" for slot in range(1, 7)]

# 95% confidence interval.
KAPPA: float = 2.24140273

# How many plays' worth of belief the prior carries. A build needs evidence on
# this scale before its own win rate outweighs its lane's. The sweep over this
# was flat inside the noise, so it is a defensible middle rather than a fitted
# value — see the module docstring before treating it as tuned.
PRIOR_STRENGTH: float = 60.0

# Standard deviations of posterior uncertainty subtracted before ranking. See
# `shrunk_rate` for what it is protecting against.
#
# The sweep could not separate 0, 0.5 and 1.0 — 69% to 72% of decided cells over
# two cutoffs, from sixteen to twenty-seven decisions each, which is not enough
# to rank them. So this is chosen on the property rather than the average: at
# zero the estimator has a pathology a pool can trigger, at two it starts
# behaving like the interval it replaced, and the middle keeps both ends honest.
PESSIMISM: float = 0.5

DEFAULT_RANKING: str = "shrunk"


def agresti_coull_lower(plays: np.ndarray, wins: np.ndarray) -> np.ndarray:
    """Lower bound of the 95% interval on win rate, vectorised.

    Retained because it is still the right tool for a comparison made once —
    `best_relics` picks among a handful of candidates, not thousands — and
    because `build_eval` needs to be able to ask for the old ranking by name to
    show that the new one beats it.
    """
    plays = np.asarray(plays, dtype=float)
    wins = np.asarray(wins, dtype=float)

    kest = wins + KAPPA**2 / 2
    nest = plays + KAPPA**2
    pest = kest / nest
    radius = KAPPA * np.sqrt(pest * (1 - pest) / nest)
    return np.maximum(0.0, pest - radius)


def corrected_lower(plays: np.ndarray, wins: np.ndarray) -> np.ndarray:
    """The same bound, widened for the number of candidates it chose among.

    A Bonferroni correction on the confidence level: choosing the best of n
    intervals at 95% is choosing among n chances to be wrong, so each one is
    taken at 1 - 0.05/n instead. `NormalDist` rather than scipy, which is not a
    dependency of this project and should not become one for a single quantile.
    """
    from statistics import NormalDist  # noqa: PLC0415

    plays = np.asarray(plays, dtype=float)
    wins = np.asarray(wins, dtype=float)
    count = max(len(plays), 1)
    kappa = NormalDist().inv_cdf(1.0 - 0.05 / (2.0 * count))

    nest = plays + kappa**2
    pest = (wins + kappa**2 / 2) / nest
    return np.maximum(0.0, pest - kappa * np.sqrt(pest * (1 - pest) / nest))


def shrunk_rate(
    plays: np.ndarray,
    wins: np.ndarray,
    strength: float = PRIOR_STRENGTH,
    pessimism: float = PESSIMISM,
) -> np.ndarray:
    """Posterior win rate under a Beta prior, discounted by its own uncertainty.

    The prior is the pooled win rate of every candidate in the same request —
    the god, in that lane, in that queue — which is close enough to 50% that the
    shrinkage is mostly a penalty on thin evidence, but not exactly 50%, and the
    difference matters for a god that is genuinely strong or genuinely weak.

    `pessimism` is not decoration. The posterior *mean* alone has a failure mode
    that only shows up in a pool: a build with almost no evidence converges to
    the prior, and the prior is set by whichever build dominates the pool — so a
    two-play build lands a hair above the thousand-play build that defined the
    number it was shrunk to, and wins. Subtracting a multiple of the posterior
    standard deviation restores the property the old interval had for free,
    that being uncertain is itself a reason to lose, while keeping the property
    the interval lacked, that thin evidence regresses to its population rather
    than being punished into last place.
    """
    plays = np.asarray(plays, dtype=float)
    wins = np.asarray(wins, dtype=float)

    total_plays = float(plays.sum())
    prior = float(wins.sum()) / total_plays if total_plays > 0 else 0.5

    posterior = plays + strength
    mean = (wins + strength * prior) / posterior
    if not pessimism:
        return mean
    deviation = np.sqrt(mean * (1.0 - mean) / (posterior + 1.0))
    return mean - pessimism * deviation


def pooled_rate(plays: np.ndarray, wins: np.ndarray) -> float:
    """The win rate of every candidate in a request, taken together.

    This is the prior `shrunk_rate` shrinks toward, and it is also the honest
    thing to compare a recommendation against: "how much better than the way
    this god's lane is usually built" is the question a player is asking, and
    the lane's own pooled rate is the answer's denominator.
    """
    total = float(np.asarray(plays, dtype=float).sum())
    if total <= 0:
        return 0.5
    return float(np.asarray(wins, dtype=float).sum()) / total


def shrunk_estimate(
    plays: np.ndarray, wins: np.ndarray, strength: float = PRIOR_STRENGTH
) -> np.ndarray:
    """The posterior mean alone — what to *show*, where `shrunk_rate` ranks.

    The two differ by the pessimism term, and the difference matters for a
    number a person reads. Subtracting a multiple of the posterior deviation is
    the right way to *choose* between candidates, because being uncertain should
    itself cost something; it is the wrong number to print, because it is not an
    estimate of anything — it is an estimate deliberately biased downward.

    This is what replaced "played 3 times and won 100.00% of them". That
    sentence was true and useless: a three-game record is not evidence, and
    quoting it to two decimal places implied it was. Ten Smite 2 gods were being
    shown a 100% win rate, and the median recommendation rested on 34 games out
    of a lane with thousands.
    """
    return shrunk_rate(plays, wins, strength, pessimism=0.0)


RANKINGS: Dict[str, object] = {
    "shrunk": shrunk_rate,
    "lower_bound": agresti_coull_lower,
    "corrected_bound": corrected_lower,
}

# Ranking by the items a build is made of, rather than by the build itself.
# Named separately because it needs the item table, which the plain estimators
# above never see; `best_build` dispatches on it before reaching `RANKINGS`.
ADDITIVE: str = "additive"

# How much of a build's score comes from its own record rather than from its
# items'. Zero is pure item-level evidence; one is the per-build rate, which is
# what every ranking above reduces to. Swept against held-out lift.
OWN_RECORD_WEIGHT: float = 0.25

# Plays a build needs in the aggregate before it can be recommended at all.
#
# Off by default, deliberately. The idea is sound — the item-level ranking below
# will otherwise surface builds a dozen people have ever assembled — but it could
# not be validated here: on a fortnight of corpus almost no build clears a
# meaningful floor, so every setting fell through to the "answer anyway" path and
# the sweep measured nothing. The production aggregate spans hundreds of days,
# where a floor would genuinely bind, so this wants sweeping there before being
# turned on. Shipping an unmeasured filter that silently changes every
# recommendation is the mistake this whole harness exists to stop making.
MIN_CANDIDATE_PLAYS: float = 0.0

# The same floor for relics, and on by default where the build one is not.
#
# The asymmetry is the point. A rarely-run *build* can be a genuine edge, so
# excluding it costs something real. A rarely-brought *relic pair* almost never
# is: the choice is close to settled per god and per lane, the candidate space
# is dozens rather than thousands, and the tail is made of people who misclicked
# or were experimenting. Fifty games is low enough that any pair a real player
# would consider clears it.
MIN_RELIC_PLAYS: float = 50.0

# Starters are ranked with the relics' floor and for the relics' reason: a
# small, settled candidate space where the rare choice is far more likely to
# be a misclick than an edge.
MIN_STARTER_PLAYS: float = 50.0

# The absolute floor is calibrated to Smite 1, where a cell's relic pool runs
# thousands of plays deep and fifty games genuinely separates settled choices
# from misclicks. Smite 2's corpus is a fraction of that size, and there the
# same fifty stopped filtering: Apollo's mid pool held Beads at 315 plays and
# Phantom Shell at 59, Shell cleared the floor and won the shrinkage on luck —
# the exact failure the floor exists to stop, one corpus smaller. So a
# candidate must also hold its own against the pool it is in: at least this
# share of the most-played option's games. Relative, it scales with corpus
# size instead of being tuned to one.
RELATIVE_SUPPORT_FLOOR: float = 0.2


class BuildStats:
    """The aggregate tables, and the queries /build makes against them."""

    FILES = ("build_stats", "build_items", "relic_stats", "god_stats")

    def __init__(
        self,
        builds: pd.DataFrame,
        items: pd.DataFrame,
        relics: pd.DataFrame,
        gods: pd.DataFrame,
        starters: Optional[pd.DataFrame] = None,
    ):
        self.builds = builds
        self.items = items.set_index("BuildHash")
        self.relics = relics
        self.gods = gods
        # Smite 2 only; Smite 1 keeps its starters inside the core slots, so
        # its table is absent or empty and queries answer None.
        self.starters = (
            starters if starters is not None and starters.shape[0] else None
        )

    @staticmethod
    def load(directory: str) -> Optional["BuildStats"]:
        """Load the aggregate, or None if it hasn't been built yet.

        Absence is a normal state — the bot can start before the first
        aggregate run — so this returns None rather than raising. The starter
        table is newer than the rest and optional for the same reason.
        """
        paths = {
            name: os.path.join(directory, f"{name}.parquet") for name in BuildStats.FILES
        }
        if not all(os.path.isfile(path) for path in paths.values()):
            return None
        starter_path = os.path.join(directory, "starter_stats.parquet")
        starters = pd.read_parquet(starter_path) if os.path.isfile(starter_path) else None
        return BuildStats(
            *(pd.read_parquet(paths[name]) for name in BuildStats.FILES),
            starters=starters,
        )

    def __filter(
        self,
        frame: pd.DataFrame,
        god_id: int,
        queue_id: Optional[int],
        role: Optional[str],
        high_mmr: bool,
    ) -> pd.DataFrame:
        """Rows matching the request.

        Unspecified dimensions are summed over rather than filtered, which is
        what "any queue" and "any role" mean: the aggregate is keyed on every
        dimension, so a broader request is a coarser grouping of the same rows.
        """
        selected = frame[frame["GodId"] == god_id]
        if queue_id is not None:
            selected = selected[selected["match_queue_id"] == queue_id]
        if role:
            selected = selected[
                selected["Role"].str.lower() == str(role).lower()
            ]
        if high_mmr:
            selected = selected[selected["HighMmr"]]
        return selected

    def best_build(
        self,
        god_id: int,
        queue_id: Optional[int] = None,
        role: Optional[str] = None,
        high_mmr: bool = False,
        require_starter: bool = False,
        starter_ids: Tuple[int, ...] = (),
        ranking=DEFAULT_RANKING,
        ranking_options: Optional[Dict] = None,
        min_plays: float = MIN_CANDIDATE_PLAYS,
    ) -> Optional[Dict]:
        """The highest-ranked build for these filters, or None if there is none.

        `ranking` names one of `RANKINGS`, or is a callable taking the weighted
        play and win columns. It exists so the evaluation harness can score the
        alternatives against each other on held-out days — including a sweep of
        the prior strength, which needs a callable rather than a name. The bot
        never passes it and gets the default.
        """
        selected = self.__filter(self.builds, god_id, queue_id, role, high_mmr)
        if not selected.shape[0]:
            return None

        # Sum across whichever dimensions the request left open.
        grouped = selected.groupby("BuildHash", observed=True).sum(numeric_only=True)
        if not grouped.shape[0]:
            return None

        if require_starter and len(starter_ids):
            with_starter = self.__hashes_with_starter(grouped.index, starter_ids)
            # Only apply it if some build qualifies; a god whose recorded builds
            # never include a starter should still get a recommendation.
            if any(with_starter):
                grouped = grouped.loc[with_starter]

        if min_plays > 1:
            supported = grouped[grouped["plays"] >= min_plays]
            # Same "only if something survives" rule as the starter filter: a
            # rarely-played god should still get an answer, just a shakier one.
            if supported.shape[0]:
                grouped = supported

        # The prior is pooled *after* the starter filter, so it describes the
        # population actually being chosen among rather than a wider one.
        if ranking == ADDITIVE:
            rank = self.additive_scores(grouped, **(ranking_options or {}))
        else:
            scorer = (
                ranking if callable(ranking) else RANKINGS.get(ranking, shrunk_rate)
            )
            rank = scorer(grouped["wplays"], grouped["wwins"])
        position = int(np.argmax(rank))
        best_hash = grouped.index[position]
        estimates = shrunk_estimate(grouped["wplays"], grouped["wwins"])
        return self.__described(
            grouped,
            grouped.loc[best_hash],
            best_hash,
            self.items_for(best_hash),
            float(np.max(rank)),
            estimate=float(estimates[position]),
            baseline=pooled_rate(grouped["wplays"], grouped["wwins"]),
        )

    def __described(
        self,
        grouped,
        row,
        build_hash,
        items,
        rank: float,
        estimate: Optional[float] = None,
        baseline: Optional[float] = None,
    ) -> Dict:
        """One candidate, with everything /build's description quotes.

        Shared by `best_build` and `ranked_builds` so a build reordered by a
        matchup carries the same figures as one that was not — the alternative
        was the second query returning a thinner dict, and the description
        quietly reporting the wrong build's win rate.
        """
        wins = float(row["wins"])
        rated = float(row.get("rated_wins", 0.0))
        return {
            "build_hash": build_hash,
            "items": items,
            "plays": int(row["plays"]),
            "wins": int(row["wins"]),
            "win_rate": float(row["wins"]) / max(float(row["plays"]), 1.0),
            "rank": rank,
            # What to tell a player. `estimate` is the build's win rate after
            # its evidence has been weighed against its lane's, `baseline` is
            # the lane's own rate, and the difference between them is the only
            # defensible "this build is better by X" available without a
            # holdout — which the bot cannot run, because it holds the
            # aggregate and not the days behind it.
            "estimate": estimate,
            "baseline": baseline,
            "edge": (
                estimate - baseline
                if estimate is not None and baseline is not None
                else None
            ),
            "unique_builds": int(grouped.shape[0]),
            # Stat sums are over winning rows, so the divisor is the win count.
            "avg_kills": float(row.get("sum_Kills_Player", 0.0)) / max(wins, 1.0),
            "avg_deaths": float(row.get("sum_Deaths", 0.0)) / max(wins, 1.0),
            "avg_assists": float(row.get("sum_Assists", 0.0)) / max(wins, 1.0),
            "avg_damage": float(row.get("sum_Damage_Player", 0.0)) / max(wins, 1.0),
            "avg_rating": (
                float(row.get("sum_rating", 0.0)) / rated if rated > 0 else 0.0
            ),
            "avg_tier": (
                float(row.get("sum_tier", 0.0)) / rated if rated > 0 else 0.0
            ),
        }

    def additive_scores(
        self,
        grouped: pd.DataFrame,
        strength: float = PRIOR_STRENGTH,
        own_weight: float = OWN_RECORD_WEIGHT,
    ) -> np.ndarray:
        """Score each candidate by the items in it, not by its own win rate.

        Ranking whole builds asks an estimate per six-item combination, and
        there are thousands of those per god with a handful of games each. No
        amount of shrinkage rescues that: sweeping the prior strength over a
        holdout moved held-out lift by less than the difference between two
        cutoffs, which is the signature of a measurement that is all noise.

        An item appears in hundreds or thousands of the same god's builds, so
        its win rate is estimated from real evidence. A build is then scored as
        the sum of what its items are worth, which is a much smaller model
        fitted to much more data. It also stays honest about what it is
        recommending: the candidates are still builds people actually ran, so
        legality, cost and the things the corpus knows implicitly all survive.

        `own_weight` blends a little of the build's own record back in, because
        an additive model cannot see that two items are redundant together —
        the combination's own record is the only evidence of that there is.
        """
        known = self.items.reindex(grouped.index)
        matrix = known[ITEM_COLUMNS].to_numpy()
        plays = np.asarray(grouped["wplays"], dtype=float)
        wins = np.asarray(grouped["wwins"], dtype=float)

        total = float(plays.sum())
        prior = float(wins.sum()) / total if total > 0 else 0.5

        # A build whose items are missing from the item table cannot be scored
        # from them; it keeps the population rate so it neither wins nor is
        # dropped from a list it is legitimately part of.
        missing = pd.isna(matrix).any(axis=1)
        matrix = np.where(pd.isna(matrix), -1, matrix).astype(np.int64)

        unique, inverse = np.unique(matrix, return_inverse=True)
        inverse = inverse.reshape(matrix.shape)

        item_plays = np.zeros(len(unique), dtype=float)
        item_wins = np.zeros(len(unique), dtype=float)
        # Each build contributes its whole record to each of its six items,
        # which is what makes an item's sample the union of its builds'.
        np.add.at(item_plays, inverse, plays[:, None])
        np.add.at(item_wins, inverse, wins[:, None])

        item_rate = (item_wins + strength * prior) / (item_plays + strength)
        from_items = item_rate[inverse].mean(axis=1)

        own_rate = shrunk_rate(plays, wins, strength)
        scores = (1.0 - own_weight) * from_items + own_weight * own_rate
        return np.where(missing, prior, scores)

    def ranked_builds(
        self,
        god_id: int,
        queue_id: Optional[int] = None,
        role: Optional[str] = None,
        high_mmr: bool = False,
        require_starter: bool = False,
        starter_ids: Tuple[int, ...] = (),
        ranking=DEFAULT_RANKING,
        min_plays: float = MIN_CANDIDATE_PLAYS,
        # Deep enough to reach an anti-heal build. Twenty-four was chosen when
        # the only consumer was the three-way fork, which never looks past the
        # twelfth; `build_engine.promote_anti_heal` needs the pool to actually
        # contain the build it is allowed to promote, and across the Smite 2
        # roster the highest-ranked anti-heal build sits at a median position of
        # eighteen with a long tail past twenty-four.
        limit: int = 64,
    ) -> List[Dict]:
        """The best builds for these filters, best first.

        `best_build` answers with one, which is all `/build` ever needed while
        it showed a single grid. Showing the conditional tree needs a *set* —
        the branch that presses an advantage and the one that survives a
        deficit are two more builds out of the same ranking, not a separate
        calculation — so this exposes what the ranking already computed instead
        of running it three times.
        """
        selected = self.__filter(self.builds, god_id, queue_id, role, high_mmr)
        if not selected.shape[0]:
            return []

        grouped = selected.groupby("BuildHash", observed=True).sum(numeric_only=True)
        if not grouped.shape[0]:
            return []

        if require_starter and len(starter_ids):
            with_starter = self.__hashes_with_starter(grouped.index, starter_ids)
            if any(with_starter):
                grouped = grouped.loc[with_starter]
        if min_plays > 1:
            supported = grouped[grouped["plays"] >= min_plays]
            if supported.shape[0]:
                grouped = supported

        if ranking == ADDITIVE:
            rank = self.additive_scores(grouped)
        else:
            scorer = (
                ranking if callable(ranking) else RANKINGS.get(ranking, shrunk_rate)
            )
            rank = scorer(grouped["wplays"], grouped["wwins"])

        order = np.argsort(np.asarray(rank))[::-1][:limit]
        estimates = shrunk_estimate(grouped["wplays"], grouped["wwins"])
        baseline = pooled_rate(grouped["wplays"], grouped["wwins"])
        out: List[Dict] = []
        for position in order:
            build_hash = grouped.index[int(position)]
            items = self.items_for(build_hash)
            if len(items) != len(ITEM_COLUMNS):
                continue
            out.append(
                self.__described(
                    grouped, grouped.iloc[int(position)], build_hash, items,
                    float(rank[int(position)]),
                    estimate=float(estimates[int(position)]),
                    baseline=baseline,
                )
            )
        return out

    def __hashes_with_starter(self, hashes, starter_ids: Tuple[int, ...]):
        """Which of these build hashes contain at least one starter item."""
        known = self.items.reindex(hashes)
        matrix = known[ITEM_COLUMNS].to_numpy()
        return pd.Index(hashes)[np.isin(matrix, np.asarray(starter_ids)).any(axis=1)]

    def best_relics(
        self,
        god_id: int,
        queue_id: Optional[int] = None,
        role: Optional[str] = None,
        high_mmr: bool = False,
        ranking=DEFAULT_RANKING,
        min_plays: float = MIN_RELIC_PLAYS,
    ) -> Optional[List[int]]:
        """The highest-ranked relic pair, by win rate rather than popularity.

        Ranked the same way builds are, which it was not: this kept the old
        lower bound after `best_build` moved off it, and inherited exactly the
        failure that motivated the move. Relics are a *small* candidate space —
        dozens of pairs, not thousands of builds — so the rare ones are rarer
        still, and a pair with a handful of games could clear the bound and win.
        That is how Ymir came back with Teleport and Anhur with Blink: not
        builds anyone runs, just the luckiest thin sample in the pool.

        The floor matters more here than it does for builds, and is on by
        default for the same reason. Relic choice is close to settled per god
        and per lane, so a pair nobody brings is far more likely to be noise
        than insight — the opposite of a build, where a rare one can be a real
        edge.
        """
        selected = self.__filter(self.relics, god_id, queue_id, role, high_mmr)
        if not selected.shape[0]:
            return None

        grouped = selected.groupby("Relics", observed=True).sum(numeric_only=True)
        if not grouped.shape[0]:
            return None

        if min_plays > 1:
            floor = max(
                min_plays, float(grouped["plays"].max()) * RELATIVE_SUPPORT_FLOOR
            )
            supported = grouped[grouped["plays"] >= floor]
            # Same "only if something survives" rule used everywhere else: a
            # god nobody plays should still get an answer, just a shakier one.
            if supported.shape[0]:
                grouped = supported

        scorer = ranking if callable(ranking) else RANKINGS.get(ranking, shrunk_rate)
        rank = scorer(grouped["wplays"], grouped["wwins"])
        best = grouped.index[int(np.argmax(rank))]
        return [int(value) for value in str(best).split(",") if value]

    def best_starter(
        self,
        god_id: int,
        queue_id: Optional[int] = None,
        role: Optional[str] = None,
        high_mmr: bool = False,
        ranking=DEFAULT_RANKING,
        min_plays: float = MIN_STARTER_PLAYS,
    ) -> Optional[int]:
        """The highest-ranked starter, or None where the table has no answer.

        Smite 2 keeps the starter outside the six core slots, so it is ranked
        here the way relics are — same floor, same reasoning — instead of the
        stat model guessing one at display time.
        """
        if self.starters is None:
            return None
        selected = self.__filter(self.starters, god_id, queue_id, role, high_mmr)
        if not selected.shape[0]:
            return None

        grouped = selected.groupby("StarterId", observed=True).sum(numeric_only=True)
        if not grouped.shape[0]:
            return None

        if min_plays > 1:
            floor = max(
                min_plays, float(grouped["plays"].max()) * RELATIVE_SUPPORT_FLOOR
            )
            supported = grouped[grouped["plays"] >= floor]
            if supported.shape[0]:
                grouped = supported

        scorer = ranking if callable(ranking) else RANKINGS.get(ranking, shrunk_rate)
        rank = scorer(grouped["wplays"], grouped["wwins"])
        return int(grouped.index[int(np.argmax(rank))])

    def god_totals(
        self,
        god_id: int,
        queue_id: Optional[int] = None,
        role: Optional[str] = None,
        high_mmr: bool = False,
    ) -> Tuple[int, int]:
        """(plays, wins) for the god under these filters, across all builds."""
        selected = self.__filter(self.gods, god_id, queue_id, role, high_mmr)
        if not selected.shape[0]:
            return (0, 0)
        return (int(selected["plays"].sum()), int(selected["wins"].sum()))

    def aspect_share(
        self,
        god_id: int,
        queue_id: Optional[int] = None,
        role: Optional[str] = None,
        high_mmr: bool = False,
    ) -> Optional[float]:
        """What share of this lane's recorded games took the god's Aspect.

        None for Smite 1, for an aggregate written before the column existed,
        and for a cell with nothing in it — all three mean "no answer" rather
        than "nobody takes it", and the embed says nothing in that case.

        The Aspect is not a grouping key and should not become one; splitting a
        cell in two costs more evidence than knowing the Aspect adds, which
        `tools/aspect_value.py` measures. But it is not nothing either: uptake
        runs at 14% across Conquest and reaches 98% in the lanes an Aspect is
        what makes viable, so a recommendation is often an Aspect build that has
        never said so.
        """
        if "aspect_plays" not in self.gods.columns:
            return None
        selected = self.__filter(self.gods, god_id, queue_id, role, high_mmr)
        if not selected.shape[0]:
            return None
        plays = float(selected["plays"].sum())
        if plays <= 0:
            return None
        return float(selected["aspect_plays"].sum()) / plays

    def common_role(self, god_id: int, queue_id: Optional[int] = None) -> str:
        """The role this god is played in most often, optionally in one mode.

        The queue matters more than it looks. Asked without one, this answers
        from every mode at once, and Arena and Assault have no lanes at all —
        tracker.gg labels a role in them anyway, and an "Arena support" is a
        full damage build. A god whose Conquest lane is support can therefore
        come back as a carry, which is not a lane it is ever played in.
        """
        selected = self.gods[
            (self.gods["GodId"] == god_id) & (self.gods["Role"] != "Unknown")
        ]
        if queue_id is not None:
            selected = selected[selected["match_queue_id"] == queue_id]
        if not selected.shape[0]:
            return ""
        by_role = selected.groupby("Role", observed=True)["plays"].sum()
        return str(by_role.idxmax()) if by_role.shape[0] else ""

    def items_for(self, build_hash) -> List[int]:
        try:
            row = self.items.loc[build_hash]
        except KeyError:
            return []
        return [int(row[column]) for column in ITEM_COLUMNS]
