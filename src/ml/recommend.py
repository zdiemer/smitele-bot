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
from typing import Dict, List, NamedTuple, Sequence, Tuple

import numpy as np

import features
from model import NumpyScorer

DEFAULT_MODEL_FILENAME: str = "model.npz"
MIN_CANDIDATE_SUPPORT: int = 5


class Recommendation(NamedTuple):
    """One scored build.

    A tuple rather than an object because callers index it — `found[0][0]` is
    the item list — and adding the Smite 2 fields on the end keeps that reading
    true while giving the new ones somewhere to live.
    """

    items: List[int]
    # The raw sigmoid: a ranking, not a rate.
    score: float
    starter: int = 0
    aspect: int = 0
    # The calibrated score, and None unless the training run's calibration
    # improved on held-out days — so a caller that shows someone a percentage
    # cannot show an unchecked one.
    probability: float = None


class BuildRecommender:
    def __init__(self, scorer: NumpyScorer, candidates: Dict[int, np.ndarray]):
        self.scorer = scorer
        # god id -> (n_builds, width) array of *indices*, already encoded. The
        # width is the six item slots for Smite 1, and those plus the starter
        # and the Aspect for Smite 2 — a candidate is a build as that game
        # records one, not just its core items.
        self.candidates = candidates
        self.gods = features.Vocabulary.from_dict(scorer.meta["gods"])
        self.items = features.Vocabulary.from_dict(scorer.meta["items"])
        self.roles = features.Vocabulary.from_dict(scorer.meta["roles"])
        self.aspects = features.Vocabulary.from_dict(scorer.meta.get("aspects") or {})
        self.shape = features.shape_for(scorer.meta.get("game", "smite"))
        self.item_id_by_index = {
            index: item_id for item_id, index in self.items.index_by_id.items()
        }
        self.aspect_id_by_index = {
            index: aspect_id for aspect_id, index in self.aspects.index_by_id.items()
        }

    @property
    def test_auc(self) -> float:
        return float(self.scorer.meta.get("test_auc", float("nan")))

    @property
    def is_calibrated(self) -> bool:
        return self.scorer.is_calibrated

    def recommend(
        self,
        god_id: int,
        role: str = "",
        opponent_god_id: int = 0,
        enemy_god_ids: Sequence[int] = (),
        ally_god_ids: Sequence[int] = (),
        top_n: int = 3,
    ) -> List[Recommendation]:
        """Best-scoring known builds for this god in this matchup, best first."""
        pool = self.candidates.get(int(god_id))
        if pool is None or not len(pool):
            return []

        items, starters, aspects = self.__split(pool)
        count = len(pool)
        batch = {
            "god": np.full(count, self.gods.encode(god_id), np.int64),
            "opponent": np.full(count, self.gods.encode(opponent_god_id), np.int64),
            "role": np.full(
                count, self.roles.encode(features._role_index(str(role).lower())), np.int64
            ),
            "items": items,
            # Relics are not being recommended, so they are left absent rather
            # than guessed at; index 0 is masked out of the pooled mean.
            "relics": np.zeros((count, len(self.shape.relic_columns)), np.int64),
            "allies": self.__composition(ally_god_ids, features.TEAM_SIZE - 1, count),
            "enemies": self.__composition(enemy_god_ids, features.TEAM_SIZE, count),
            # Skill is normalised, so zero is the average player. Recommending
            # for a specific player would mean passing their real stats here.
            "skill": np.zeros((count, self.shape.skill_width), np.float32),
        }
        # The starter and the Aspect are not left absent the way relics are.
        # Both are context the candidate carries — it is a build that was
        # played, with the starter it was played with — and index 0 is what the
        # model saw for a *missing* one, which for a starter is 1% of Smite 2
        # rows. Passing zero would score every build as the rare case.
        if "starter" in self.scorer.context_fields:
            batch["starter"] = starters
        if "aspect" in self.scorer.context_fields:
            batch["aspect"] = aspects

        scores = self.scorer(batch)
        calibrated = self.scorer.calibrate(scores) if self.is_calibrated else None
        best = np.argsort(scores)[::-1][:top_n]
        return [
            Recommendation(
                items=[
                    self.item_id_by_index[index]
                    for index in items[row]
                    if index in self.item_id_by_index
                ],
                score=float(scores[row]),
                starter=self.item_id_by_index.get(int(starters[row]), 0),
                aspect=self.aspect_id_by_index.get(int(aspects[row]), 0),
                probability=None if calibrated is None else float(calibrated[row]),
            )
            for row in best
        ]

    def __split(self, pool: np.ndarray):
        """A candidate array into its item, starter and Aspect columns.

        Read by width rather than by the model's fields, so a candidates file
        written by an older run — six columns, no starter — still loads against
        a newer model instead of indexing off the end of it.
        """
        width = len(self.shape.item_columns)
        items = pool[:, :width]
        rows = len(pool)
        starters = (
            pool[:, width] if pool.shape[1] > width else np.zeros(rows, np.int64)
        )
        aspects = (
            pool[:, width + 1] if pool.shape[1] > width + 1 else np.zeros(rows, np.int64)
        )
        return items, starters, aspects

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
    frame,
    items: features.Vocabulary,
    min_support: int = MIN_CANDIDATE_SUPPORT,
    shape: features.Shape = features.SMITE1,
    aspects: features.Vocabulary = None,
) -> Dict[int, np.ndarray]:
    """Distinct builds actually played per god, above a support threshold.

    Builds are keyed as a sorted item set: purchase order varies run to run and
    is not a meaningful difference between two otherwise identical builds. The
    starter and the Aspect are appended unsorted after it, because they are not
    interchangeable with the core items and are distinct choices — the same six
    items behind two different Aspects are two candidates, and the model is
    being asked to tell them apart.
    """
    encoded = np.stack(
        [items.encode_series(frame[slot]) for slot in shape.item_columns], axis=1
    )
    starters = (
        items.encode_series(frame[shape.starter_column])
        if shape.starter_column
        else None
    )
    aspect_indices = (
        (aspects or features.Vocabulary()).encode_series(frame[shape.aspect_column])
        if shape.aspect_column
        else None
    )
    god_ids = frame["GodId"].astype(int).to_numpy()

    seen: Dict[int, Counter] = {}
    for row in range(len(god_ids)):
        build = tuple(sorted(int(v) for v in encoded[row] if v > 0))
        if len(build) != len(shape.item_columns):
            continue
        if starters is not None:
            build += (int(starters[row]), int(aspect_indices[row]))
        seen.setdefault(int(god_ids[row]), Counter())[build] += 1

    out: Dict[int, np.ndarray] = {}
    for god_id, counter in seen.items():
        builds = [b for b, n in counter.items() if n >= min_support]
        if builds:
            out[god_id] = np.array(builds, dtype=np.int64)
    return out
