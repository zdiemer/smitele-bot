"""Reading a Smite 2 lobby out of tracker.gg's shape.

The bot refused Smite 2 live matches on the belief that the lobby was not
exposed. It is, in two requests, and `scripts/probe_live_match.py` demonstrates
it against a running match. What these pin is the parsing, because every way it
can be wrong is quiet: a segment mistaken for a player adds an eleventh god, a
missed team turns five enemies into nine, and either produces a build aimed at
a lobby that never existed.

The fixtures are the real payload's shape, taken from a live ranked Conquest
match on 2026-08-10: twelve segments of which ten are players, teams named
`order` and `chaos` rather than numbered, and the requesting player's own god
arriving from a *different* request than the other nine.
"""

from __future__ import annotations

import pytest

players_module = pytest.importorskip("smite2.players")

LiveMatch = players_module.LiveMatch
LivePlayer = players_module.LivePlayer
_live_player = players_module._live_player  # noqa: SLF001 — the unit under test


def segment(god: str = None, team: str = "order", handle: str = "") -> dict:
    """A match segment in tracker.gg's shape.

    Type is `overview` for player rows *and* for the lobby's own summary, which
    is why the god name is what separates them.
    """
    metadata = {"godName": god} if god else {}
    if handle:
        metadata["platformInfo"] = {"platformUserHandle": handle}
    return {"type": "overview", "attributes": {"teamId": team}, "metadata": metadata}


LOBBY = [
    segment("Achilles", "order", "zachd"),
    segment("Bastet", "order"),
    segment("Thanatos", "order"),
    segment("Kukulkan", "order"),
    segment("Apollo", "order"),
    segment("Mordred", "chaos"),
    segment("Princess Bari", "chaos"),
    segment("Susano", "chaos"),
    segment("Gilgamesh", "chaos"),
    segment("Xbalanque", "chaos"),
    # The two that are not players. A match carries twelve segments.
    segment(None, "order"),
    segment(None, "chaos"),
]


def lobby(own_god: str = "Achilles", own_team: str = "order") -> LiveMatch:
    found = [p for p in (_live_player(s) for s in LOBBY) if p is not None]
    return LiveMatch(
        match_id="cbdb6807-16ea-475b-ab3a-c37126ef7dcc",
        mode="conquest-ranked",
        mode_name="Ranked Conquest",
        ranked=True,
        own_god=own_god,
        own_team=own_team,
        players=found,
    )


class TestReadingASegment:
    def test_a_player_row_yields_a_player(self):
        found = _live_player(segment("Bastet", "order", "someone"))
        assert found == LivePlayer(god="Bastet", team="order", handle="someone")

    def test_a_segment_without_a_god_is_not_a_player(self):
        """The two non-player segments share the player rows' type, so
        selecting on type would put twelve gods in a ten-god lobby."""
        assert _live_player(segment(None)) is None

    def test_a_missing_handle_is_empty_rather_than_absent(self):
        """Live snapshots frequently omit the handle; the god is what matters
        and a missing name must not drop the player."""
        found = _live_player(segment("Susano", "chaos"))
        assert found is not None
        assert found.handle == ""

    def test_the_team_can_arrive_in_metadata_instead(self):
        raw = {"attributes": {}, "metadata": {"godName": "Ares", "teamId": "chaos"}}
        assert _live_player(raw).team == "chaos"


class TestSplittingTheLobby:
    def test_ten_players_come_out_of_twelve_segments(self):
        assert len(lobby().players) == 10

    def test_allies_exclude_the_player_themselves(self):
        """Four, not five. The requesting player is on their own team and
        building for themselves."""
        assert lobby().allies == ["Bastet", "Thanatos", "Kukulkan", "Apollo"]

    def test_enemies_are_the_other_team(self):
        assert lobby().enemies == [
            "Mordred",
            "Princess Bari",
            "Susano",
            "Gilgamesh",
            "Xbalanque",
        ]

    def test_reading_it_from_the_other_side_mirrors_it(self):
        """The lobby is symmetric, so a chaos player sees the reverse. This is
        the assertion that would fail if `own_team` were ignored and the split
        hard-coded to order-versus-chaos."""
        theirs = lobby(own_god="Mordred", own_team="chaos")
        assert theirs.enemies == ["Achilles", "Bastet", "Thanatos", "Kukulkan", "Apollo"]
        assert "Mordred" not in theirs.allies
        assert len(theirs.allies) == 4

    def test_a_duplicate_god_only_removes_one_copy(self):
        """Both teams can pick the same god in Smite 2's casual modes. Removing
        by value must not take the enemy's copy as well."""
        both = LiveMatch(
            match_id="x",
            mode="conquest",
            mode_name="Casual Conquest",
            ranked=False,
            own_god="Loki",
            own_team="order",
            players=[
                LivePlayer("Loki", "order", ""),
                LivePlayer("Loki", "chaos", ""),
                LivePlayer("Ares", "order", ""),
            ],
        )
        assert both.allies == ["Ares"]
        assert both.enemies == ["Loki"]

    def test_a_player_with_no_team_counts_as_neither(self):
        """An unlabelled row is dropped from enemies rather than assumed
        hostile: aiming a build at a god who might be a team-mate is worse than
        aiming at one fewer."""
        partial = LiveMatch(
            match_id="x",
            mode="conquest-ranked",
            mode_name="Ranked Conquest",
            ranked=True,
            own_god="Achilles",
            own_team="order",
            players=[
                LivePlayer("Achilles", "order", ""),
                LivePlayer("Mordred", "", ""),
            ],
        )
        assert partial.enemies == []
        assert partial.allies == []
