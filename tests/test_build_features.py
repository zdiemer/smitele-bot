"""`annotate` decides which rows become build statistics, and gets no second
look — a build hashed wrongly does not raise, it just aggregates into the wrong
bucket or silently matches nothing. So the values below are pinned.

`EXPECTED_*` were captured from the implementation as it stood *before*
`BuildShape` existed. That is the point of them: the Smite 2 work must not move
a single Smite 1 number, and the existing aggregate on the NAS is 250 days deep
with no way to tell by eye that it drifted.
"""

from __future__ import annotations

import types

import pandas as pd
import pytest

import build_features


def item(tier: int, is_starter: bool = False):
    return types.SimpleNamespace(tier=tier, is_starter=is_starter)


ITEMS = {
    0: item(0),
    12333: item(0),  # "No Relic"
    23795: item(0),  # "No Shard Relic"
    100: item(3),
    101: item(3),
    102: item(3),
    103: item(3),
    104: item(3),
    105: item(3),
    106: item(1, is_starter=True),
    107: item(2),  # a real item, but not one that completes a build
    200: item(0),  # relics
    201: item(0),
}

ROWS = [
    # 0  full build, full relics
    dict(ItemId1=100, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
         ItemId6=105, ActiveId1=200, ActiveId2=201),
    # 1  the same six items reversed — the hash must match row 0
    dict(ItemId1=105, ItemId2=104, ItemId3=103, ItemId4=102, ItemId5=101,
         ItemId6=100, ActiveId1=200, ActiveId2=201),
    # 2  a starter counts toward a full build in Smite 1
    dict(ItemId1=106, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
         ItemId6=105, ActiveId1=200, ActiveId2=201),
    # 3  a tier-2 item disqualifies it
    dict(ItemId1=107, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
         ItemId6=105, ActiveId1=200, ActiveId2=201),
    # 4  an empty slot disqualifies it
    dict(ItemId1=0, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
         ItemId6=105, ActiveId1=200, ActiveId2=201),
    # 5  an unknown id suppresses the relics too, not just the build
    dict(ItemId1=999, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
         ItemId6=105, ActiveId1=200, ActiveId2=201),
    # 6  "No Relic" fills a relic slot without filling it
    dict(ItemId1=100, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
         ItemId6=105, ActiveId1=12333, ActiveId2=201),
    # 7  an unparseable id falls out rather than raising
    dict(ItemId1="oops", ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
         ItemId6=105, ActiveId1=200, ActiveId2=201),
]

EXPECTED_BUILD_HASH = [
    438159557988013564,
    438159557988013564,
    10224288508711552672,
    None,
    None,
    None,
    438159557988013564,
    None,
]
EXPECTED_RELICS = [
    "200,201", "200,201", "200,201", "200,201", "200,201", None, None, None
]
EXPECTED_FULL_BUILD = [True, True, True, False, False, False, True, False]
EXPECTED_FULL_RELICS = [True, True, True, True, True, False, False, False]


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame(ROWS)


def test_smite1_output_is_unchanged(frame):
    """No shape argument must behave exactly as it did before BuildShape."""
    build_features.annotate(frame, ITEMS)

    assert [
        None if pd.isna(v) else int(v) for v in frame["BuildHash"]
    ] == EXPECTED_BUILD_HASH
    assert list(frame["Relics"]) == EXPECTED_RELICS
    assert [bool(v) for v in frame["IsFullBuild"]] == EXPECTED_FULL_BUILD
    assert [bool(v) for v in frame["IsFullRelics"]] == EXPECTED_FULL_RELICS


def test_passing_smite1_explicitly_is_the_same(frame):
    explicit = frame.copy()
    build_features.annotate(frame, ITEMS)
    build_features.annotate(explicit, ITEMS, build_features.SMITE1)
    pd.testing.assert_frame_equal(frame, explicit)


def test_build_hash_ignores_slot_order(frame):
    build_features.annotate(frame, ITEMS)
    assert frame["BuildHash"][0] == frame["BuildHash"][1]


def test_smite2_uses_one_relic_and_excludes_starters(frame):
    build_features.annotate(frame, ITEMS, build_features.SMITE2)

    # Row 2's starter no longer completes a build: in Smite 2 the starter has
    # its own column, so a starter in a core slot means a five-item build.
    assert bool(frame["IsFullBuild"][2]) is False
    assert bool(frame["IsFullBuild"][0]) is True

    # One relic column, so no comma and ActiveId2 is ignored entirely.
    assert frame["Relics"][0] == "200"

    # Row 6 is the shape difference made concrete. 12333 is Smite 1's "No
    # Relic" sentinel, so Smite 1 reads that row as having an unfilled relic
    # slot; Smite 2 has no such sentinel, so the same id is an ordinary relic.
    smite1 = pd.DataFrame(ROWS)
    build_features.annotate(smite1, ITEMS, build_features.SMITE1)
    assert bool(smite1["IsFullRelics"][6]) is False
    assert bool(frame["IsFullRelics"][6]) is True


def test_smite2_treats_zero_as_the_only_empty_relic():
    rows = pd.DataFrame(
        [
            dict(ItemId1=100, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
                 ItemId6=105, ActiveId1=0, ActiveId2=0),
            dict(ItemId1=100, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
                 ItemId6=105, ActiveId1=200, ActiveId2=0),
        ]
    )
    build_features.annotate(rows, ITEMS, build_features.SMITE2)
    assert [bool(v) for v in rows["IsFullRelics"]] == [False, True]
    # ActiveId2 is written as a constant 0 by the collector and must be ignored
    # rather than dragging the row down with it.
    assert bool(rows["IsFullBuild"][1]) is True


def test_smite2_build_hash_matches_smite1_for_the_same_six_items(frame):
    """The hash is over the item matrix alone, so it does not depend on which
    game's shape produced it. Corpora never mix, but the ids are disjoint by
    construction anyway."""
    one = frame.copy()
    two = frame.copy()
    build_features.annotate(one, ITEMS, build_features.SMITE1)
    build_features.annotate(two, ITEMS, build_features.SMITE2)
    assert one["BuildHash"][0] == two["BuildHash"][0]
