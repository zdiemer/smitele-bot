"""Smite 2 ids are derived by hash rather than assigned, and four processes on
three schedules have to agree on them without coordinating. A drift here does
not raise — it writes a corpus that reads back as unknown items.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from smite2.ids import (
    S2_ID_BASE,
    NameIndex,
    assert_no_collisions,
    god_id,
    item_id,
    join_keys,
    lookup_key,
    slugify,
    squash,
)


def test_ids_do_not_depend_on_the_hash_seed():
    """The builtin `hash()` is salted per process, so using it would give the
    bot and the collector different ids for the same item. This is the trap the
    blake2b derivation exists to avoid, so it is checked in a real subprocess
    with a different seed rather than asserted in a comment."""
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        from smite2.ids import god_id, item_id
        print(god_id("cu-chulainn"), item_id("book-of-thoth"))
        """
    )
    outputs = set()
    for seed in ("0", "1", "12345"):
        result = subprocess.run(
            [sys.executable, "-c", script, _src()],
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, f"ids differ across hash seeds: {outputs}"
    assert outputs.pop() == f"{god_id('cu-chulainn')} {item_id('book-of-thoth')}"


def _src() -> str:
    import os

    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "HirezAPI"
    )


def test_ids_sit_above_every_smite1_id():
    """Smite 1's largest id is a five-figure number. Keeping Smite 2 far above
    it means a frame that mixes games produces ids belonging to neither, which
    `build_features.annotate` already discards, instead of a wrong join."""
    from god_types import GodId

    assert max(g.value for g in GodId) < S2_ID_BASE
    assert god_id("anubis") >= S2_ID_BASE
    assert item_id("book-of-thoth") >= S2_ID_BASE


@pytest.mark.parametrize("slug", ["anubis", "cu-chulainn", "book-of-thoth", "zzz"])
def test_ids_fit_in_int32(slug):
    """`build_aggregate.prepare` stores GodId as int32 — it exists to fit a
    132-million-row corpus in memory. An id above 2^31-1 silently overflows
    there rather than raising, which would scramble every Smite 2 god into a
    negative number and aggregate them together."""
    import numpy as np

    for value in (god_id(slug), item_id(slug)):
        assert value <= np.iinfo(np.int32).max
        assert int(np.int32(value)) == value


def test_gods_and_items_do_not_share_an_id_space():
    assert god_id("anubis") != item_id("anubis")


def test_collisions_raise_rather_than_being_trusted():
    seen = assert_no_collisions("item", ["a", "b", "c"])
    assert len(seen) == 3


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Chronos' Pendant", "chronos-pendant"),
        ("Book of Thoth", "book-of-thoth"),
        ("Ah Puch", "ah-puch"),
        ("Bumba's Spear", "bumbas-spear"),
    ],
)
def test_slugify_matches_trackers_spelling(name, expected):
    assert slugify(name) == expected


class TestJoining:
    """The join was measured, not guessed: slug matching alone covered 96.09%
    of 26,444 observed god values, and these four rules take it to 100%."""

    @pytest.mark.parametrize(
        "tracker_value,wiki_name",
        [
            ("jingwei", "Jing Wei"),          # collapsed punctuation
            ("morrigan", "The Morrigan"),     # dropped article
            ("Gods.CuChulainn", "Cu Chulainn"),  # raw engine token
            ("xing-tian", "Xing Tian"),       # absent from Data:Gods.json
            ("anubis", "Anubis"),
        ],
    )
    def test_every_spelling_resolves(self, tracker_value, wiki_name):
        index = NameIndex()
        index.add(wiki_name)
        assert index.get(tracker_value) == wiki_name

    def test_the_engine_prefix_is_stripped_from_lookups(self):
        assert lookup_key("Gods.CuChulainn") == "cuchulainn"

    def test_an_article_is_optional_in_both_directions(self):
        assert "morrigan" in join_keys("The Morrigan")
        assert "themorrigan" in join_keys("The Morrigan")

    def test_an_unknown_name_is_none_rather_than_a_guess(self):
        index = NameIndex()
        index.add("Anubis")
        assert index.get("Ra") is None

    def test_the_first_writer_wins(self):
        """The authoritative page list is added before Data:Gods.json, so a stub
        record cannot displace it."""
        index = NameIndex()
        index.add("Xing Tian")
        index.add("Xing Tian (old)", "xingtian")
        assert index.get("xingtian") == "Xing Tian"

    def test_squash_ignores_case_and_punctuation(self):
        assert squash("Chang'e") == squash("CHANGE") == "change"
