"""Which tier put a player on the roster, and why that must not be persisted.

`select` draws from three pools in order — never-visited, stale, written-off —
and until now the run record could say how many players were visited but not
which pool paid for them. Without that split there is no way to answer the one
question that decides the ordering: never-visited players are discovered from
the matches just read, so their pages should overlap what the corpus already
has, and if they yield worse than stale players the ordering is backwards.

The tag is deliberately not a column. It describes one night's selection, not a
standing fact about a player, and a persisted `tier` would invite reading last
night's reason for tonight's visit.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src",
        "smite2_collector",
    ),
)

pytest.importorskip("pandas", reason="pandas not installed")
frontier_module = pytest.importorskip("frontier", reason="collector deps not installed")


TODAY = "2026-08-07"
YESTERDAY = "2026-08-06"


def roster(tmp_path):
    return frontier_module.Frontier(str(tmp_path))


def visited(player, *, day, visits=1, fresh=0, barren=0):
    player.last_queried = day
    player.visits = visits
    player.new_matches_yielded = fresh
    player.barren_visits = barren
    return player


class TestTiers:
    def test_each_pool_labels_what_it_contributed(self, tmp_path):
        front = roster(tmp_path)
        front.add("steam", "never", TODAY)
        visited(front.add("steam", "stale", TODAY), day=YESTERDAY, fresh=4)
        visited(
            front.add("steam", "written-off", TODAY),
            day=YESTERDAY,
            barren=frontier_module.DEAD_AFTER_BARREN_VISITS,
        )

        chosen = front.select(10, TODAY)
        tiers = {p.handle: p.tier for p in chosen}
        assert tiers == {
            "never": "fresh",
            "stale": "stale",
            "written-off": "revivable",
        }

    def test_the_pools_are_drawn_in_order(self, tmp_path):
        front = roster(tmp_path)
        visited(front.add("steam", "stale", TODAY), day=YESTERDAY, fresh=9)
        front.add("steam", "never", TODAY)

        # Budget of one: the never-visited player is the snowball edge and must
        # win even against a stale player with a far better history.
        chosen = front.select(1, TODAY)
        assert [p.handle for p in chosen] == ["never"]
        assert chosen[0].tier == "fresh"

    def test_a_tier_is_not_a_column(self, tmp_path):
        front = roster(tmp_path)
        front.add("steam", "never", TODAY)
        assert front.select(10, TODAY)[0].tier == "fresh"

        front.save()
        assert front.select(10, TODAY)  # the tag survives in memory

        reloaded = roster(tmp_path)
        assert reloaded.players["steam:never"].tier == ""
        assert "tier" not in frontier_module.COLUMNS


class TestPartySuppression:
    def test_holding_a_premade_back_is_counted(self, tmp_path):
        front = roster(tmp_path)
        one = front.add("steam", "duo-a", TODAY)
        two = front.add("steam", "duo-b", TODAY)
        one.party_key = two.party_key = "steam:duo-a"

        chosen = front.select(10, TODAY)
        assert len(chosen) == 1
        assert front.suppressed == 1

    def test_the_count_resets_between_selections(self, tmp_path):
        front = roster(tmp_path)
        one = front.add("steam", "duo-a", TODAY)
        two = front.add("steam", "duo-b", TODAY)
        one.party_key = two.party_key = "steam:duo-a"

        front.select(10, TODAY)
        front.select(10, TODAY)
        # Not two: a stale counter would read as the suppression doubling every
        # time the roster refills mid-run.
        assert front.suppressed == 1

    def test_a_solo_player_is_never_suppressed(self, tmp_path):
        front = roster(tmp_path)
        front.add("steam", "solo-a", TODAY)
        front.add("steam", "solo-b", TODAY)

        assert len(front.select(10, TODAY)) == 2
        assert front.suppressed == 0
