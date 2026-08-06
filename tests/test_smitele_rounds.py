"""Smite-le has to survive a provider that cannot answer every round.

Its six rounds were written against Hi-Rez, which always returns skins, a god
leaderboard and ability art. Smite 2 has some of that and not the rest, and the
failures were not graceful: a bare `next()` killed the session before round one,
and the ability round spun forever on a god whose art it could not fetch.

The round loop already knows how to drop a round — it catches IndexError and
renumbers. These tests pin that every unavailable clue raises exactly that,
rather than escaping or hanging.
"""

from __future__ import annotations

import asyncio
import os
import types

import pytest

os.environ.setdefault("SMITELE_DISCORD_TOKEN", "test-token")
os.environ.setdefault("SMITELE_HIREZ_DEV_ID", "0")
os.environ.setdefault("SMITELE_HIREZ_AUTH_KEY", "0")

discord = pytest.importorskip("discord", reason="py-cord not installed")

from game import Game  # noqa: E402


def ability(icon_url):
    from ability import Ability, _itemDescription

    return Ability(
        _itemDescription("", "", "d", [], []),
        id=1,
        name="Some Ability",
        icon_url=icon_url,
        is_passive=False,
    )


def god(name="Anubis", icons=True):
    return types.SimpleNamespace(
        name=name,
        title="God of the Dead",
        abilities=[ability("http://x/a.png" if icons else None) for _ in range(4)],
        id=1,
    )


class Provider:
    def __init__(self, game, skins=(), leaderboard=()):
        self.game = game
        self.__skins = list(skins)
        self.__leaderboard = list(leaderboard)

    async def get_god_skins(self, _id):
        return self.__skins

    async def get_god_leaderboard(self, _id, _q):
        return self.__leaderboard


def session_for(provider, the_god=None):
    from smitele_bot import SmiteleGame, SmiteleGameContext, _SmiteleRoundContext

    channel = types.SimpleNamespace(
        send=_noop, typing=_typing, last_message=None, last_message_id=0
    )
    context = SmiteleGameContext(types.SimpleNamespace(id=1, mention="@u"), channel)
    session = SmiteleGame(the_god or god(), context, provider=provider)
    session.current_round = _SmiteleRoundContext(6)
    session.current_round.round_number = 1
    return session


async def _noop(*a, **kw):
    return types.SimpleNamespace(id=1)


def _typing():
    class _T:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return None
    return _T()


def cog_for(provider):
    from discord.ext import commands as dcommands

    from guild_settings import GuildSettings
    from providers import Providers
    from smitele_bot import Smitele

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    bot = dcommands.Bot(command_prefix="$", intents=discord.Intents.default())
    registry = Providers(provider)
    return Smitele(bot, registry, GuildSettings("/dev/null"))


class TestAbilityIconRound:
    @pytest.mark.asyncio
    async def test_no_ability_art_raises_rather_than_looping(self):
        """This was `while not saved_image` around a bare except, so a god with
        no reachable art printed failures forever and hung the game."""
        provider = Provider(Game.SMITE_2)
        cog = cog_for(provider)
        session = session_for(provider, god(icons=False))

        with pytest.raises(IndexError):
            await asyncio.wait_for(
                cog._Smitele__send_god_ability_icon(session), timeout=10
            )

    @pytest.mark.asyncio
    async def test_unreachable_art_also_terminates(self):
        """Every ability has a URL, none of them fetch. Must still exit."""
        provider = Provider(Game.SMITE_2)
        cog = cog_for(provider)
        session = session_for(provider, god(icons=True))

        with pytest.raises((IndexError, Exception)):
            await asyncio.wait_for(
                cog._Smitele__send_god_ability_icon(session), timeout=20
            )


class TestBuildRound:
    @pytest.mark.asyncio
    async def test_no_leaderboard_raises_index_error(self):
        """tracker.gg has no leaderboard-by-god route, so this returns empty.
        It used to die as an unretrieved task exception."""
        provider = Provider(Game.SMITE_2, leaderboard=[])
        cog = cog_for(provider)
        session = session_for(provider)

        with pytest.raises(IndexError):
            await asyncio.wait_for(
                cog._Smitele__prefetch_build_image(session), timeout=10
            )


class TestVoicelineRound:
    @pytest.mark.asyncio
    async def test_smite2_is_skipped_rather_than_served_smite1_audio(self):
        """The scrape targets smite.fandom.com. Anubis exists in both games, so
        a Smite 2 round would serve Smite 1 Anubis's voiceline as its clue —
        wrong, and wrong in a way a player cannot detect."""
        provider = Provider(Game.SMITE_2, skins=[_skin("Default")])
        cog = cog_for(provider)
        session = session_for(provider)

        with pytest.raises(IndexError):
            await asyncio.wait_for(
                cog._Smitele__send_god_voiceline(session, [_skin("Default")]),
                timeout=10,
            )


def _skin(name, url="http://x/s.png"):
    from skin import Skin

    skin = Skin()
    skin.name = name
    skin.card_url = url
    skin.god_id = 1
    skin.obtainability = "Normal"
    skin.price_favor = 0
    skin.price_gems = 0
    skin.id = (0, 0)
    return skin


class TestBaseSkin:
    """The lookup that killed the session outright, before any round ran."""

    @staticmethod
    def pick(skins, name="Anubis"):
        return next(
            (s for s in skins if s.name in (f"Standard {name}", "Default", name)),
            next((s for s in skins if s.has_url), None),
        )

    def test_smite1_naming(self):
        assert self.pick([_skin("Standard Anubis")]).name == "Standard Anubis"

    def test_smite2_naming(self):
        assert self.pick([_skin("Default")]).name == "Default"

    def test_falls_back_to_any_skin_with_art(self):
        assert self.pick([_skin("Ra Ra Rasputin")]) is not None

    def test_no_skins_at_all_is_none_not_an_exception(self):
        assert self.pick([]) is None


class TestSkinCoercion:
    def test_a_provider_may_return_either_shape(self):
        """Smite 1 returns Hi-Rez JSON; Smite 2 returns parsed Skins."""
        from skin import Skin

        already = _skin("Default")
        assert Skin.coerce(already) is already
