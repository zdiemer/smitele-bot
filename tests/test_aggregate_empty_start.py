"""What pass 1 does before it has counted anything.

A rebuild starts from no stored total, and the corpus it walks is not obliged to
produce a build early. The oldest days here are Smite 2's alpha — a few dozen
rows apiece — and a run of them can pass without yielding a single full build.
Smite 2 reaches that state routinely rather than exceptionally, because its
crawl rewrites days it has already counted and so forces a rebuild most nights.

It lost all three attempts to `KeyError: 'BuildHash'` on 2026-08-09, before a
single progress line was printed.
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
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "match_data_collector"
    ),
)

pd = pytest.importorskip("pandas")
build_aggregate = pytest.importorskip("build_aggregate")


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
    200: item(0),
    201: item(0),
}

FULL_BUILD = dict(
    GodId=1, ItemId1=100, ItemId2=101, ItemId3=102, ItemId4=103, ItemId5=104,
    ItemId6=105, ActiveId1=200, ActiveId2=201,
)
# An empty sixth slot: a real row, but not one pass 1 can count.
NO_BUILD = {**FULL_BUILD, "ItemId6": 0}


def write(directory, name: str, row: dict, rows: int = 4) -> str:
    path = os.path.join(str(directory), name)
    pd.DataFrame([row] * rows).to_parquet(path, compression="zstd", index=False)
    return path


def count(corpus, every: int = 2, min_plays: int = 1):
    """Pass 1 itself, with the Smite 1 shape and no stored total — a rebuild."""
    return build_aggregate.count_build_plays(
        corpus, ITEMS, min_plays, every, previous=None, config=None
    )


@pytest.fixture
def barren_then_fertile(tmp_path):
    """Two files with nothing countable, then two with a build.

    Ordered this way because pass 1 folds every `every` files, so the first fold
    lands while nothing has been counted.
    """
    return [
        write(tmp_path, "match_details_2024-08-27.parquet", NO_BUILD),
        write(tmp_path, "match_details_2024-08-28.parquet", NO_BUILD),
        write(tmp_path, "match_details_2024-08-29.parquet", FULL_BUILD),
        write(tmp_path, "match_details_2024-08-30.parquet", FULL_BUILD),
    ]


@pytest.fixture
def barren(tmp_path):
    """A corpus with no countable build anywhere — a crawl that has so far
    collected only partial matches."""
    return [
        write(tmp_path, "match_details_2024-08-27.parquet", NO_BUILD),
        write(tmp_path, "match_details_2024-08-28.parquet", NO_BUILD),
    ]


class TestARebuildThatFindsNothingEarly:
    def test_a_barren_first_batch_does_not_kill_the_run(self, barren_then_fertile):
        """The regression: the first fold happens with nothing counted."""
        keep, total = count(barren_then_fertile)

        assert len(keep) == 1, "the one build the later files share"
        assert int(total["plays"].sum()) == 8, "four rows in each fertile file"

    def test_a_corpus_with_no_builds_at_all_yields_nothing(self, barren):
        """The degenerate end of the same case. It must return empty rather than
        raise, so a crawl holding only partial matches still completes."""
        keep, total = count(barren)

        assert keep.size == 0
        assert total.empty
        assert list(total.columns) == ["BuildHash", "plays"]

    def test_a_bare_starting_total_is_what_used_to_break(self, barren, monkeypatch):
        """Pins the fix as load-bearing: restore the old starting value and the
        same corpus fails again. `fold` now also drops empties, so what surfaces
        is the filter at the end of pass 1 rather than the concat itself."""
        monkeypatch.setattr(build_aggregate, "empty_build_plays", pd.DataFrame)

        with pytest.raises(KeyError):
            count(barren)

    def test_the_hash_stays_wide_enough_to_hold_a_build(self, barren_then_fertile):
        """BuildHash routinely exceeds int64, which is why build_features types
        it UInt64. A starting frame typed narrower would downcast the first fold
        that touched it."""
        _, total = count(barren_then_fertile)

        assert total["BuildHash"].dtype == "UInt64"

    def test_each_call_gets_its_own_frame(self):
        """Two runs in one process must not share a total — the Smite 1 and
        Smite 2 aggregates both call this."""
        first = build_aggregate.empty_build_plays()
        first.loc[0] = (1, 1)

        assert build_aggregate.empty_build_plays().empty
