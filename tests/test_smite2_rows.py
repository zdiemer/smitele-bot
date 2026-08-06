"""Mapping a tracker.gg player row into a corpus record.

This is the most dangerous code in the Smite 2 work, because every way of
getting it wrong produces a plausible build rather than an error. The item
layout in particular is *almost* positional, and the exceptions are common
enough to matter: measured over 26,444 real player rows there were 2,079
talents sitting at positions 3-8, 1,530 unnameable entries among them, and 156
relics at position 1.
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

import rows  # noqa: E402
from smite2.ids import item_id  # noqa: E402
from smite2.queues import Smite2QueueId  # noqa: E402


def entry(position, kind, slug):
    return {"position": position, "equipmentType": kind, "id": slug, "name": slug}


def match(items, *, god="anubis", team="order", winner="order", mode="conquest",
          ranked=False, rating=None, party=None):
    stats = {"kills": {"value": 5}, "deaths": {"value": 2}, "assists": {"value": 7},
             "damage": {"value": 20000}}
    if rating is not None:
        stats["skillRating"] = {"value": rating}
    return {
        "attributes": {"id": "match-1", "gamemode": mode},
        "metadata": {
            "timestamp": "2026-08-06T13:39:00+00:00",
            "winningTeamId": winner,
            "isRanked": ranked,
        },
        "segments": [
            {
                "type": "overview",
                "attributes": {"platformSlug": "steam", "platformUserIdentifier": "1"},
                "metadata": {
                    "teamId": team,
                    "god": god,
                    "items": items,
                    "playedRole": {"key": "mid"},
                    "partyId": party,
                },
                "stats": stats,
            }
        ],
    }


GODS = {"anubis": 12345}


def only_row(m):
    got = list(rows.player_rows(m, GODS))
    assert len(got) == 1
    return got[0]


class TestSlotting:
    def test_the_ordinary_layout(self):
        row = only_row(match([
            entry(1, "starter", "sands-of-time"),
            entry(2, "relic", "purification-beads"),
            *[entry(p, "item-passive", f"item-{p}") for p in range(3, 9)],
        ]))
        assert row["StarterId"] == item_id("sands-of-time")
        assert row["ActiveId1"] == item_id("purification-beads")
        assert [row[f"ItemId{i}"] for i in range(1, 7)] == [
            item_id(f"item-{p}") for p in range(3, 9)
        ]

    def test_a_talent_at_a_core_position_is_not_an_item(self):
        """The failure this whole module is arranged to avoid. A talent sits at
        position 6 here; reading positions 3-8 as the six slots would write an
        Aspect into a build slot and the build would still look complete."""
        row = only_row(match([
            entry(1, "starter", "sands-of-time"),
            entry(2, "relic", "beads"),
            entry(3, "item-passive", "a"),
            entry(4, "item-passive", "b"),
            entry(5, "item-passive", "c"),
            entry(6, "talent", "aspect-of-ruin"),
            entry(7, "item-passive", "d"),
            entry(8, "item-passive", "e"),
        ]))
        core = [row[f"ItemId{i}"] for i in range(1, 7)]
        assert item_id("aspect-of-ruin") not in core
        assert row["Aspect"] == item_id("aspect-of-ruin")
        # Five real items and an empty slot, which is the truth.
        assert core == [item_id(x) for x in ("a", "b", "c", "d", "e")] + [0]

    def test_a_relic_at_position_one_is_still_a_relic(self):
        row = only_row(match([
            entry(1, "relic", "beads"),
            entry(2, "starter", "sands-of-time"),
            entry(3, "item-passive", "a"),
        ]))
        assert row["ActiveId1"] == item_id("beads")
        assert row["StarterId"] == item_id("sands-of-time")

    def test_non_contiguous_positions_do_not_shift_items(self):
        """A gap means an unfilled slot, not a missing entry to close over."""
        row = only_row(match([
            entry(1, "starter", "s"),
            entry(2, "relic", "r"),
            entry(3, "item-passive", "a"),
            entry(4, "item-passive", "b"),
            entry(6, "item-passive", "c"),
            entry(7, "item-passive", "d"),
        ]))
        assert [row[f"ItemId{i}"] for i in range(1, 7)] == [
            item_id("a"), item_id("b"), item_id("c"), item_id("d"), 0, 0
        ]

    def test_unnameable_entries_become_empty_and_are_counted(self):
        """An item tracker.gg could not name is a hex id. Zero fails
        IsFullBuild, which is right — a build we cannot name is not rankable."""
        row = only_row(match([
            entry(1, "starter", "s"),
            entry(3, "item-passive", "a"),
            entry(4, "unknown", "00000000000000000000000000002D71"),
        ]))
        assert row["UnknownItems"] == 1
        assert [row[f"ItemId{i}"] for i in range(1, 7)] == [item_id("a"), 0, 0, 0, 0, 0]

    def test_item_active_counts_as_a_core_item(self):
        row = only_row(match([entry(3, "item-active", "a")]))
        assert row["ItemId1"] == item_id("a")

    def test_more_than_six_core_items_is_truncated_not_wrapped(self):
        row = only_row(match(
            [entry(p, "item-passive", f"i{p}") for p in range(3, 12)]
        ))
        assert [row[f"ItemId{i}"] for i in range(1, 7)] == [
            item_id(f"i{p}") for p in range(3, 9)
        ]

    def test_an_empty_item_list(self):
        row = only_row(match([]))
        assert [row[f"ItemId{i}"] for i in range(1, 7)] == [0] * 6
        assert row["StarterId"] == 0 and row["ActiveId1"] == 0


class TestColumnContract:
    def test_win_status_is_the_literal_string_prepare_compares_against(self):
        """`prepare` does `frame["Win_Status"] == "Winner"`. A boolean here
        would make every row a loss."""
        assert only_row(match([], team="order", winner="order"))["Win_Status"] == "Winner"
        assert only_row(match([], team="chaos", winner="order"))["Win_Status"] == "Loser"

    def test_task_force_matches_the_win(self):
        row = only_row(match([], team="order", winner="chaos"))
        assert row["TaskForce"] == 1
        assert row["Winning_TaskForce"] == 2

    def test_active_id_two_is_written_as_zero_not_omitted(self):
        """src/ml maps 0 to a masked vocabulary index, so writing the column
        keeps the model working unchanged. Omitting it would drop the row."""
        assert only_row(match([]))["ActiveId2"] == 0

    def test_an_unnameable_god_is_zero_so_prepare_drops_the_row(self):
        assert only_row(match([], god="not-a-god"))["GodId"] == 0

    def test_roles_map_onto_the_aggregates_categories(self):
        assert only_row(match([]))["Role"] == "Mid"

    def test_an_unknown_role_is_not_invented(self):
        m = match([])
        m["segments"][0]["metadata"]["playedRole"] = {"key": "ENone"}
        assert only_row(m)["Role"] == "Unknown"

    def test_skill_rating_only_lands_on_ranked_rows(self):
        ranked = only_row(match([], mode="conquest-ranked", ranked=True, rating=655))
        casual = only_row(match([], mode="conquest", rating=655))
        assert ranked["Rank_Stat_Conquest"] == 655.0
        assert casual["Rank_Stat_Conquest"] == 0.0

    def test_every_column_the_aggregate_reads_is_present(self):
        row = only_row(match([]))
        needed = [
            "GodId", "Role", "Win_Status", "match_queue_id",
            "Rank_Stat_Conquest", "Rank_Stat_Duel", "Rank_Stat_Joust",
            "Conquest_Tier", "Duel_Tier", "Joust_Tier",
            "Kills_Player", "Deaths", "Assists", "Damage_Player",
            "Match", "TaskForce", "Winning_TaskForce",
            "Account_Level", "Mastery_Level",
            *[f"ItemId{i}" for i in range(1, 7)],
            "ActiveId1", "ActiveId2",
        ]
        assert [c for c in needed if c not in row] == []


class TestModes:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            ("conquest", Smite2QueueId.CONQUEST),
            ("conquest-ranked", Smite2QueueId.RANKED_CONQUEST),
            ("assault", Smite2QueueId.ASSAULT),
            ("joust-bots", Smite2QueueId.JOUST_BOTS),
            ("something-new", Smite2QueueId.UNKNOWN),
        ],
    )
    def test_observed_modes_map(self, mode, expected):
        assert only_row(match([], mode=mode))["match_queue_id"] == expected.value

    def test_queue_ids_cannot_be_mistaken_for_smite1(self):
        from HirezAPI import QueueId

        assert min(q.value for q in Smite2QueueId) > max(q.value for q in QueueId)


class TestDates:
    def test_rows_are_filed_by_when_the_match_was_played(self):
        """Not by when it was collected. A page spans about three days, so
        filing by collection date would discard two thirds of a night's work."""
        assert rows.match_date(match([])) == "2026-08-06"

    def test_a_match_without_a_timestamp_is_skipped(self):
        m = match([])
        m["metadata"]["timestamp"] = ""
        assert rows.match_date(m) is None
