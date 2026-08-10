"""Pass 1 of the aggregate, over days that contain no complete builds.

This is the shape that broke the nightly Smite 2 run: the corpus is walked
oldest first, and the oldest days are the game's alpha — a few dozen rows each,
none of them a finished build. A whole fold window went by with nothing to fold
and no stored total to fold it into, and the running total was a bare frame with
no column to group on, so the job died on `KeyError: 'BuildHash'` before it had
looked at a single day that mattered.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "HirezAPI"),
)

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")
build_aggregate = pytest.importorskip("build_aggregate")
build_features = pytest.importorskip("build_features")


def item(tier: int, is_starter: bool = False):
    return types.SimpleNamespace(tier=tier, is_starter=is_starter)


ITEMS = {
    0: item(0),
    100: item(3),
    101: item(3),
    102: item(3),
    103: item(3),
    104: item(3),
    105: item(3),
    107: item(2),  # real, but does not complete a build
    200: item(0),  # relics
    201: item(0),
}

FULL_BUILD = dict(
    ItemId1=100, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104, ItemId6=105
)
# One tier-2 item is enough to disqualify the row.
PARTIAL_BUILD = dict(FULL_BUILD, ItemId1=107)


def write_day(directory, name: str, build: dict, rows: int) -> str:
    """A corpus file of `rows` identical matches."""
    frame = pd.DataFrame(
        [dict(build, GodId=1, ActiveId1=200, ActiveId2=201)] * rows
    )
    path = os.path.join(str(directory), f"match_details_{name}.parquet")
    frame.to_parquet(path, index=False)
    return path


def count(corpus, min_plays=2, every=2, previous=None):
    return build_aggregate.count_build_plays(corpus, ITEMS, min_plays, every, previous)


def test_a_fold_window_with_no_complete_builds_is_survivable(tmp_path):
    """The original failure: nothing to fold, and nothing folded yet either."""
    corpus = [
        write_day(tmp_path, "2024-08-28", PARTIAL_BUILD, 30),
        write_day(tmp_path, "2024-08-29", PARTIAL_BUILD, 40),
        write_day(tmp_path, "2026-08-08", FULL_BUILD, 5),
        write_day(tmp_path, "2026-08-09", FULL_BUILD, 5),
    ]

    keep, total = count(corpus)

    # The empty days cost nothing and the real ones still land.
    assert total.shape[0] == 1
    assert total["plays"].tolist() == [10]
    assert len(keep) == 1


def test_a_corpus_with_no_complete_builds_at_all_yields_nothing(tmp_path):
    """Not an error — there is simply no build worth keeping yet."""
    corpus = [
        write_day(tmp_path, "2024-08-28", PARTIAL_BUILD, 30),
        write_day(tmp_path, "2024-08-29", PARTIAL_BUILD, 40),
    ]

    keep, total = count(corpus)

    assert keep.size == 0
    assert total.empty
    # The schema survives an empty run, because the next incremental pass folds
    # into this frame rather than starting over.
    assert list(total.columns) == ["BuildHash", "plays"]


def test_empty_days_do_not_disturb_a_stored_total(tmp_path):
    """The incremental path: a quiet night must not drop what came before."""
    corpus = [write_day(tmp_path, "2024-08-28", PARTIAL_BUILD, 30)]
    previous = count([write_day(tmp_path, "2026-08-09", FULL_BUILD, 5)])[1]

    keep, total = count(corpus, previous=previous)

    assert total["plays"].tolist() == [5]
    assert len(keep) == 1


def test_consolidate_ignores_the_empty_starting_total():
    """Pass 2 folds the same way, from the same bare frame."""
    counted = pd.DataFrame(
        {
            "GodId": [1],
            "BuildHash": pd.array([123], dtype="UInt64"),
            "plays": [4],
            "wins": [2],
            "wplays": [4.0],
            "wwins": [2.0],
        }
    )

    merged = build_aggregate.consolidate(
        [counted, pd.DataFrame()], ["GodId", "BuildHash"]
    )

    assert merged["plays"].tolist() == [4]
