"""Turn the win-probability model into a build recommendation.

The model scores a (context, build) pair; it never emits a build. A
recommendation is therefore a search: hold the matchup fixed, vary the items,
and return the highest-scoring builds. This runs in the bot, so it is numpy
only — no torch.

The candidate set is drawn from builds that were actually played on this god,
rather than from all combinations of every item. Two reasons. The combinatorial
space is ~330 choose 6, which is far too large to score exhaustively and mostly
nonsense. And the model has only ever seen plausible builds, so its score on an
arbitrary six-item pile is an extrapolation with nothing behind it — searching
outside the data would reliably produce confident garbage.

What comes back is therefore "of the builds people actually run on this god,
these score best against this composition", which is the honest version of the
question.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Dict, List, Sequence, Tuple

import numpy as np

import features
from model import NumpyScorer

DEFAULT_MODEL_FILENAME: str = "model.npz"
MIN_CANDIDATE_SUPPORT: int = 5


class BuildRecommender:
    def __init__(self, scorer: NumpyScorer, candidates: Dict[int, np.ndarray]):
        self.scorer = scorer
        # god id -> (n_builds, 6) array of item *indices*, already encoded.
        self.candidates = candidates
        self.gods = features.Vocabulary.from_dict(scorer.meta["gods"])
        self.items = features.Vocabulary.from_dict(scorer.meta["items"])
        self.roles = features.Vocabulary.from_dict(scorer.meta["roles"])
        self.item_id_by_index = {
            index: item_id for item_id, index in self.items.index_by_id.items()
        }

    @property
    def test_auc(self) -> float:
        return float(self.scorer.meta.get("test_auc", float("nan")))

    def recommend(
        self,
        god_id: int,
        role: str = "",
        opponent_god_id: int = 0,
        enemy_god_ids: Sequence[int] = (),
        ally_god_ids: Sequence[int] = (),
        top_n: int = 3,
    ) -> List[Tuple[List[int], float]]:
        """Best-scoring known builds for this god in this matchup.

        Returns (item ids, predicted win probability), best first.
        """
        pool = self.candidates.get(int(god_id))
        if pool is None or not len(pool):
            return []

        count = len(pool)
        batch = {
            "god": np.full(count, self.gods.encode(god_id), np.int64),
            "opponent": np.full(count, self.gods.encode(opponent_god_id), np.int64),
            "role": np.full(
                count, self.roles.encode(features._role_index(str(role).lower())), np.int64
            ),
            "items": pool,
            # Relics are not being recommended, so they are left absent rather
            # than guessed at; index 0 is masked out of the pooled mean.
            "relics": np.zeros((count, len(features.RELIC_SLOTS)), np.int64),
            "allies": self.__composition(ally_god_ids, features.TEAM_SIZE - 1, count),
            "enemies": self.__composition(enemy_god_ids, features.TEAM_SIZE, count),
            # Skill is normalised, so zero is the average player. Recommending
            # for a specific player would mean passing their real stats here.
            "skill": np.zeros((count, len(features.SKILL_FEATURES)), np.float32),
        }

        scores = self.scorer(batch)
        best = np.argsort(scores)[::-1][:top_n]
        return [
            (
                [
                    self.item_id_by_index[index]
                    for index in pool[row]
                    if index in self.item_id_by_index
                ],
                float(scores[row]),
            )
            for row in best
        ]

    def __composition(
        self, god_ids: Sequence[int], width: int, rows: int
    ) -> np.ndarray:
        encoded = np.zeros((1, width), np.int64)
        for slot, god_id in enumerate(list(god_ids)[:width]):
            encoded[0, slot] = self.gods.encode(god_id)
        return np.repeat(encoded, rows, axis=0)

    @staticmethod
    def load(directory: str = None, filename: str = DEFAULT_MODEL_FILENAME):
        """Load a trained model plus its candidate builds, or None if absent.

        Missing is a normal state — the bot runs fine before the first training
        run — so this returns None rather than raising.
        """
        path = os.path.join(directory, filename)
        candidates_path = os.path.join(directory, "candidates.npz")
        if not (os.path.isfile(path) and os.path.isfile(candidates_path)):
            return None

        scorer = NumpyScorer.load(path)
        raw = np.load(candidates_path, allow_pickle=False)
        candidates = {int(key): raw[key] for key in raw.files}
        return BuildRecommender(scorer, candidates)


def extract_candidates(
    frame, items: features.Vocabulary, min_support: int = MIN_CANDIDATE_SUPPORT
) -> Dict[int, np.ndarray]:
    """Distinct builds actually played per god, above a support threshold.

    Builds are keyed as a sorted item set: purchase order varies run to run and
    is not a meaningful difference between two otherwise identical builds.
    """
    encoded = np.stack(
        [items.encode_series(frame[slot]) for slot in features.ITEM_SLOTS], axis=1
    )
    god_ids = frame["GodId"].astype(int).to_numpy()

    seen: Dict[int, Counter] = {}
    for row in range(len(god_ids)):
        build = tuple(sorted(int(v) for v in encoded[row] if v > 0))
        if len(build) != len(features.ITEM_SLOTS):
            continue
        seen.setdefault(int(god_ids[row]), Counter())[build] += 1

    out: Dict[int, np.ndarray] = {}
    for god_id, counter in seen.items():
        builds = [b for b, n in counter.items() if n >= min_support]
        if builds:
            out[god_id] = np.array(builds, dtype=np.int64)
    return out
