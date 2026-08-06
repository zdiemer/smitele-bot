"""The resolution order here decides what every command does when the user
passes no `game:`, which is the common case. Getting it wrong silently answers
Smite 1 questions with Smite 2 data.
"""

from __future__ import annotations

import json

import pytest

from game import DEFAULT_GAME, Game
from guild_settings import GuildSettings


@pytest.fixture
def settings(tmp_path):
    return GuildSettings(str(tmp_path / "guild_settings.json"))


def test_unset_guild_gets_the_global_default(settings):
    assert settings.game_for(123) is DEFAULT_GAME


def test_dms_get_the_global_default(settings):
    assert settings.game_for(None) is DEFAULT_GAME


def test_set_then_read(settings):
    settings.set_game(123, Game.SMITE_2)
    assert settings.game_for(123) is Game.SMITE_2
    assert settings.game_for(456) is DEFAULT_GAME


def test_survives_a_restart(tmp_path):
    path = str(tmp_path / "guild_settings.json")
    GuildSettings(path).set_game(123, Game.SMITE_2)
    assert GuildSettings(path).game_for(123) is Game.SMITE_2


def test_corrupt_file_falls_back_rather_than_failing(tmp_path):
    path = tmp_path / "guild_settings.json"
    path.write_text("{ this is not json")
    assert GuildSettings(str(path)).game_for(123) is DEFAULT_GAME


def test_unknown_stored_value_falls_back(tmp_path):
    path = tmp_path / "guild_settings.json"
    path.write_text(json.dumps({"123": {"game": "smite3"}}))
    assert GuildSettings(str(path)).game_for(123) is DEFAULT_GAME


def test_explicit_option_beats_the_guild_default(settings):
    settings.set_game(123, Game.SMITE_2)
    assert settings.resolve("Smite 1", 123) is Game.SMITE
    assert settings.resolve("Smite 2", 123) is Game.SMITE_2


def test_absent_option_falls_through_to_the_guild(settings):
    """Discord omits an option the user never touched rather than sending its
    default, so None must mean 'they did not say'."""
    settings.set_game(123, Game.SMITE_2)
    assert settings.resolve(None, 123) is Game.SMITE_2
    assert settings.resolve("", 123) is Game.SMITE_2


def test_unparseable_option_falls_through_rather_than_raising(settings):
    settings.set_game(123, Game.SMITE_2)
    assert settings.resolve("Smite 7", 123) is Game.SMITE_2


@pytest.mark.parametrize(
    "spelling", ["Smite 1", "smite 1", "smite1", "SMITE-1", "smite"]
)
def test_display_name_round_trips(spelling):
    assert Game.from_display_name(spelling) is Game.SMITE


@pytest.mark.parametrize("spelling", ["Smite 2", "smite2", "SMITE 2", "smite-2"])
def test_smite2_display_name_round_trips(spelling):
    assert Game.from_display_name(spelling) is Game.SMITE_2
