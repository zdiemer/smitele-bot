"""Deriving build columns from raw match rows.

Shared by SmiteProvider, which annotates the frame it serves to the bot, and by
the aggregate builder, which rolls the whole corpus into per-build win counts.
The two must agree exactly — a build hash computed one way and looked up the
other would silently match nothing — so the logic lives here rather than being
implemented twice.

The two games disagree about the shape of a build without disagreeing about any
of the arithmetic. Smite 1 has two relic slots and counts a starter as filling a
core slot; Smite 2 has one relic, and its starter lives in its own column
because putting it in a core slot would make a five-item build look complete.
`BuildShape` carries that difference so neither the hashing nor the annotation
has to be written twice — and `annotate` defaults to the Smite 1 shape, so every
existing caller and every byte of the existing aggregate is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

import numpy as np
import pandas as pd

ITEM_COLUMNS: List[str] = [f"ItemId{slot}" for slot in range(1, 7)]
RELIC_COLUMNS: List[str] = [f"ActiveId{slot}" for slot in range(1, 3)]

# "No Relic" and "No Shard Relic" occupy a relic slot without filling it.
EMPTY_RELIC_IDS: Tuple[int, ...] = (0, 12333, 23795)


@dataclass(frozen=True)
class BuildShape:
    """How one game lays a build out across corpus columns."""

    item_columns: List[str]
    relic_columns: List[str]
    empty_relic_ids: Tuple[int, ...]
    counts_toward_build: Callable[[object], bool]
    # Smite 2 records the starter outside the core slots; Smite 1 has no such
    # column because a starter occupies a core slot there.
    starter_column: str = None


SMITE1 = BuildShape(
    item_columns=ITEM_COLUMNS,
    relic_columns=RELIC_COLUMNS,
    empty_relic_ids=EMPTY_RELIC_IDS,
    counts_toward_build=lambda item: item.tier >= 3 or item.is_starter,
)

SMITE2 = BuildShape(
    item_columns=ITEM_COLUMNS,
    # One relic, not two. There is no analogue of ActiveId2, and the collector
    # writes it as a constant 0 so the ML vocabularies keep working unchanged.
    relic_columns=["ActiveId1"],
    empty_relic_ids=(0,),
    # No `or is_starter`: the starter is carried in StarterId rather than a core
    # slot, which the tier-3 catalogue measured on the wiki supports exactly —
    # every Offensive/Defensive/Hybrid item has tier 3 and nothing else does.
    counts_toward_build=lambda item: item.tier >= 3,
    starter_column="StarterId",
)


def id_matrix(frame: pd.DataFrame, columns: List[str]) -> np.ndarray:
    """Item/relic id columns as one integer matrix.

    Anything unparseable becomes -1, which matches no known id and so falls out
    as unusable rather than raising.
    """
    return (
        frame[columns]
        .apply(pd.to_numeric, errors="coerce")
        .fillna(-1)
        .to_numpy(np.int64)
    )


def hash_builds(items: np.ndarray) -> np.ndarray:
    """triple32 over every slot, summed per row.

    Order-independent, because the slots are summed. Runs in uint64 where the
    original scalar version used unbounded Python ints; values are recomputed
    on every load and only ever compared within one, so nothing persisted
    depends on the exact numbers.
    """
    x = items.astype(np.uint64)
    x ^= x >> np.uint64(17)
    x *= np.uint64(0xED5AD4BB)
    x ^= x >> np.uint64(11)
    x *= np.uint64(0xAC4C1B51)
    x ^= x >> np.uint64(15)
    x *= np.uint64(0x31848BAB)
    x ^= x >> np.uint64(14)
    return x.sum(axis=1, dtype=np.uint64)


def annotate(
    frame: pd.DataFrame, items: Dict[int, object], shape: BuildShape = SMITE1
) -> None:
    """Attach BuildHash, Relics, IsFullBuild and IsFullRelics in place.

    Column-wise throughout. The row-wise version this replaced rebuilt a Series
    and did eight dictionary lookups per player row, which was most of the cost
    of loading the corpus.

    `shape` defaults to Smite 1, so this is bit-identical for every caller that
    does not pass one.
    """
    item_ids = id_matrix(frame, shape.item_columns)
    relic_ids = id_matrix(frame, shape.relic_columns)

    known = np.fromiter(items.keys(), np.int64, len(items))
    known.sort()
    # The original bailed out entirely on an id it didn't recognise — in either
    # the item or the relic slots — so an unknown relic suppressed the build
    # hash too. Preserved deliberately.
    usable = np.isin(item_ids, known).all(axis=1) & np.isin(relic_ids, known).all(
        axis=1
    )

    counts_toward_build = np.fromiter(
        (
            item_id
            for item_id, item in items.items()
            if shape.counts_toward_build(item)
        ),
        np.int64,
    )
    counts_toward_build.sort()
    is_full_build = usable & (
        np.isin(item_ids, counts_toward_build) & (item_ids != 0)
    ).all(axis=1)

    is_full_relics = usable & ~np.isin(
        relic_ids, np.array(shape.empty_relic_ids, np.int64)
    ).any(axis=1)

    # Joined across however many relic columns the game has. Smite 2's single
    # relic yields "12345" where Smite 1 yields "200,201"; build_ranker splits
    # on the comma either way and gets a list of the right length.
    relic_text = relic_ids[:, 0].astype(str)
    for column in range(1, relic_ids.shape[1]):
        relic_text = np.char.add(
            np.char.add(relic_text, ","), relic_ids[:, column].astype(str)
        )

    # A nullable UInt64 rather than object. The hash routinely exceeds int64's
    # range, and an object column of Python ints overflows on the way into
    # Parquet — which the aggregate builder has to write.
    build_hash = pd.array(hash_builds(item_ids), dtype="UInt64")
    build_hash[~is_full_build] = pd.NA

    frame["BuildHash"] = build_hash
    frame["Relics"] = np.where(is_full_relics, relic_text, None)
    frame["IsFullBuild"] = is_full_build
    frame["IsFullRelics"] = is_full_relics
