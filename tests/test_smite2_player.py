"""A Smite 2 player, at the same depth as a Smite 1 one.

Two requests, not three: the profile response already carries every gamemode
segment, so asking `/profile` and then `/segments/gamemode` fetches identical
rows twice. That is not a micro-optimisation — tracker.gg refused this address
after ~300 requests in a single run, and every request the six-hourly player
refresh spends is one the nightly crawl does not get.

The identity half only exists here. Hi-Rez has no usable avatar: `Avatar_URL` is
a vestige of the old web profile, thirteen of fourteen are empty, and the one
populated value 403s on its CDN. tracker.gg returns a Steam display name and a
live steamstatic avatar, which is why Smite 2 players get a face and a name
where Smite 1 players get a god icon.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "HirezAPI"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "match_data_collector"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "web"))

pytest.importorskip("pandas")
snapshot = pytest.importorskip("snapshot")
players_module = pytest.importorskip("smite2.players")

Segment = players_module.Segment


def segment(name, matches, wins, *, kills=0, deaths=0, assists=0, rating=None, **extra):
    # tracker.gg's own key names — `matchesWon`, not `wins`. Modelling them
    # exactly is the point of the fake: an invented schema would have passed
    # here and returned zeroes against the real API.
    stats = {
        "matchesPlayed": matches,
        "matchesWon": wins,
        "matchesLost": matches - wins,
        "matchesWinPct": (wins / matches * 100) if matches else 0.0,
        "kdaRatio": (kills + assists / 2) / (deaths or 1),
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        **extra,
    }
    if rating is not None:
        stats["skillRating"] = rating
        stats["peakSkillRating"] = rating + 100
    return Segment(key=name.lower(), name=name, image_url=None, stats=stats)


class FakeLookups:
    def __init__(self, info, modes, gods=(), gods_raise=False):
        self.info, self.modes, self.gods = info, modes, list(gods)
        self.gods_raise = gods_raise
        self.calls = []

    async def overview(self, platform, handle):
        self.calls.append(("overview", platform, handle))
        return (self.info, self.modes)

    async def segments(self, platform, handle, kind):
        self.calls.append(("segments", kind))
        if self.gods_raise:
            raise RuntimeError("tracker refused")
        return self.gods


class FakeProvider:
    def __init__(self, lookups):
        self.players = lookups


INFO = {
    "platformUserHandle": "Zachjak",
    "platformUserIdentifier": "76561198047678579",
    "avatarUrl": "https://avatars.steamstatic.com/abc_full.jpg",
}


def build(**kwargs):
    lookups = FakeLookups(**kwargs)
    return asyncio.run(
        snapshot.smite2_player(FakeProvider(lookups), "steam:76561198047678579")
    ), lookups


class TestIdentity:
    def test_the_steam_name_and_avatar_come_through(self):
        doc, _ = build(info=INFO, modes=[segment("Conquest", 10, 6)])

        # The only place a Smite 2 player has a readable name or a picture.
        assert doc["name"] == "Zachjak"
        assert doc["avatar_url"] == "https://avatars.steamstatic.com/abc_full.jpg"
        assert doc["handle"] == "76561198047678579"
        assert doc["platform"] == "steam"

    def test_a_nameless_profile_falls_back_to_the_id(self):
        doc, _ = build(info={}, modes=[segment("Conquest", 10, 6)])

        assert doc["name"] == "76561198047678579"
        assert doc["avatar_url"] is None


class TestItCostsTwoRequests:
    def test_gamemode_segments_are_not_fetched_twice(self):
        _, lookups = build(info=INFO, modes=[segment("Conquest", 10, 6)])

        kinds = [call for call in lookups.calls if call[0] == "segments"]
        assert [k[1] for k in kinds] == ["god"], (
            "the profile already carries gamemode segments; re-fetching them "
            "spends a request the crawl needs"
        )
        assert len(lookups.calls) == 2


class TestTotals:
    def test_totals_are_summed_across_modes(self):
        doc, _ = build(
            info=INFO,
            modes=[
                segment("Conquest", 100, 60, kills=500, deaths=400, assists=800),
                segment("Arena", 50, 20, kills=300, deaths=250, assists=400),
            ],
        )

        assert doc["matches"] == 150
        assert doc["wins"] == 80
        assert doc["losses"] == 70
        assert doc["win_percent"] == pytest.approx(80 / 150, abs=0.001)
        assert doc["kills"] == 800
        # (kills + assists/2) / deaths — the same formula the Smite 1 page uses,
        # so the two are comparable rather than merely similar.
        assert doc["kda"] == pytest.approx((800 + 1200 / 2) / 650, abs=0.01)

    def test_modes_with_no_matches_are_dropped(self):
        doc, _ = build(
            info=INFO,
            modes=[segment("Conquest", 10, 5), segment("Slash", 0, 0)],
        )

        assert [m["name"] for m in doc["modes"]] == ["Conquest"]

    def test_a_player_with_no_matches_does_not_divide_by_zero(self):
        doc, _ = build(info=INFO, modes=[segment("Conquest", 0, 0)])

        assert doc["matches"] == 0
        assert doc["win_percent"] is None
        assert doc["best_mode"] is None


class TestBestMode:
    def test_it_needs_a_real_sample(self):
        doc, _ = build(
            info=INFO,
            modes=[
                segment("Duel", 3, 3),        # 100%, three games
                segment("Conquest", 100, 60),  # 60%, a hundred
            ],
        )

        # Same ten-match floor the Smite 1 "best queue" uses, for the same
        # reason: a perfect record over three games is true and worthless.
        assert doc["best_mode"]["name"] == "Conquest"

    def test_rating_comes_from_the_highest_ranked_mode(self):
        doc, _ = build(
            info=INFO,
            modes=[
                segment("Ranked Conquest", 50, 30, rating=1800),
                segment("Ranked Joust", 20, 12, rating=2400),
                segment("Arena", 90, 45),
            ],
        )

        assert doc["skill_rating"] == 2400
        assert doc["peak_skill_rating"] == 2500

    def test_no_ranked_play_is_null_not_zero(self):
        doc, _ = build(info=INFO, modes=[segment("Arena", 90, 45)])

        # Zero would read as "rated at nothing" rather than "never rated" —
        # which is the whole roster's actual state today.
        assert doc["skill_rating"] is None
        assert doc["peak_skill_rating"] is None


class TestGods:
    def test_top_gods_are_ranked_and_capped(self):
        gods = [segment(f"God{n}", n * 10, n * 5) for n in range(1, 15)]
        doc, _ = build(info=INFO, modes=[segment("Conquest", 10, 5)], gods=gods)

        assert len(doc["top_gods"]) == 10
        assert doc["top_gods"][0]["god"] == "God14"

    def test_a_failed_god_lookup_costs_one_panel_not_the_player(self):
        doc, _ = build(
            info=INFO, modes=[segment("Conquest", 10, 6)], gods_raise=True
        )

        assert doc["found"] is True
        assert doc["matches"] == 10
        assert doc["top_gods"] == []
