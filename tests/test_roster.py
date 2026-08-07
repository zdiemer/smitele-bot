"""Two rosters, because a tracker.gg identity does not follow from a Hi-Rez name.

Smite 1 keys on a Hi-Rez username. Smite 2 keys on `platform:handle`, and on
Steam the handle is a **SteamID64** — the 17-digit `7656119…`, not a vanity URL,
which tracker.gg will not resolve. Measured across the 40,859 players the
crawler has read: steam 22,677 (all numeric), psn 8,838, xbl 8,089, epic 1,240,
all display names. So the platform prefix is load-bearing, not decoration —
45% of that population is not on Steam.

The five user commands are context menus on a Discord member and cannot carry a
`game:` option, so the server's setting decides which map they read. Picking the
wrong one would silently look up a Hi-Rez name on tracker.gg.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "HirezAPI"))

roster = pytest.importorskip("roster")
from game import Game  # noqa: E402


class TestForGame:
    def test_each_game_gets_its_own_map(self):
        assert roster.for_game(Game.SMITE) is roster.DISCORD_TO_SMITE
        assert roster.for_game(Game.SMITE_2) is roster.DISCORD_TO_SMITE2

    def test_the_two_maps_are_not_the_same_object(self):
        # Sharing one dict would mean adding a Smite 2 id also answered Smite 1
        # lookups with a `platform:handle` string the Hi-Rez API cannot use.
        assert roster.DISCORD_TO_SMITE is not roster.DISCORD_TO_SMITE2

    def test_it_takes_the_enum_not_a_string(self):
        # A string would let "smite2" fall through to the Smite 1 roster, which
        # fails as a silent wrong answer rather than a loud one.
        assert roster.for_game("smite2") is roster.DISCORD_TO_SMITE


class TestSmite2Handles:
    """Shape rules for entries as they get added. Vacuous while the map is
    empty, which is the point — they start guarding the moment it isn't."""

    def test_every_entry_names_a_platform(self):
        for discord_id, handle in roster.DISCORD_TO_SMITE2.items():
            assert ":" in handle, (
                f"{discord_id} -> {handle!r} has no platform; "
                "parse_player would guess steam and be wrong for console players"
            )

    def test_every_platform_is_one_tracker_knows(self):
        platforms = pytest.importorskip("smite2.players").PLATFORMS

        for handle in roster.DISCORD_TO_SMITE2.values():
            platform = handle.partition(":")[0]
            assert platform in platforms, f"{platform!r} is not a tracker.gg platform"

    def test_steam_handles_are_steamid64_not_vanity_urls(self):
        for discord_id, handle in roster.DISCORD_TO_SMITE2.items():
            platform, _, value = handle.partition(":")
            if platform != "steam":
                continue
            assert re.fullmatch(r"7656119\d{10}", value), (
                f"{discord_id} -> {value!r} is not a SteamID64. tracker.gg keys "
                "Steam on the 17-digit id and will not resolve a vanity name."
            )

    def test_entries_round_trip_through_the_parser(self):
        parse_player = pytest.importorskip("smite2.players").parse_player

        for handle in roster.DISCORD_TO_SMITE2.values():
            platform, name = parse_player(handle)
            assert platform and name
            assert handle == f"{platform}:{name}"


class TestPublicViews:
    def test_neither_public_tuple_carries_a_discord_id(self):
        for value in roster.SMITE_USERNAMES + roster.SMITE2_PLAYERS:
            assert not value.isdigit(), "a bare numeric handle could be a Discord id"

    def test_both_views_cover_their_map(self):
        assert len(roster.SMITE_USERNAMES) == len(roster.DISCORD_TO_SMITE)
        assert len(roster.SMITE2_PLAYERS) == len(roster.DISCORD_TO_SMITE2)

    def test_smite2_view_sorts_on_the_handle_not_the_platform(self):
        # Otherwise the roster groups by platform, which is an implementation
        # detail nobody reading a player list cares about.
        assert list(roster.SMITE2_PLAYERS) == sorted(
            roster.DISCORD_TO_SMITE2.values(),
            key=lambda value: value.partition(":")[2].lower(),
        )
