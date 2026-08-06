"""Does the bot still assemble, and do the commands still declare what they should?

The cogs are constructed at import time in `__main__` against a live provider,
so nothing about their wiring was ever checked without a Discord token and a
Hi-Rez key. These tests build the same objects against a fake provider, which
catches the failure modes that the multi-game refactor actually risks: a cog
whose constructor no longer matches its call site, a command that lost an
option, an autocomplete that cannot reach the registry.

They are not a substitute for running the bot — py-cord's decorators are
evaluated at class-definition time, so this proves the shape of the command
tree, not that Discord accepts it.
"""

from __future__ import annotations

import os
import types

import pytest

os.environ.setdefault("SMITELE_DISCORD_TOKEN", "test-token")
os.environ.setdefault("SMITELE_HIREZ_DEV_ID", "0")
os.environ.setdefault("SMITELE_HIREZ_AUTH_KEY", "0")

discord = pytest.importorskip("discord", reason="py-cord not installed")

from game import DEFAULT_GAME, Game  # noqa: E402
from guild_settings import GuildSettings  # noqa: E402
from providers import Providers  # noqa: E402


def fake_god(name: str, god_id: int):
    return types.SimpleNamespace(
        name=name, id=types.SimpleNamespace(value=god_id), icon_url="", role=None
    )


def fake_item(name: str, item_id: int):
    return types.SimpleNamespace(
        name=name,
        id=item_id,
        active=True,
        tier=3,
        is_starter=False,
        price=100,
        parent_item_id=None,
        item_properties=[],
        icon_url="",
    )


class FakeProvider:
    """Enough of a provider to build a cog against."""

    def __init__(self, game: Game, gods=("Anubis", "Ra", "Chang'e", "Ah Muzen Cab")):
        self.game = game
        self.gods = {
            i: fake_god(name, i) for i, name in enumerate(gods, start=1)
        }
        self.items = {1: fake_item("Book of Thoth", 1)}
        self.build_stats = None
        self.player_matches = None

    async def create(self):
        pass

    async def load_dataframe(self):
        pass

    def load_build_stats(self):
        return False

    def god_by_name(self, name):
        from unidecode import unidecode

        wanted = unidecode(str(name)).strip().lower().replace("'", "")
        for god in self.gods.values():
            if unidecode(god.name).lower().replace("'", "") == wanted:
                return god
        return None

    def god_id_from_name(self, name):
        god = self.god_by_name(name)
        return None if god is None else god.id

    def random_god_id(self):
        return next(iter(self.gods))


@pytest.fixture
def registry(tmp_path):
    settings = GuildSettings(str(tmp_path / "guild_settings.json"))
    return Providers(FakeProvider(Game.SMITE), settings=settings), settings


@pytest.fixture
def bot():
    import asyncio

    from discord.ext import commands

    # py-cord reaches for the running loop while constructing the Bot. Under
    # pytest-asyncio each test gets a fresh loop that is closed afterwards, so
    # by the second test there is none installed and construction raises.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    intents = discord.Intents.default()
    return commands.Bot(command_prefix="$", intents=intents)


@pytest.fixture
def cogs(bot, registry):
    from player_stats import PlayerStats
    from smitele_bot import Smitele
    from smitetrivia import SmiteTrivia

    providers, settings = registry
    smitele = Smitele(bot, providers, settings)
    trivia = SmiteTrivia(bot, providers)
    stats = PlayerStats(providers)
    bot.add_cog(smitele)
    bot.add_cog(trivia)
    bot.add_cog(stats)
    return types.SimpleNamespace(
        bot=bot, smitele=smitele, trivia=trivia, stats=stats, settings=settings
    )


def commands_by_name(bot):
    # pending_application_commands, not application_commands: the latter is
    # populated by the sync that happens on connect, which never runs here.
    return {c.name: c for c in bot.pending_application_commands}


def test_the_bot_assembles(cogs):
    """The __main__ block's exact construction, minus the network."""
    names = commands_by_name(cogs.bot)
    for expected in ("build", "random_build", "smitele", "edge", "trivia", "set_game"):
        assert expected in names, f"{expected} did not register"


def test_every_guild_scoped_command_gets_all_three_guilds(cogs):
    """Eleven commands carried inline two-guild copies, so one server could
    play the game but not ask for a build."""
    from slash_guilds import SLASH_COMMAND_GUILD_IDS

    for command in cogs.bot.pending_application_commands:
        ids = getattr(command, "guild_ids", None)
        if not ids:
            continue
        assert sorted(ids) == sorted(SLASH_COMMAND_GUILD_IDS), (
            f"{command.name} is registered to {ids}"
        )


