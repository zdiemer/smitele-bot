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

import dataclasses
import os
import re
from typing import Dict, List, Optional, Tuple

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


@dataclasses.dataclass(frozen=True)
class Shape:
    """Which corpus columns are a training row, for one game.

    `build_features.BuildShape` already says how a *build* is laid out; this is
    the model's version of the same question, and it is deliberately separate.
    That one exists to hash a build identically in the aggregate and the bot, so
    it carries what counts toward a build. This one decides what the network
    gets to look at, which is a different list — it includes the matchup and the
    skill columns, and it excludes anything the corpus records as a constant.

    Constants are the reason a shape is needed at all rather than a single union
    of columns. tracker.gg cannot see a Smite 2 player's account level, mastery
    or Conquest tier, so `rows.py` writes those as literal zeros to keep the
    downstream column names identical. Fed to the model they are three inputs
    with no variance: they cost parameters, they dilute the normalisation, and
    they contribute nothing. Meanwhile the two columns that *are* Smite 2's own
    signal — the starter, filled on 99% of rows, and the Aspect on 15% — were
    not in the Smite 1 layout at all and so were never read.
    """

    item_columns: List[str]
    relic_columns: List[str]
    skill_features: List[str]
    # Columns where "recorded at all" is its own signal. A Smite 2 row outside
    # Ranked Conquest has no rating, and a rating of zero normalises to roughly
    # the mean — so without this the model cannot tell an unranked player from
    # an average one. The indicator is 1.0 when the column is non-zero.
    skill_indicators: Tuple[str, ...] = ()
    # Smite 2 records the starter outside the six core slots, and the Aspect is
    # a selection-time choice with no Smite 1 analogue. Both are single ids, so
    # they join the context fields rather than the pooled ones.
    starter_column: Optional[str] = None
    aspect_column: Optional[str] = None

    @property
    def skill_width(self) -> int:
        return len(self.skill_features) + len(self.skill_indicators)

    @property
    def context_fields(self) -> List[str]:
        """Single-id inputs, in the order the feature vector concatenates them."""
        fields = list(BASE_CONTEXT_FIELDS)
        if self.starter_column:
            fields.append("starter")
        if self.aspect_column:
            fields.append("aspect")
        return fields

    def build_columns(self) -> List[str]:
        """Every column that describes the build itself."""
        columns = list(self.item_columns) + list(self.relic_columns)
        for column in (self.starter_column, self.aspect_column):
            if column:
                columns.append(column)
        return columns


# The context fields every game has. `model.CONTEXT_FIELDS` is the same list;
# it lives there too so the scorer can be read without this module.
BASE_CONTEXT_FIELDS: Tuple[str, ...] = ("god", "opponent", "role")

SMITE1 = Shape(
    item_columns=ITEM_SLOTS,
    relic_columns=RELIC_SLOTS,
    skill_features=SKILL_FEATURES,
)

SMITE2 = Shape(
    item_columns=ITEM_SLOTS,
    # One relic. ActiveId2 is written as a constant 0 by the collector, so
    # reading it would pool a permanent absence into every build's relic mean.
    relic_columns=["ActiveId1"],
    # The only skill column tracker.gg fills, and only for Ranked Conquest.
    skill_features=["Rank_Stat_Conquest"],
    skill_indicators=("Rank_Stat_Conquest",),
    starter_column="StarterId",
    aspect_column="Aspect",
)


def shape_for(game) -> Shape:
    """The shape for a `Game`, its value, or anything that stringifies to one.

    Takes the value rather than importing `Game`, because this module is loaded
    by the bot, by the trainer and by the eval tool, and only some of those have
    `HirezAPI` on the path at import time.
    """
    return SMITE2 if str(getattr(game, "value", game)) == "smite2" else SMITE1


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


def before_day(paths: List[str], cutoff: str) -> List[str]:
    """The files belonging to days strictly before `cutoff` (YYYY-MM-DD).

    The inverse of the window `build_eval --cutoff` holds out, so that a model
    can be trained without having seen the days it is about to be scored on.
    Undated files are dropped rather than kept, which is the opposite of what
    `recent_days` does and for the opposite reason: there, including an
    unlabelled file at worst adds old data; here it could be a held-out day,
    and a leaked evaluation is worth less than a smaller one.
    """
    keep = []
    for path in paths:
        found = _DATE_IN_NAME.search(os.path.basename(path))
        if found and found.group(1) < cutoff:
            keep.append(path)
    return sorted(keep)


def corpus_columns(shape: Shape = SMITE1) -> List[str]:
    """Every column a training row is built from, deduplicated.

    Shared by the trainer and by `build_eval`, which reads the held-out files
    itself rather than by directory and so has to ask for the same columns.
    """
    return list(
        dict.fromkeys(
            [
                "Match",
                "TaskForce",
                "Winning_TaskForce",
                "GodId",
                "Role",
                "match_queue_id",
            ]
            + shape.build_columns()
            + list(shape.skill_features)
        )
    )


