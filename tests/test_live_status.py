"""The `/livematch` "no lobby" answer, decided from the fast signals.

This is where being wrong is quiet in the way the repo cares about: report a
friend as not playing while they are mid-match, or claim someone is in a game
who quit an hour ago. The ordering — RallyHere in-match, then online, then
Steam, then nothing — is what these pin, plus the two traps: a stale presence
`state` beside `online=False`, and skipping the Steam call once RallyHere has
already answered.
"""

from __future__ import annotations

import pytest

pytest.importorskip("aiohttp", reason="rallyhere imports aiohttp")

from smite2 import live_status  # noqa: E402
from smite2.rallyhere import PlayerStatus, SessionRef  # noqa: E402

NAME = "vinnied"


def status(online=False, state=None, sessions=()):
    return PlayerStatus(
        uuid="u", online=online, state=state, sessions=list(sessions)
    )


class TestAbsenceMessage:
    def test_rallyhere_in_a_match_beats_everything(self):
        stat = status(online=True, sessions=[SessionRef("s", "matchmaking")])
        message = live_status.absence_message(NAME, stat, steam_running=None)
        assert "in a Smite 2 match right now" in message

    def test_a_party_is_not_a_match(self):
        """A group session must not read as a live game."""
        stat = status(online=True, sessions=[SessionRef("s", "party")])
        message = live_status.absence_message(NAME, stat, steam_running=None)
        assert "match right now" not in message
        assert "in a match tracker.gg can see" in message

    def test_online_in_a_lobby_is_sharpened_by_the_state(self):
        stat = status(online=True, state="InLobby")
        message = live_status.absence_message(NAME, stat, steam_running=None)
        assert "sitting in a Smite 2 lobby" in message

    def test_online_without_a_lobby_state_is_generic(self):
        message = live_status.absence_message(NAME, status(online=True), None)
        assert "in Smite 2 right now" in message
        assert "lobby" not in message

    def test_steam_is_the_backstop_when_rallyhere_is_silent(self):
        message = live_status.absence_message(NAME, None, steam_running=True)
        assert "in Smite 2 right now" in message

    def test_steam_not_running_is_the_plain_no(self):
        message = live_status.absence_message(NAME, None, steam_running=False)
        assert "isn't in a match" in message

    def test_nothing_at_all_is_the_plain_no(self):
        message = live_status.absence_message(NAME, None, steam_running=None)
        assert "isn't in a match" in message

    def test_offline_with_a_stale_lobby_state_does_not_read_as_playing(self):
        """The trap: presence `state` outlives the session that set it.

        A player logged off from a lobby keeps `state=InLobby` while
        `online=False`. That must fall through to the Steam/plain answer, not
        announce them as in a lobby.
        """
        stat = status(online=False, state="InLobby")
        message = live_status.absence_message(NAME, stat, steam_running=False)
        assert "isn't in a match" in message

    def test_the_player_name_is_always_in_the_message(self):
        for stat, steam in (
            (status(online=True, sessions=[SessionRef("s", "match")]), None),
            (status(online=True), None),
            (None, True),
            (None, False),
        ):
            assert NAME in live_status.absence_message(NAME, stat, steam)


class TestNeedsSteamFallback:
    def test_no_fallback_once_rallyhere_places_them_in_a_match(self):
        stat = status(online=True, sessions=[SessionRef("s", "match")])
        assert live_status.needs_steam_fallback(stat) is False

    def test_no_fallback_once_rallyhere_says_online(self):
        assert live_status.needs_steam_fallback(status(online=True)) is False

    def test_fallback_when_rallyhere_could_not_answer(self):
        assert live_status.needs_steam_fallback(None) is True

    def test_fallback_when_rallyhere_says_offline(self):
        assert live_status.needs_steam_fallback(status(online=False)) is True