@pytest.mark.parametrize(
    "command,expected",
    [
        ("build", "god_name"),
        ("random_build", "god_name"),
        ("smitele", "god"),
        ("edge", "god"),
        ("trivia", None),
    ],
)
def test_game_option_comes_first(cogs, command, expected):
    """Discord sends options in declaration order, and the god autocomplete
    reads the chosen game out of `ctx.options` while the user is still typing —
    so `game` has to be sent before the god field, not after."""
    options = [o.name for o in commands_by_name(cogs.bot)[command].options]
    assert options[0] == "game", f"{command} declares {options}"
    if expected is not None:
        assert options.index("game") < options.index(expected)


@pytest.mark.parametrize("command,option", [
    ("build", "god_name"),
    ("random_build", "god_name"),
    ("smitele", "god"),
    ("edge", "god"),
    ("edge", "against"),
])
def test_god_options_have_autocomplete(cogs, command, option):
    """There was no autocomplete anywhere before this; god names were free text
    validated after the fact."""
    found = [o for o in commands_by_name(cogs.bot)[command].options if o.name == option]
    assert found, f"{command} has no {option} option"
    assert found[0].autocomplete is not None


@pytest.mark.asyncio
async def test_god_autocomplete_filters_and_folds_accents(cogs):
    from smitele_bot import god_autocomplete

    ctx = types.SimpleNamespace(
        bot=cogs.bot,
        options={},
        value="chang",
        interaction=types.SimpleNamespace(guild_id=1),
    )
    assert "Chang'e" in await god_autocomplete(ctx)

    # Apostrophes are not required, and matching is not only a prefix.
    ctx.value = "muzen"
    assert "Ah Muzen Cab" in await god_autocomplete(ctx)

    ctx.value = "zzzz"
    assert await god_autocomplete(ctx) == []


@pytest.mark.asyncio
async def test_autocomplete_respects_an_absent_game_option(cogs):
    """Discord omits an option the user never touched, so `game` missing from
    ctx.options must fall through to the guild default rather than erroring."""
    from smitele_bot import god_autocomplete

    ctx = types.SimpleNamespace(
        bot=cogs.bot,
        options={},
        value="",
        interaction=types.SimpleNamespace(guild_id=999),
    )
    assert len(await god_autocomplete(ctx)) == 4


@pytest.mark.asyncio
async def test_autocomplete_caps_at_discords_limit(bot, registry):
    """Discord rejects a response with more than 25 choices outright."""
    providers, settings = registry
    from smitele_bot import Smitele, god_autocomplete

    big = Providers(
        FakeProvider(Game.SMITE, gods=tuple(f"God{i}" for i in range(60))),
        settings=settings,
    )
    bot.add_cog(Smitele(bot, big, settings))
    ctx = types.SimpleNamespace(
        bot=bot, options={}, value="", interaction=types.SimpleNamespace(guild_id=1)
    )
    assert len(await god_autocomplete(ctx)) == 25


def test_trivia_withholds_the_friends_category_for_smite2(cogs):
    """FRIENDS asks about specific players through the Hi-Rez player API, which
    has no Smite 2 counterpart."""
    from smitetrivia import TriviaCategory

    smite1 = cogs.trivia.categories_for(FakeProvider(Game.SMITE))
    smite2 = cogs.trivia.categories_for(FakeProvider(Game.SMITE_2))
    assert TriviaCategory.FRIENDS in smite1
    assert TriviaCategory.FRIENDS not in smite2
    assert len(smite2) == len(smite1) - 1


def test_is_ready_requires_every_registered_provider(bot, registry):
    """A half-loaded bot would answer for one game and fail for the other, and
    the deploy guard reads this to decide whether to cut over."""
    from smitele_bot import Smitele

    providers, settings = registry
    empty = FakeProvider(Game.SMITE_2)
    empty.gods = {}
    both = Providers(FakeProvider(Game.SMITE), empty, settings=settings)
    assert Smitele(bot, both, settings).is_ready is False

    ready = Providers(
        FakeProvider(Game.SMITE), FakeProvider(Game.SMITE_2), settings=settings
    )
    assert Smitele(bot, ready, settings).is_ready is True


def test_set_game_choices_cover_every_game(cogs):
    option = [o for o in commands_by_name(cogs.bot)["set_game"].options if o.name == "game"][0]
    labels = [c.name if hasattr(c, "name") else c for c in option.choices]
    assert sorted(labels) == sorted(g.display_name for g in Game)


def test_default_game_is_what_resolution_falls_back_to(cogs):
    assert cogs.smitele.provider(types.SimpleNamespace(guild_id=None)).game is DEFAULT_GAME
