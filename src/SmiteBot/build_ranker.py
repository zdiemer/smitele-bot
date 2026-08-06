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

Ranking is the lower bound of a 95% confidence interval on the win rate, so a
build at 58% over two thousand games outranks one at 80% over five. Counts are
recency-weighted, which means a build whose evidence is mostly years old has a
smaller effective sample and a correspondingly wider interval — it has to be
better to win the comparison.
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


def agresti_coull_lower(plays: np.ndarray, wins: np.ndarray) -> np.ndarray:
    """Lower bound of the 95% interval on win rate, vectorised.

    The same estimator the row-wise version used, applied to every candidate at
    once rather than to a pre-filtered subset.
    """
    plays = np.asarray(plays, dtype=float)
    wins = np.asarray(wins, dtype=float)

    kest = wins + KAPPA**2 / 2
    nest = plays + KAPPA**2
    pest = kest / nest
    radius = KAPPA * np.sqrt(pest * (1 - pest) / nest)
    return np.maximum(0.0, pest - radius)


class BuildStats:
    """The aggregate tables, and the queries /build makes against them."""

    FILES = ("build_stats", "build_items", "relic_stats", "god_stats")

    def __init__(
        self,
        builds: pd.DataFrame,
        items: pd.DataFrame,
        relics: pd.DataFrame,
        gods: pd.DataFrame,
    ):
        self.builds = builds
        self.items = items.set_index("BuildHash")
        self.relics = relics
        self.gods = gods

    @staticmethod
    def load(directory: str) -> Optional["BuildStats"]:
        """Load the aggregate, or None if it hasn't been built yet.

        Absence is a normal state — the bot can start before the first
        aggregate run — so this returns None rather than raising.
        """
        paths = {
            name: os.path.join(directory, f"{name}.parquet") for name in BuildStats.FILES
        }
        if not all(os.path.isfile(path) for path in paths.values()):
            return None
        return BuildStats(*(pd.read_parquet(paths[name]) for name in BuildStats.FILES))

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
    ) -> Optional[Dict]:
        """The highest-ranked build for these filters, or None if there is none."""
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

        rank = agresti_coull_lower(grouped["wplays"], grouped["wwins"])
        best_hash = grouped.index[int(np.argmax(rank))]
        row = grouped.loc[best_hash]

        wins = float(row["wins"])
        return {
            "build_hash": best_hash,
            "items": self.items_for(best_hash),
            "plays": int(row["plays"]),
            "wins": int(row["wins"]),
            "win_rate": float(row["wins"]) / max(float(row["plays"]), 1.0),
            "rank": float(np.max(rank)),
            "unique_builds": int(grouped.shape[0]),
            # Stat sums are over winning rows, so the divisor is the win count.
            "avg_kills": float(row.get("sum_Kills_Player", 0.0)) / max(wins, 1.0),
            "avg_deaths": float(row.get("sum_Deaths", 0.0)) / max(wins, 1.0),
            "avg_assists": float(row.get("sum_Assists", 0.0)) / max(wins, 1.0),
            "avg_damage": float(row.get("sum_Damage_Player", 0.0)) / max(wins, 1.0),
            "avg_rating": (
                float(row.get("sum_rating", 0.0)) / float(row["rated_wins"])
                if float(row.get("rated_wins", 0.0)) > 0
                else 0.0
            ),
            "avg_tier": (
                float(row.get("sum_tier", 0.0)) / float(row["rated_wins"])
                if float(row.get("rated_wins", 0.0)) > 0
                else 0.0
            ),
        }

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
    ) -> Optional[List[int]]:
        """The highest-ranked relic pair, by win rate rather than popularity."""
        selected = self.__filter(self.relics, god_id, queue_id, role, high_mmr)
        if not selected.shape[0]:
            return None

        grouped = selected.groupby("Relics", observed=True).sum(numeric_only=True)
        if not grouped.shape[0]:
            return None

        rank = agresti_coull_lower(grouped["wplays"], grouped["wwins"])
        best = grouped.index[int(np.argmax(rank))]
        return [int(value) for value in str(best).split(",") if value]

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

    def common_role(self, god_id: int) -> str:
        """The role this god is played in most often."""
        selected = self.gods[
            (self.gods["GodId"] == god_id) & (self.gods["Role"] != "Unknown")
        ]
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