def load_corpus(
    directories: List[str],
    queue_ids: List[int] = None,
    limit_files: int = None,
    max_files: int = None,
    shape: Shape = SMITE1,
    until: str = None,
) -> pd.DataFrame:
    """Read corpus files, keeping only the columns the model needs.

    max_files bounds the read itself. Sampling rows after loading cannot help
    when loading is what runs out of memory: the corpus is 3,300 files and 158M
    rows, and accumulating even the 8% that is Ranked Conquest exceeded 8GB.
    Files are sampled uniformly across the whole corpus rather than truncated
    to the most recent, so the sample still spans its full range.
    """
    columns = corpus_columns(shape)

    paths = match_storage.corpus_paths(*directories)
    if until:
        paths = before_day(paths, until)
        print(f"  {len(paths)} files from before {until}", flush=True)
    if limit_files:
        paths = recent_days(paths, limit_files)
    if max_files and len(paths) > max_files:
        step = len(paths) / max_files
        paths = [paths[int(i * step)] for i in range(max_files)]
        print(f"  sampling {len(paths)} files across the corpus", flush=True)

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
    shape: Shape = SMITE1,
    aspects: Vocabulary = None,
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
        [items.encode_series(frame[slot]) for slot in shape.item_columns], axis=1
    )
    encoded["relics"] = np.stack(
        [items.encode_series(frame[slot]) for slot in shape.relic_columns], axis=1
    )
    # The starter shares the item vocabulary — a starter is an item, and one
    # index space means a starter that upgrades into a core item is the same
    # id in both places. Its embedding table is still its own, because what a
    # starter does for a build is not what the same id does in slot four.
    if shape.starter_column:
        encoded["starter"] = items.encode_series(frame[shape.starter_column])
    if shape.aspect_column:
        encoded["aspect"] = (aspects or Vocabulary()).encode_series(
            frame[shape.aspect_column]
        )
    encoded["allies"] = _encode_composition(frame["ally_gods"], gods, TEAM_SIZE - 1)
    encoded["enemies"] = _encode_composition(frame["enemy_gods"], gods, TEAM_SIZE)

    encoded["skill"], skill_stats = encode_skill(frame, shape, skill_stats)

    return encoded, frame["won"].to_numpy(np.float32), skill_stats


def encode_skill(
    frame: pd.DataFrame,
    shape: Shape = SMITE1,
    skill_stats: Dict[str, Tuple[float, float]] = None,
) -> Tuple[np.ndarray, Dict[str, Tuple[float, float]]]:
    """The normalised skill block, plus the stats used to normalise it.

    Indicators are appended after the normalised columns and are deliberately
    not themselves normalised: a 0/1 flag means "this was recorded", and
    centring it would spread that meaning across both values.

    A column that has an indicator is normalised over its *observed* rows only,
    and its missing rows are imputed at that mean rather than left at zero.
    Both halves of that matter, and the first Smite 2 model is why. Half its
    rows have no Rank_Stat_Conquest, so filling with zero and normalising over
    the mixture put every unrated player 1.92 deviations below the mean — the
    model read "no rank recorded" as "worst player in the game" and quoted them
    a 6% win chance on days they won 51.6% of. Held-out ECE was 0.21. It also
    dragged the mean down for the rated players, so the rows that did carry a
    rank were mis-scaled too.

    Only columns with an indicator get this. Without a flag to carry the
    missingness there is nothing to distinguish an imputed row from a real
    average one, and Smite 1 — whose skill columns are near-always present —
    keeps the behaviour it was trained under.
    """
    columns = list(shape.skill_features)
    raw = np.stack(
        [
            pd.to_numeric(frame[column], errors="coerce").fillna(0.0).to_numpy(float)
            for column in columns
        ],
        axis=1,
    ) if columns else np.zeros((len(frame), 0), float)

    # Which rows actually recorded the column, for the columns that say so.
    observed = {
        column: raw[:, columns.index(column)] > 0
        for column in shape.skill_indicators
        if column in columns
    }

    if skill_stats is None:
        skill_stats = {}
        for i, column in enumerate(columns):
            values = raw[:, i]
            seen = observed.get(column)
            if seen is not None and seen.any():
                values = values[seen]
            skill_stats[column] = (float(values.mean()), float(values.std() or 1.0))

    skill = raw.copy()
    for i, column in enumerate(columns):
        mean, deviation = skill_stats.get(column, (0.0, 1.0))
        seen = observed.get(column)
        if seen is not None:
            skill[~seen, i] = mean
        skill[:, i] = (skill[:, i] - mean) / (deviation or 1.0)

    if shape.skill_indicators:
        flags = np.stack(
            [
                (
                    pd.to_numeric(frame[column], errors="coerce").fillna(0.0) > 0
                ).to_numpy(float)
                for column in shape.skill_indicators
            ],
            axis=1,
        )
        skill = np.concatenate([skill, flags], axis=1)

    return skill.astype(np.float32), skill_stats


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
    frame: pd.DataFrame, shape: Shape = SMITE1
) -> Tuple[Vocabulary, Vocabulary, Vocabulary, Vocabulary]:
    """Stable index maps for gods, items, roles and Aspects.

    The Aspect vocabulary is empty for a game that has none, which makes its
    embedding table one row — the "absent" row — rather than a special case in
    the model.
    """
    god_ids = pd.to_numeric(frame["GodId"], errors="coerce").dropna().astype(int)
    item_columns = list(shape.item_columns) + list(shape.relic_columns)
    if shape.starter_column:
        item_columns.append(shape.starter_column)
    item_ids = pd.concat(
        [pd.to_numeric(frame[slot], errors="coerce") for slot in item_columns]
    )
    item_ids = item_ids[item_ids > 0].dropna().astype(int)

    aspect_ids: List[int] = []
    if shape.aspect_column:
        values = pd.to_numeric(frame[shape.aspect_column], errors="coerce")
        aspect_ids = values[values > 0].dropna().astype(int).tolist()

    roles = Vocabulary(list(range(1, len(_ROLE_ORDER) + 1)))
    return (
        Vocabulary(god_ids.tolist()),
        Vocabulary(item_ids.tolist()),
        roles,
        Vocabulary(aspect_ids),
    )
