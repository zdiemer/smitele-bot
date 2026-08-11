"""Splitting one shared leaderboard into three without anyone losing a point.

The flat file records who scored and not where, so the only honest thing to do
with those totals is hand every guild a copy and let the boards diverge. What
these pin down is that the copy happens once: a guild that has played since
must not be re-seeded, or every restart would wipe its round back to the old
totals.
"""

from __future__ import annotations

import json

import pytest

from trivia_scores import TriviaScores


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "scores.json")


def test_a_fresh_install_has_an_empty_board(path):
    assert TriviaScores(path).board_for(123) == []


def test_a_round_lands_on_the_guild_that_played_it(path):
    scores = TriviaScores(path)
    scores.record(123, {11: 3, 22: 1})
    assert scores.board_for(123) == [(11, 3), (22, 1)]
    assert scores.board_for(456) == []


def test_rounds_accumulate(path):
    scores = TriviaScores(path)
    scores.record(123, {11: 3})
    scores.record(123, {11: 2, 22: 1})
    assert scores.board_for(123) == [(11, 5), (22, 1)]


def test_survives_a_restart(path):
    TriviaScores(path).record(123, {11: 3})
    assert TriviaScores(path).board_for(123) == [(11, 3)]


class TestTheSplit:
    """What happens to the totals from before scores were per guild."""

    @pytest.fixture
    def flat(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"11": 40, "22": 25}, handle)
        return path

    def test_every_guild_starts_from_the_old_totals(self, flat):
        scores = TriviaScores(flat)
        assert scores.board_for(123) == [(11, 40), (22, 25)]
        assert scores.board_for(456) == [(11, 40), (22, 25)]

    def test_the_boards_diverge_from_there(self, flat):
        scores = TriviaScores(flat)
        scores.record(123, {22: 20})
        assert scores.board_for(123) == [(22, 45), (11, 40)]
        assert scores.board_for(456) == [(11, 40), (22, 25)]

    def test_a_guild_that_has_played_is_not_seeded_again(self, flat):
        TriviaScores(flat).record(123, {22: 20})
        assert TriviaScores(flat).board_for(123) == [(22, 45), (11, 40)]

    def test_a_guild_that_has_not_played_yet_is_still_seeded_later(self, flat):
        TriviaScores(flat).record(123, {22: 20})
        assert TriviaScores(flat).board_for(789) == [(11, 40), (22, 25)]


def test_a_corrupt_file_costs_the_scores_and_not_the_command(path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    assert TriviaScores(path).board_for(123) == []


def test_an_unreadable_entry_is_dropped_rather_than_the_board(path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"11": 40, "not-a-user": 5, "22": "nonsense"}, handle)
    assert TriviaScores(path).board_for(123) == [(11, 40)]


def test_an_empty_round_writes_nothing(path):
    scores = TriviaScores(path)
    scores.record(123, {})
    assert TriviaScores(path).board_for(123) == []
