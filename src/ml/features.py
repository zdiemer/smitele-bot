"""Turns the match corpus into matchup-aware training rows.

The question the model answers is "given this god, in this lane, against this
enemy composition, which build maximises the chance of winning" — so a training
row has to carry the context a build is chosen in, not just the build. Each
player row in the corpus is expanded with:

  own god, role, and skill      who is playing, and how well they usually do
  direct opponent              the god in the same role on the other team,
                               which is the matchup a build is chosen against
  enemy composition            the five enemy gods, order-independent
  ally composition             the four team-mates, order-independent
  the build                    six items and two relics
  label                        whether that player's team won

Two things here matter more than they look.

Vocabularies are *stable*: gods and items map to indices derived from the
identifier itself, not from `astype("category").cat.codes`, which numbers
categories by their order of appearance within one file. Codes built that way
mean something different in every file, so a model trained on one and evaluated
on another is reading its embedding table through the wrong index — the
previous version did exactly this across its train/test split.

And the label comes from `Winning_TaskForce` compared against the player's own
`TaskForce`, rather than the `Win_Status` string, so it stays correct for rows
where that field is missing or inconsistent.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

import match_storage

# Slots that make up a build.
ITEM_SLOTS: List[str] = [f"ItemId{i}" for i in range(1, 7)]
RELIC_SLOTS: List[str] = ["ActiveId1", "ActiveId2"]

# Per-player context that isn't the build itself.
SKILL_FEATURES: List[str] = [
    "Account_Level",
    "Mastery_Level",
    "Rank_Stat_Conquest",
    "Conquest_Tier",
]

TEAM_SIZE: int = 5


class Vocabulary:
    """A stable id -> dense index map, with 0 reserved for unknown/absent.

    Built once from the full corpus and saved with the model. Anything unseen
    at inference time — a god released after training, an item that rotated
    out — lands on 0 rather than indexing past the end of an embedding table.
    """

    def __init__(self, ids: List[int] = None):
        self.index_by_id: Dict[int, int] = {}
        for identifier in sorted(set(ids or [])):
            self.index_by_id[int(identifier)] = len(self.index_by_id) + 1

    def __len__(self) -> int:
        return len(self.index_by_id) + 1

    def encode(self, identifier) -> int:
        try:
            return self.index_by_id.get(int(identifier), 0)
        except (TypeError, ValueError):
            return 0

    def encode_series(self, series: pd.Series) -> np.ndarray:
        numeric = pd.to_numeric(series, errors="coerce").fillna(-1).astype(np.int64)
        mapping = self.index_by_id
        return numeric.map(lambda value: mapping.get(value, 0)).to_numpy(np.int64)

    def to_dict(self) -> Dict[str, int]:
        return {str(k): v for k, v in self.index_by_id.items()}

    @staticmethod
    def from_dict(raw: Dict[str, int]) -> "Vocabulary":
        vocabulary = Vocabulary()
        vocabulary.index_by_id = {int(k): int(v) for k, v in raw.items()}
        return vocabulary


_DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")


def recent_days(paths: List[str], days: int) -> List[str]:
    """The files belonging to the most recent `days` distinct days.

    Not the last N files: a day is written as however many Parquet parts its
    size required, so slicing the file list took ~60 parts — about four days —
    when asked for sixty. Files with no date in the name are kept, since
    dropping data because it is unlabelled is worse than including it.
    """
    dated = {}
    undated = []
    for path in paths:
        found = _DATE_IN_NAME.search(os.path.basename(path))
        if found:
            dated.setdefault(found.group(1), []).append(path)
        else:
            undated.append(path)

    keep = sorted(dated)[-days:]
    return sorted(undated + [p for day in keep for p in dated[day]])


def load_corpus(
    directories: List[str], queue_ids: List[int] = None, limit_files: int = None
) -> pd.DataFrame:
    """Read corpus files, keeping only the columns the model needs."""
    columns = list(
        dict.fromkeys(
            [
                "Match",
                "TaskForce",
                "Winning_TaskForce",
                "GodId",
                "Role",
                "match_queue_id",
            ]
            + ITEM_SLOTS
            + RELIC_SLOTS
            + SKILL_FEATURES
        )
    )

    paths = match_storage.corpus_paths(*directories)
    if limit_files:
        paths = recent_days(paths, limit_files)

    frames = []
    for path in paths:
        frame = match_storage.read_frame_columns(path, columns)
        if queue_ids:
            frame = frame[
                pd.to_numeric(frame["match_queue_id"], errors="coerce").isin(queue_ids)
            ]
        if frame.shape[0]:
            frames.append(frame)
        print(
            f"  loaded {os.path.basename(path)}: {frame.shape[0]:,} rows", flush=True
        )

    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


def build_matchup_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach opponent, ally and enemy gods to every player row.

    Done with joins over the match/team grouping rather than per-row lookups;
    at a million rows the row-wise version is the difference between seconds
    and most of an hour.
    """
    frame = frame.copy()
    frame["GodId"] = pd.to_numeric(frame["GodId"], errors="coerce").fillna(-1)
    frame["TaskForce"] = pd.to_numeric(frame["TaskForce"], errors="coerce").fillna(-1)
    frame["Winning_TaskForce"] = pd.to_numeric(
        frame["Winning_TaskForce"], errors="coerce"
    ).fillna(-1)

    # Only matches with a full, well-formed roster are usable: a partial match
    # would silently produce a composition padded with absences.
    sizes = frame.groupby("Match")["GodId"].transform("size")
    frame = frame[sizes == TEAM_SIZE * 2]
    frame = frame[frame["Winning_TaskForce"].isin([1, 2])]
    if not frame.shape[0]:
        return frame

    frame["won"] = (frame["TaskForce"] == frame["Winning_TaskForce"]).astype(np.int8)

    # Gods per match/team, then subtract self to get allies and flip the team
    # to get enemies.
    by_team = (
        frame.groupby(["Match", "TaskForce"])["GodId"].apply(list).rename("team_gods")
    )
    lookup = by_team.to_dict()

    def compositions(row) -> Tuple[List[float], List[float]]:
        own = lookup.get((row.Match, row.TaskForce), [])
        other = lookup.get((row.Match, 2 if row.TaskForce == 1 else 1), [])
        allies = list(own)
        try:
            allies.remove(row.GodId)
        except ValueError:
            pass
        return allies, list(other)

    allies_and_enemies = [compositions(row) for row in frame.itertuples(index=False)]
    frame["ally_gods"] = [pair[0] for pair in allies_and_enemies]
    frame["enemy_gods"] = [pair[1] for pair in allies_and_enemies]

    # The direct opponent: same role, other team. Role strings are inconsistent
    # in the data, so this is a best effort and absence is encoded as unknown.
    frame["role_key"] = frame["Role"].astype(str).str.strip().str.lower()
    opponents = frame[["Match", "TaskForce", "role_key", "GodId"]].rename(
        columns={"GodId": "opponent_god", "TaskForce": "opponent_force"}
    )
    frame = frame.merge(
        opponents,
        left_on=["Match", "role_key"],
        right_on=["Match", "role_key"],
        how="left",
    )
    frame = frame[frame["TaskForce"] != frame["opponent_force"]]
    frame = frame.drop_duplicates(subset=["Match", "TaskForce", "GodId", "role_key"])
    frame["opponent_god"] = frame["opponent_god"].fillna(-1)

    return frame


