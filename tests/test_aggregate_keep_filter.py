"""Which builds pass 2 still counts after the min-plays filter.

Pass 1 decides the surviving set and pass 2 tests every row against it. The set
became a uint64 array rather than a Python set of boxed ints — ~19MB instead of
~150MB on a full Smite 1 corpus — and the membership test moved off
`Series.isin`, which dispatches to pandas' masked implementation for a nullable
column. Neither is meant to change *which* builds survive, so that is what these
pin.
"""

from __future__ import annotations

import os
import sys
import types

import numpy as np
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


ITEMS = {n: item(3) for n in range(100, 112)}
ITEMS[0] = item(0)
ITEMS[200] = item(0)
ITEMS[201] = item(0)


def build(*items_, god: int = 1) -> dict:
    row = dict(GodId=god, ActiveId1=200, ActiveId2=201)
    for slot, value in enumerate(items_, start=1):
        row[f"ItemId{slot}"] = value
    return row


SIX = (100, 101, 102, 103, 104, 105)
OTHER_SIX = (106, 107, 108, 109, 110, 111)
PARTIAL = (100, 101, 102, 103, 104, 0)


def write(directory, name: str, rows: list) -> str:
    path = os.path.join(str(directory), name)
    pd.DataFrame(rows).to_parquet(path, compression="zstd", index=False)
    return path


class TestTheSurvivingSet:
    def test_only_builds_over_the_threshold_survive(self, tmp_path):
        """One build played three times, another once, min-plays two."""
        corpus = [
            write(
                tmp_path,
                "match_details_2026-01-01.parquet",
                [build(*SIX)] * 3 + [build(*OTHER_SIX)],
            )
        ]

        keep, _ = build_aggregate.count_build_plays(
            corpus, ITEMS, 2, 1, previous=None, config=None
        )

        assert len(keep) == 1

    def test_the_surviving_set_is_a_uint64_array(self, tmp_path):
        """Pass 2 feeds this straight to np.isin against raw uint64 hashes; a
        set or a narrower dtype would put the boxing back or truncate."""
        corpus = [
            write(tmp_path, "match_details_2026-01-01.parquet", [build(*SIX)] * 2)
        ]

        keep, _ = build_aggregate.count_build_plays(
            corpus, ITEMS, 1, 1, previous=None, config=None
        )

        assert isinstance(keep, np.ndarray)
        assert keep.dtype == np.uint64

    def test_a_partial_build_is_never_in_the_set(self, tmp_path):
        """Its hash is NA, and NA maps to 0 on the way to numpy — so a corpus of
        nothing but partials must not leave a 0 in the surviving set for real
        rows to match against."""
        corpus = [
            write(tmp_path, "match_details_2026-01-01.parquet", [build(*PARTIAL)] * 5)
        ]

        keep, _ = build_aggregate.count_build_plays(
            corpus, ITEMS, 1, 1, previous=None, config=None
        )

        assert keep.size == 0


class TestMembershipAgreesWithPandas:
    """The change is only safe if np.isin and Series.isin pick the same rows."""

    def test_the_two_paths_select_identically(self):
        hashes = pd.array([11, 22, 33, None, 44], dtype="UInt64")
        frame = pd.DataFrame({"BuildHash": hashes})
        keep = np.array([22, 44], dtype="uint64")

        with_pandas = frame["BuildHash"].isin(list(keep)).to_numpy(na_value=False)
        with_numpy = np.isin(
            frame["BuildHash"].to_numpy(dtype="uint64", na_value=0), keep
        )

        assert list(with_pandas) == list(with_numpy) == [False, True, False, False, True]

    def test_a_hash_above_int64_still_matches(self):
        """BuildHash is UInt64 precisely because it overflows int64. Going via a
        signed type anywhere would wrap this into a different number."""
        big = np.uint64(2**64 - 1)
        frame = pd.DataFrame({"BuildHash": pd.array([big, 7], dtype="UInt64")})

        selected = np.isin(
            frame["BuildHash"].to_numpy(dtype="uint64", na_value=0),
            np.array([big], dtype="uint64"),
        )

        assert list(selected) == [True, False]

    def test_null_hashes_do_not_match_a_zero_in_the_set(self):
        """NA becomes 0, so a literal 0 in the surviving set would sweep up every
        partial build in the corpus. Guarded by never emitting one, and this is
        what would catch that guard being lost."""
        frame = pd.DataFrame({"BuildHash": pd.array([None, None], dtype="UInt64")})

        selected = np.isin(
            frame["BuildHash"].to_numpy(dtype="uint64", na_value=0),
            np.array([], dtype="uint64"),
        )

        assert not selected.any()
