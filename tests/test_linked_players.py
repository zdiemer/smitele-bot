"""Which account a Discord user plays under, and what happens when nobody said.

The store is small; the precedence is the part worth pinning. `roster.py` keeps
answering for the people already in it, an explicit link outranks it, and every
failure mode has to land on "we don't know" rather than on an exception —
because the caller is a build command that should lose its matchup, not its
response.
"""

from __future__ import annotations

import json
import os

import pytest

linked_players = pytest.importorskip("linked_players")
roster = pytest.importorskip("roster")
game_module = pytest.importorskip("game")

Game = game_module.Game
LinkedPlayers = linked_players.LinkedPlayers

# Someone who is really in roster.py, so the fallback is tested against the
# actual data rather than a stand-in for it.
ROSTERED = next(iter(roster.DISCORD_TO_SMITE))
STRANGER = 1


def store(tmp_path) -> LinkedPlayers:
    return LinkedPlayers(path=os.path.join(str(tmp_path), "linked_players.json"))


class TestPrecedence:
    def test_an_unlinked_stranger_is_unknown(self, tmp_path):
        assert store(tmp_path).handle_for(STRANGER, Game.SMITE) is None

    def test_the_roster_still_answers_for_the_people_in_it(self, tmp_path):
        """Nobody who worked before has to run /link to keep working."""
        assert store(tmp_path).handle_for(ROSTERED, Game.SMITE) is not None

    def test_an_explicit_link_beats_the_roster(self, tmp_path):
        """Someone in the roster who links a different account meant it."""
        links = store(tmp_path)
        links.link(ROSTERED, Game.SMITE, "SomeoneElse")
        assert links.handle_for(ROSTERED, Game.SMITE) == "SomeoneElse"

    def test_a_link_for_one_game_does_not_answer_for_the_other(self, tmp_path):
        """The two games do not share an identity; roster.py keeps two maps
        for the same reason."""
        links = store(tmp_path)
        links.link(STRANGER, Game.SMITE_2, "steam:123")
        assert links.handle_for(STRANGER, Game.SMITE_2) == "steam:123"
        assert links.handle_for(STRANGER, Game.SMITE) is None

    def test_no_user_id_is_unknown_rather_than_an_error(self, tmp_path):
        """There is no user in a webhook context, and that is not a failure."""
        assert store(tmp_path).handle_for(None, Game.SMITE) is None


class TestPersistence:
    def test_a_link_survives_a_restart(self, tmp_path):
        store(tmp_path).link(STRANGER, Game.SMITE, "Weak3n")
        assert store(tmp_path).handle_for(STRANGER, Game.SMITE) == "Weak3n"

    def test_the_handle_is_stored_stripped(self, tmp_path):
        links = store(tmp_path)
        links.link(STRANGER, Game.SMITE, "  Weak3n  ")
        assert links.handle_for(STRANGER, Game.SMITE) == "Weak3n"

    def test_a_corrupt_file_falls_back_rather_than_raising(self, tmp_path):
        path = os.path.join(str(tmp_path), "linked_players.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        links = LinkedPlayers(path=path)
        assert links.handle_for(STRANGER, Game.SMITE) is None
        # And the roster underneath is untouched by the bad file.
        assert links.handle_for(ROSTERED, Game.SMITE) is not None

    def test_a_file_of_the_wrong_shape_falls_back(self, tmp_path):
        path = os.path.join(str(tmp_path), "linked_players.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(["not", "a", "mapping"], handle)
        assert LinkedPlayers(path=path).handle_for(STRANGER, Game.SMITE) is None

    def test_writing_leaves_no_partial_behind(self, tmp_path):
        """The write goes through a temporary file; it must not stay one."""
        links = store(tmp_path)
        links.link(STRANGER, Game.SMITE, "Weak3n")
        assert os.listdir(str(tmp_path)) == ["linked_players.json"]


class TestUnlinking:
    def test_unlinking_one_game_leaves_the_other(self, tmp_path):
        links = store(tmp_path)
        links.link(STRANGER, Game.SMITE, "Weak3n")
        links.link(STRANGER, Game.SMITE_2, "steam:123")
        assert links.unlink(STRANGER, Game.SMITE) is True
        assert links.handle_for(STRANGER, Game.SMITE) is None
        assert links.handle_for(STRANGER, Game.SMITE_2) == "steam:123"

    def test_unlinking_without_a_game_forgets_everything(self, tmp_path):
        """Someone asking to be forgotten means all of it."""
        links = store(tmp_path)
        links.link(STRANGER, Game.SMITE, "Weak3n")
        links.link(STRANGER, Game.SMITE_2, "steam:123")
        assert links.unlink(STRANGER) is True
        assert links.handle_for(STRANGER, Game.SMITE) is None
        assert links.handle_for(STRANGER, Game.SMITE_2) is None

    def test_unlinking_what_was_never_linked_says_so(self, tmp_path):
        assert store(tmp_path).unlink(STRANGER) is False

    def test_unlinking_does_not_remove_someone_from_the_roster(self, tmp_path):
        """It cannot, and the command has to be able to say that rather than
        reporting a success that changed nothing."""
        links = store(tmp_path)
        assert links.unlink(ROSTERED, Game.SMITE) is False
        assert links.handle_for(ROSTERED, Game.SMITE) is not None

    def test_is_linked_ignores_the_roster(self, tmp_path):
        links = store(tmp_path)
        assert links.is_linked(ROSTERED, Game.SMITE) is False
        links.link(ROSTERED, Game.SMITE, "SomeoneElse")
        assert links.is_linked(ROSTERED, Game.SMITE) is True
