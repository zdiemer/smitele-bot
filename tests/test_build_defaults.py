"""What `/build` answers when you tell it only which god.

Unspecified dimensions are summed rather than filtered, which is what "any
mode" and "any role" mean — and it made the default invocation answer a
question nobody asked. With no `match_queue` and no `role`, Conquest, Arena,
Joust, Assault and Duel pooled into one ranking along with every lane, and the
description named neither: seventy-five of eighty-eight Smite 2 gods came back
with a build sharing no items with the Conquest answer for the same god, an
Aphrodite who asked for a build got the Arena one, and nothing on screen said
so.

These pin the two halves of the fix — the mode defaults to Conquest, the lane
defaults to where the god is actually played — and the escape hatch that keeps
the old behaviour reachable for anyone who wanted it.
"""

from __future__ import annotations

import types

import pytest

pd = pytest.importorskip("pandas")
smitele_bot = pytest.importorskip("smitele_bot")
build_ranker = pytest.importorskip("build_ranker")

from smite2.queues import Smite2QueueId  # noqa: E402


def god_rows(*rows):
    """`god_stats` rows: (queue, role, plays)."""
    return pd.DataFrame(
        [
            {
                "GodId": 7,
                "match_queue_id": queue,
                "Role": role,
                "HighMmr": False,
                "plays": plays,
                "wins": plays // 2,
                "wplays": float(plays),
                "wwins": float(plays // 2),
            }
            for queue, role, plays in rows
        ]
    )


def stats_with(gods: "pd.DataFrame") -> "build_ranker.BuildStats":
    empty = pd.DataFrame(columns=["BuildHash"])
    return build_ranker.BuildStats(empty, empty, pd.DataFrame(), gods)


def provider_with(gods):
    return types.SimpleNamespace(build_stats=stats_with(gods))


def options(queue):
    return types.SimpleNamespace(god_id=7, queue_id=queue)


class TestTheDefaultLane:
    def test_it_is_the_lane_the_god_is_played_in(self):
        provider = provider_with(
            god_rows(
                (Smite2QueueId.CONQUEST.value, "Support", 900),
                (Smite2QueueId.CONQUEST.value, "Mid", 100),
            )
        )
        assert (
            smitele_bot.common_lane(provider, options(Smite2QueueId.CONQUEST))
            == "support"
        )

    def test_other_modes_do_not_get_a_vote(self):
        """Arena and Assault have no lanes, and tracker.gg labels one anyway.

        An "Arena support" is a full damage build, so counting those rows makes
        a support come back a carry — a lane the god is never played in.
        """
        provider = provider_with(
            god_rows(
                (Smite2QueueId.CONQUEST.value, "Support", 200),
                (Smite2QueueId.ARENA.value, "Carry", 5000),
            )
        )
        assert (
            smitele_bot.common_lane(provider, options(Smite2QueueId.CONQUEST))
            == "support"
        )

    def test_a_mode_without_lanes_gets_no_lane(self):
        provider = provider_with(
            god_rows((Smite2QueueId.ARENA.value, "Carry", 5000))
        )
        assert smitele_bot.common_lane(provider, options(Smite2QueueId.ARENA)) == ""

    def test_no_mode_at_all_gets_no_lane(self):
        provider = provider_with(
            god_rows((Smite2QueueId.CONQUEST.value, "Support", 900))
        )
        assert smitele_bot.common_lane(provider, options(None)) == ""

    def test_no_aggregate_is_not_an_error(self):
        """The bot can start before the first aggregate run; the request then
        goes out without a role, exactly as it used to."""
        provider = types.SimpleNamespace(build_stats=None)
        assert (
            smitele_bot.common_lane(provider, options(Smite2QueueId.CONQUEST)) == ""
        )

    def test_a_god_the_corpus_has_never_seen_gets_no_lane(self):
        provider = provider_with(god_rows())
        assert (
            smitele_bot.common_lane(provider, options(Smite2QueueId.CONQUEST)) == ""
        )


class TestTheModeChoices:
    def test_every_mode_is_still_reachable(self):
        """The old behaviour is a choice now rather than the default, and it
        has to actually be in the list for that to be true."""
        assert smitele_bot.ALL_MODES in smitele_bot.queue_choices()

    def test_it_is_offered_first(self):
        assert smitele_bot.queue_choices()[0] == smitele_bot.ALL_MODES

    def test_the_real_modes_are_still_there(self):
        choices = smitele_bot.queue_choices()
        assert "Conquest" in choices
        assert Smite2QueueId.RANKED_CONQUEST.display_name in choices
