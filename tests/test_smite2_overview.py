"""A profile's segments are three kinds in one list, and only one is wanted.

Measured on a real account: 9 `god`, 4 `gamemode`, 3 `role` — sixteen rows in a
flat array distinguished only by a `type` field. Taking them all sums a player's
matches roughly twice and lets "best mode" come back as *Jungle*, which is a
lane rather than a mode. Both happened before this filter existed.

The god rows in a profile are also only a recent slice — one or two matches
each — where `segments(kind="god")` returns the full history. So they are not
merely redundant here; using them would understate every god.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "HirezAPI"))

players_module = pytest.importorskip("smite2.players")


def row(kind: str, name: str, matches: int) -> dict:
    return {
        "type": kind,
        "attributes": {"gamemode": name.lower(), "god": None, "season": None},
        "metadata": {"name": name},
        "stats": {"matchesPlayed": {"value": matches, "displayValue": str(matches)}},
    }


# The exact shape observed on a live profile.
PROFILE = {
    "platformInfo": {
        "platformUserHandle": "Zachjak",
        "avatarUrl": "https://avatars.steamstatic.com/abc_full.jpg",
    },
    "segments": [
        row("gamemode", "Casual Conquest", 10),
        row("gamemode", "Arena", 8),
        row("gamemode", "Assault", 118),
        row("gamemode", "Joust", 6),
        row("god", "Fenrir", 1),
        row("god", "Kukulkan", 1),
        row("role", "Support", 5),
        row("role", "Middle", 4),
        row("role", "Carry", 1),
    ],
}


class Lookups(players_module.PlayerLookups):
    def __init__(self, payload):
        self.payload = payload

    async def profile(self, platform, handle):  # noqa: D102
        return self.payload


def overview(payload):
    return asyncio.run(Lookups(payload).overview("steam", "76561198047678579"))


class TestOverview:
    def test_only_gamemode_segments_come_back(self):
        _, segments = overview(PROFILE)

        assert [s.name for s in segments] == [
            "Casual Conquest",
            "Arena",
            "Assault",
            "Joust",
        ]

    def test_totals_are_not_double_counted(self):
        _, segments = overview(PROFILE)

        # 10 + 8 + 118 + 6. Unfiltered this summed to 162 against a real
        # account whose true total is 142.
        assert sum(s.matches for s in segments) == 142

    def test_a_lane_cannot_be_mistaken_for_a_mode(self):
        _, segments = overview(PROFILE)
        names = {s.name for s in segments}

        for lane in ("Support", "Middle", "Carry", "Jungle", "Solo"):
            assert lane not in names

    def test_identity_comes_through(self):
        info, _ = overview(PROFILE)

        assert info["platformUserHandle"] == "Zachjak"
        assert info["avatarUrl"].startswith("https://")

    def test_an_empty_profile_is_none_not_a_crash(self):
        assert overview({}) is None
        assert overview(None) is None

    def test_a_profile_with_no_segments_is_an_empty_list(self):
        info, segments = overview({"platformInfo": {"platformUserHandle": "x"}})

        assert info["platformUserHandle"] == "x"
        assert segments == []