def encode(
    frame: pd.DataFrame,
    gods: Vocabulary,
    items: Vocabulary,
    roles: Vocabulary,
    skill_stats: Dict[str, Tuple[float, float]] = None,
) -> Tuple[Dict[str, np.ndarray], np.ndarray, Dict[str, Tuple[float, float]]]:
    """Encode a matchup frame into model inputs.

    Returns the feature dict, the labels, and the skill normalisation stats so
    training can hand them to inference unchanged.
    """
    encoded: Dict[str, np.ndarray] = {
        "god": gods.encode_series(frame["GodId"]),
        "opponent": gods.encode_series(frame["opponent_god"]),
        "role": np.array(
            [roles.encode(_role_index(value)) for value in frame["role_key"]],
            dtype=np.int64,
        ),
    }

    encoded["items"] = np.stack(
        [items.encode_series(frame[slot]) for slot in ITEM_SLOTS], axis=1
    )
    encoded["relics"] = np.stack(
        [items.encode_series(frame[slot]) for slot in RELIC_SLOTS], axis=1
    )
    encoded["allies"] = _encode_composition(frame["ally_gods"], gods, TEAM_SIZE - 1)
    encoded["enemies"] = _encode_composition(frame["enemy_gods"], gods, TEAM_SIZE)

    skill = np.stack(
        [
            pd.to_numeric(frame[column], errors="coerce").fillna(0.0).to_numpy(float)
            for column in SKILL_FEATURES
        ],
        axis=1,
    )
    if skill_stats is None:
        skill_stats = {
            column: (float(skill[:, i].mean()), float(skill[:, i].std() or 1.0))
            for i, column in enumerate(SKILL_FEATURES)
        }
    for i, column in enumerate(SKILL_FEATURES):
        mean, deviation = skill_stats[column]
        skill[:, i] = (skill[:, i] - mean) / (deviation or 1.0)
    encoded["skill"] = skill.astype(np.float32)

    return encoded, frame["won"].to_numpy(np.float32), skill_stats


_ROLE_ORDER = ["solo", "jungle", "mid", "support", "carry", "adc"]


def _role_index(value: str) -> int:
    try:
        return _ROLE_ORDER.index(value) + 1
    except ValueError:
        return 0


def _encode_composition(
    column: pd.Series, gods: Vocabulary, width: int
) -> np.ndarray:
    """Fixed-width, zero-padded god indices for a team composition."""
    out = np.zeros((len(column), width), dtype=np.int64)
    for row, values in enumerate(column):
        for slot, identifier in enumerate(list(values)[:width]):
            out[row, slot] = gods.encode(identifier)
    return out


def build_vocabularies(
    frame: pd.DataFrame,
) -> Tuple[Vocabulary, Vocabulary, Vocabulary]:
    god_ids = pd.to_numeric(frame["GodId"], errors="coerce").dropna().astype(int)
    item_ids = pd.concat(
        [
            pd.to_numeric(frame[slot], errors="coerce")
            for slot in ITEM_SLOTS + RELIC_SLOTS
        ]
    )
    item_ids = item_ids[item_ids > 0].dropna().astype(int)

    roles = Vocabulary(list(range(1, len(_ROLE_ORDER) + 1)))
    return Vocabulary(god_ids.tolist()), Vocabulary(item_ids.tolist()), roles
