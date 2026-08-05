"""Deriving build columns from raw match rows.

Shared by SmiteProvider, which annotates the frame it serves to the bot, and by
the aggregate builder, which rolls the whole corpus into per-build win counts.
The two must agree exactly — a build hash computed one way and looked up the
other would silently match nothing — so the logic lives here rather than being
implemented twice.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

ITEM_COLUMNS: List[str] = [f"ItemId{slot}" for slot in range(1, 7)]
RELIC_COLUMNS: List[str] = [f"ActiveId{slot}" for slot in range(1, 3)]

# "No Relic" and "No Shard Relic" occupy a relic slot without filling it.
EMPTY_RELIC_IDS: Tuple[int, ...] = (0, 12333, 23795)


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


def annotate(frame: pd.DataFrame, items: Dict[int, object]) -> None:
    """Attach BuildHash, Relics, IsFullBuild and IsFullRelics in place.

    Column-wise throughout. The row-wise version this replaced rebuilt a Series
    and did eight dictionary lookups per player row, which was most of the cost
    of loading the corpus.
    """
    item_ids = id_matrix(frame, ITEM_COLUMNS)
    relic_ids = id_matrix(frame, RELIC_COLUMNS)

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
            if item.tier >= 3 or item.is_starter
        ),
        np.int64,
    )
    counts_toward_build.sort()
    is_full_build = usable & (
        np.isin(item_ids, counts_toward_build) & (item_ids != 0)
    ).all(axis=1)

    is_full_relics = usable & ~np.isin(
        relic_ids, np.array(EMPTY_RELIC_IDS, np.int64)
    ).any(axis=1)

    relic_text = np.char.add(
        np.char.add(relic_ids[:, 0].astype(str), ","), relic_ids[:, 1].astype(str)
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
