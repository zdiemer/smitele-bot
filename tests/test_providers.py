"""Resolution decides which game's data answers a command, so its edge cases
are pinned — particularly the ones that only appear in a half-deployed state,
where a guild has chosen a game whose provider is not registered.
"""

from __future__ import annotations

import types

import pytest

from game import DEFAULT_GAME, Game
from guild_settings import GuildSettings
from providers import Providers


def fake(game: Game):
    return types.SimpleNamespace(
        game=game, gods={}, items={}, name=f"provider:{game.value}"
    )


def ctx(guild_id):
    return types.SimpleNamespace(guild_id=guild_id)


@pytest.fixture
def settings(tmp_path):
    return GuildSettings(str(tmp_path / "guild_settings.json"))


def test_only_registered_games_are_offered(settings):
    one = Providers(fake(Game.SMITE), settings=settings)
    assert one.choices == ["Smite 1"]

    both = Providers(fake(Game.SMITE), fake(Game.SMITE_2), settings=settings)
    assert both.choices == ["Smite 1", "Smite 2"]


def test_choices_follow_enum_order_not_registration_order(settings):
    registry = Providers(fake(Game.SMITE_2), fake(Game.SMITE), settings=settings)
    assert registry.choices == ["Smite 1", "Smite 2"]


def test_explicit_option_wins(settings):
    registry = Providers(fake(Game.SMITE), fake(Game.SMITE_2), settings=settings)
    settings.set_game(1, Game.SMITE)
    assert registry.for_ctx(ctx(1), "Smite 2").game is Game.SMITE_2


def test_guild_default_applies_when_no_option(settings):
    registry = Providers(fake(Game.SMITE), fake(Game.SMITE_2), settings=settings)
    settings.set_game(1, Game.SMITE_2)
    assert registry.for_ctx(ctx(1)).game is Game.SMITE_2
    assert registry.for_ctx(ctx(2)).game is DEFAULT_GAME


def test_a_guild_pointing_at_an_unregistered_game_degrades(settings):
    """The half-deployed state: a guild set Smite 2, then the provider failed to
    start or was rolled back. Answering with Smite 1 beats raising on every
    command in that server."""
    settings.set_game(1, Game.SMITE_2)
    registry = Providers(fake(Game.SMITE), settings=settings)
    assert registry.for_ctx(ctx(1)).game is Game.SMITE


def test_an_explicit_unregistered_choice_also_degrades(settings):
    registry = Providers(fake(Game.SMITE), settings=settings)
    assert registry.for_ctx(ctx(1), "Smite 2").game is Game.SMITE


def test_dms_resolve_without_a_guild(settings):
    registry = Providers(fake(Game.SMITE), fake(Game.SMITE_2), settings=settings)
    assert registry.for_ctx(ctx(None)).game is DEFAULT_GAME
    assert registry.for_ctx(ctx(None), "Smite 2").game is Game.SMITE_2


def test_works_without_settings_at_all(tmp_path):
    """The collector and the aggregate job build a registry too, and have no
    guild settings to consult."""
    registry = Providers(fake(Game.SMITE), fake(Game.SMITE_2))
    assert registry.resolve(None, 1) is DEFAULT_GAME
    assert registry.resolve("Smite 2", 1) is Game.SMITE_2
