"""Smite-le Bot - A Discord bot for playing Smite-le, the Smite and Wordle inspired game.

This module implements a Discord bot which allows users to play a six round game in Discord
channels. It requires some things in order to get this running: ffmpeg being installed on the
host machine, access to the Hirez API, and a Discord token.

Hirez API keys and Discord token are loaded from the environment
(SMITELE_DISCORD_TOKEN, SMITELE_HIREZ_DEV_ID, SMITELE_HIREZ_AUTH_KEY) or from a
config.json file, see the README for more details.

Typical usage example:

    bot = SmiteleBot()
    bot.start_bot()
"""

from __future__ import annotations
import asyncio
import io
import json
import math
import os
import random
import time
import traceback
from json.decoder import JSONDecodeError
from typing import Any, Callable, Coroutine, Dict, Generator, List, Set, Tuple

import aiohttp
import discord
import edit_distance
from bs4 import BeautifulSoup
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont
from unidecode import unidecode

import credentials
import paths
from build_optimizer import BuildOptimizer, compute_item_price
from god import God
from god_builder import (
    BuildBalance,
    BuildCommandType,
    BuildFailedError,
    BuildPrioritization,
    BuildOptions,
    GeneratedBuild,
    GodBuilder,
    InvalidOptionError,
)
from god_types import GodId, GodRole, GodType
from guild_settings import GuildSettings
from linked_players import LinkedPlayers
from item import Item, ItemAttribute, ItemType
from player_stats import PlayerStats
from providers import GameProvider, Providers
from slash_guilds import SLASH_COMMAND_GUILD_IDS
from skin import Skin
from smite2.provider import Smite2Provider
from SmiteProvider import SmiteProvider
from smitetrivia import SmiteTrivia
from HirezAPI import PlayerRole, QueueId
from item_tree_builder import ItemTreeBuilder
import art_cache
import build_path_image
import live_lobby
import roster
from game import DEFAULT_GAME, Game, id_value
from status_server import DEFAULT_PORT as DEFAULT_STATUS_PORT, StatusServer
from recommend import BuildRecommender


# The `game:` option, on every command whose answer depends on which game is
# being asked about. Declared through a helper so the description and the
# choice list cannot drift apart across a dozen commands.
#
# The choices are filled in at class-definition time from the registry, which
# does not exist yet, so they are the full set here and resolution degrades to
# the default when a game has no provider. See providers.Providers.resolve.
GAME_OPTION_DESCRIPTION = "Which game to answer for; defaults to this server's"


def game_option(command):
    """Attach the `game:` option.

    Declared *first* on every command, because Discord sends options in
    declaration order and the god-name autocomplete needs to see the chosen
    game in `ctx.options` while the user is still typing the god.
    """
    return discord.option(
        name="game",
        type=str,
        description=GAME_OPTION_DESCRIPTION,
        choices=[g.display_name for g in Game],
        default="",
    )(command)


# The `match_queue:` value that means "do not filter by mode at all".
#
# This used to be what an unset option did, and it was the wrong default by some
# distance. Unspecified dimensions are summed rather than filtered, so no queue
# and no role pooled Conquest, Arena, Joust, Assault and Duel — and every lane —
# into one ranking, and the description named neither. Across the Smite 2 roster
# seventy-five of eighty-eight default answers shared no six items with the
# Conquest answer for the same god: an Aphrodite who asked for a build got the
# Arena one, with nothing on screen to say so.
#
# It is still a legitimate question, so it is still askable. It is just no
# longer the question the bot assumes you meant.
ALL_MODES: str = "All Modes"


def common_lane(provider, build_options) -> str:
    """The lane this god is played in most, in the mode being asked about.

    `/build` calls this when the caller named no role. Summing across lanes is
    what "any role" used to mean and it averages a support build with a mid one
    — recommending neither — so the lane is chosen rather than dissolved, and
    the description names it.

    Empty for a mode that has no lanes, for a god the corpus has never seen, and
    whenever there is no aggregate to ask. In each case the request goes out
    without a role, exactly as it used to.
    """
    queue_id = build_options.queue_id
    if queue_id is None or "CONQUEST" not in getattr(queue_id, "name", ""):
        return ""
    stats = getattr(provider, "build_stats", None)
    if stats is None:
        return ""
    try:
        return stats.common_role(
            id_value(build_options.god_id), queue_id.value
        ).lower()
    except Exception as error:  # noqa: BLE001 — a lane guess is not the build
        print(f"could not read a common lane: {error}", flush=True)
        return ""


def queue_choices() -> List[str]:
    """The `match_queue:` choices, across every game.

    One flat list rather than a per-game one, because Discord fixes a command's
    choices when it is registered and cannot narrow them once the user picks a
    game. The names do not collide — Smite 2's are prefixed where they would —
    and `BuildOptions.set_option` resolves against the chosen game's enum, so
    naming a Smite 1 queue while asking about Smite 2 is rejected rather than
    silently answered.
    """
    from smite2.queues import Smite2QueueId  # noqa: PLC0415

    smite1 = [
        q.display_name
        for q in QueueId
        if (QueueId.is_normal(q) or QueueId.is_ranked(q))
        and q
        not in (
            QueueId.UNDER_30_ARENA,
            QueueId.UNDER_30_CONQUEST,
            QueueId.UNDER_30_JOUST,
        )
    ]
    smite2 = [
        q.display_name
        for q in Smite2QueueId
        if Smite2QueueId.is_normal(q) or Smite2QueueId.is_ranked(q)
    ]
    seen = set(smite1)
    return [ALL_MODES] + smite1 + [name for name in smite2 if name not in seen]


async def god_autocomplete(ctx: discord.AutocompleteContext):
    """God names for whichever game the interaction is about.

    `ctx.options` holds only what the user has actually filled in — Discord does
    not send an option's default — so an absent `game` means "they have not
    said", which is exactly what `Providers.resolve` treats as "fall through to
    the guild default".
    """
    cog = ctx.bot.get_cog("Smitele")
    if cog is None:
        return []
    provider = cog.providers.for_ctx(ctx.interaction, ctx.options.get("game"))
    typed = unidecode(ctx.value or "").lower().replace("'", "")
    names = sorted(god.name for god in provider.gods.values())
    if not typed:
        return names[:25]
    matches = [n for n in names if unidecode(n).lower().replace("'", "").startswith(typed)]
    if len(matches) < 25:
        matches += [
            n
            for n in names
            if n not in matches and typed in unidecode(n).lower().replace("'", "")
        ]
    return matches[:25]


# InvalidOptionError used to be declared here as well as in god_builder, two
# unrelated classes with one name. `/build` caught this one while
# BuildOptions.set_option raised the other, so an unrecognised god name escaped
# as an unhandled exception instead of "X is not a God." Both parsers now raise
# and catch the same type.


class GodOptions:
    god_id: GodId
    build: List[Item]
    level: int
    include_abilities: bool
    __items: Dict[int, Item]

    def __init__(self, items: Dict[int, Item], provider=None):
        self.god_id = None
        self.build = []
        self.level = 20
        self.include_abilities = False
        self.__items = items
        self.__provider = provider

    def set_option(self, option: str, value: str):
        if option in ("-g", "--god"):
            if self.__provider is not None:
                god_id = self.__provider.god_id_from_name(value)
                if god_id is None:
                    raise InvalidOptionError
                self.god_id = god_id
            else:
                # handles Chang'e case
                self.god_id = GodId[
                    value.strip().upper().replace(" ", "_").replace("'", "")
                ]
        elif option in ("-b", "--build"):
            split_build = value.split(",")
            build_ids = []
            build: List[Item] = []

            # First try parsing as integers
            try:
                for i in split_build:
                    build_ids.append(int(i))
            except ValueError:
                build_ids = []

            if not any(build_ids):
                for _bi in split_build:
                    found_item = False
                    for i in self.__items.values():
                        if (
                            i.active
                            and i.type == ItemType.ITEM
                            and i.name.lower() == _bi.lower().strip()
                        ):
                            build.append(i)
                            found_item = True
                            break
                    if not found_item:
                        raise ValueError
            else:
                for _id in build_ids:
                    build.append(self.__items[_id])
            self.build = build
        elif option in ("-l", "--level"):
            self.level = int(value)
        elif option in ("-ia", "--include-abilities"):
            self.include_abilities = True
        else:
            raise InvalidOptionError

    def validate(self) -> str | None:
        if self.god_id is None:
            return "Must specify a god using the -g or --god option."
        if self.level > 20 or self.level < 1:
            return "Level must be between 1 and 20"
        return None


class SmiteleGameContext:
    """A class for holding Discord context for a Smitele Game.

    SmiteleGameContext holds all of the contextual (non-game) objects
    required for a game session to be ran. This includes the player (discord.Member)
    and the discord.TextChannel where the game was initiated.

    Attributes:
        player: A discord.Member which represents the user for a SmiteleGame
        channel: A discord.TextChannel which represents the channel where a game is running
    """

    player: discord.Member
    channel: discord.TextChannel

    def __init__(self, player: discord.Member, channel: discord.TextChannel) -> None:
        """Inits SmiteleGameContext with a player and a channel"""
        self.player = player
        self.channel = channel

    def __key(self) -> Tuple[int, int]:
        """Internal method for returning a hash key"""
        return (self.player.id, self.channel.id)

    def __hash__(self) -> int:
        """Internal method for hashing a SmiteleGameContext"""
        return hash(self.__key())

    def __eq__(self, other) -> bool:
        """Internal method for equality comparison"""
        if isinstance(other, SmiteleGameContext):
            # pylint: disable=protected-access
            return self.__key() == other.__key()
        raise NotImplementedError


class _SmiteleRoundContext:
    """A helper context object for controlling round state.

    This class is used for referencing the context of a given round, allowing
    for easier access to some shared state on a round by round basis.

    Attributes:
        TOTAL_ROUNDS: A constant indicating the total number of rounds in a game.
        file_bytes: If a file is to be attached to this round's output, this represents its bytes.
        file_name: If a file is to be attahced to this round's output, this represents its name.
        round_number: The round that this context represents.
    """

    file_bytes: io.BytesIO
    file_name: str
    round_number: int

    total_rounds: int

    def __init__(self, total_rounds: int) -> None:
        """Inits _SmiteleRoundContext given a number of rounds"""
        self.total_rounds = total_rounds

    def has_file(self) -> bool:
        """Checks whether this round context has a file.

        This method checks if a file is attached to this round's context.

        Returns:
            A boolean indicating whether a file is attached.
        """
        return self.file_bytes is not None and self.file_name is not None

    def is_last_round(self) -> bool:
        """Checks whether this is the last round.

        This method checks if this round context represents the final round of the game.

        Returns:
            A boolean indicating whether this is the final round's context.
        """
        return self.round_number == self.total_rounds

    def reset_file(self):
        self.file_bytes = None
        self.file_name = None


class SmiteleGame:
    """A class for holding all information about a running Smitele Game.

    SmiteleGame is a class for holding onto a combination of Discord context
    and Smitele specific context. The Discord context indicates who and where the
    game is being played, while the Smitele specific context hosts the answer for
    the current game.

    Attributes:
        context: A SmiteleGameContext object holding Discord related context
        god: A God object which indicates the answer to this particular game
    """

    choices: List[Tuple[God, bool]]
    context: SmiteleGameContext
    current_round: _SmiteleRoundContext
    easy_mode: bool = False
    god: God
    skin: Skin
    __tasks: Set[asyncio.Task]

    def __init__(
        self, answer: God, context: SmiteleGameContext, provider=None
    ) -> None:
        """Inits SmiteleGame given an answer God and context"""
        self.god = answer
        self.context = context
        # The game runs for minutes across six rounds, each of which needs the
        # catalogue the answer came from. Captured here rather than resolved
        # per round, so a guild changing its default game mid-game cannot swap
        # the answer's roster out from under a running session.
        self.provider = provider
        self.__tasks = set()

    def generate_easy_mode_choices(self, gods: List[God]) -> None:
        self.easy_mode = True
        self.choices = [(god, False) for god in random.sample(gods, k=6)]
        self.choices.insert(random.randint(1, len(self.choices) - 1), (self.god, False))

    def cancel(self) -> None:
        """Cancels a running SmiteleGame.

        This method cancels a running SmiteleGame by ending its related
        sub-tasks.
        """
        for task in self.__tasks:
            if task is not None and not task.done():
                task.cancel()

    def get_session_id(self) -> int:
        """Fetches a session ID for the SmiteleGame.

        This method returns a game session ID, which corresponds to a hash
        of the context.

        Returns:
            An integer session ID
        """
        return hash(self.context)

    def add_task(self, task: asyncio.Task) -> asyncio.Task:
        """Adds a task to the running SmiteleGame.

        This method appends a task to the running SmiteleGame, allowing a
        sub-task to be canceled if the game is also canceled.

        Args:
            task: An asyncio.Task to be appended to this game's task list

        Returns:
            The task that was added to the list
        """
        self.__tasks.add(task)
        return task


class Smitele(commands.Cog):
    """SmiteleBot implements wrapped Discord and Hirez functionality.

    This is the main class for implementing SmiteleBot. It has methods for running
    a complete game of Smitele. It's wrapped around a Discord bot, and implements
    functionality for listening and responding to messages.

    Attributes:
        ABILITY_IMAGE_FILE:
            Used in Round 4, this is the file name for the
            ability icon that will be saved and shown.
        BUILD_IMAGE_FILE: Used in Round 2, this is the build that gets scraped from recent matches.
        CONFIG_FILE:
            Default config file name, this stores your
            Discord token, Hirez Dev ID, and Hirez Auth Key.
        GOD_IMAGE_FILE: Used throughout, this pulls the default card art for the god.
        GOD_CROP_IMAGE_FILE: This is a cropped image of the god card art used in Round 6.
        GODS_FILE: This is a cached version of the getgods route through the Hirez API.
        ITEMS_FILE: This is a cached version of the getitems route through the Hirez API.
        SKIN_IMAGE_FILE: Used in Round 1, this is the full image of the skin that then gets cropped.
        SKIN_CROP_IMAGE_FILE: The cropped image of the skin mentioned above.
        SMITE_PATCH_VERSION_FILE: A cached version of the current patch, checked at launch.
        VOICE_LINE_FILE: This is the default file name for where the voice line in Round 3 is saved.
    """

    ABILITY_IMAGE_FILE: str = "ability.jpg"
    BUILD_IMAGE_FILE: str = "build.jpg"
    BUILD_PATH_IMAGE_FILE: str = "build_path.png"
    CONFIG_FILE: str = paths.CONFIG_FILE
    GOD_IMAGE_FILE: str = "god.jpg"
    GOD_CROP_IMAGE_FILE: str = "godCrop.jpg"
    SKIN_IMAGE_FILE: str = "skin.jpg"
    SKIN_CROP_IMAGE_FILE: str = "crop.jpg"
    VOICE_LINE_FILE: str = "voice.ogg"
    # Hi-Rez serves item icons at 128px. This was 96, so every tile was
    # downscaled on the way in and then upscaled again by Discord fitting the
    # embed width — which is what made the build image look soft.
    BUILD_TILE_SIZE: int = 128
    # The name above is what Discord shows on the attachment; this is where the
    # file actually lands, which has to be somewhere writable.
    VOICE_LINE_PATH: str = paths.data_file("voice.ogg")

    __bot: commands.Bot

    __gods: Dict[GodId, God]

    # Cached config values
    __config: dict = None

    __items: Dict[int, Item]

    # Mapping of session IDs to running games
    __running_sessions: Dict[int, SmiteleGame]

    __smite_client: SmiteProvider

    __tree_builder: ItemTreeBuilder

    __dataframe_refresher_running: bool

    __status_server: StatusServer

    __recommender: object

    __recommender_stamp: float

    # A helper lambda for hitting a random Smite wiki voicelines route
    __get_base_smite_wiki: Callable[[commands.Cog, str], str] = (
        lambda self, name: f"https://smite.fandom.com/wiki/{name}_voicelines"
    )

    def __init__(
        self,
        _bot: commands.Bot,
        _providers: Providers,
        _settings: GuildSettings = None,
        _links: LinkedPlayers = None,
    ) -> None:
        # Setting our intents so that Discord knows what our bot is going to do
        self.__bot = _bot
        self.providers = _providers
        self.settings = _settings or GuildSettings()
        self.links = _links or LinkedPlayers()
        self.__running_sessions = {}
        self.__dataframe_refresher_running = False
        self.__status_server = None
        # game -> (recommender, model mtime). Per game because the two have
        # disjoint vocabularies, and stamped because the trainer replaces the
        # file weekly.
        self.__recommender = {}

        if self.__config is None:
            self.__config = credentials.load("discordToken")

    def provider(self, ctx, game: str = "") -> GameProvider:
        """The provider one interaction is about.

        Resolved per interaction rather than bound at construction: two gods
        called Anubis exist, and which one a command means depends on the
        `game:` option and the guild's default, neither of which is known when
        the cog is built.
        """
        return self.providers.for_ctx(ctx, game)

    @property
    def __smite(self) -> SmiteProvider:
        """The Smite 1 provider specifically.

        For the owner commands that report on Hi-Rez itself — quota used,
        corpus shape — which are about that API rather than about a game.
        """
        return self.providers[Game.SMITE]

    def tree_builder(self, provider: GameProvider) -> ItemTreeBuilder:
        return ItemTreeBuilder(provider.items)

    @property
    def active_sessions(self) -> int:
        """Smite-le games currently in progress. Read by the deploy guard."""
        return len(self.__running_sessions)

    @property
    def is_ready(self) -> bool:
        """Whether the god/item caches are populated, i.e. games can start.

        Every registered provider has to be loaded, not just one: a half-ready
        bot would answer for one game and fail for the other, and the deploy
        guard reads this to decide whether the new pod can take over.
        """
        return all(
            any(provider.gods) and any(provider.items) for provider in self.providers
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        activity = discord.Game(name="Smite", type=3)
        await self.__bot.change_presence(
            status=discord.Status.online, activity=activity
        )

        if not self.__dataframe_refresher_running:
            for provider in self.providers:
                self.__bot.loop.create_task(provider.load_dataframe())
            self.__dataframe_refresher_running = True

        # on_ready fires again on every gateway reconnect, so the port is only
        # ever bound once.
        if self.__status_server is None:
            trivia: SmiteTrivia = self.__bot.get_cog("SmiteTrivia")
            self.__status_server = StatusServer(
                session_counts={
                    "smitele": lambda: self.active_sessions,
                    "trivia": (lambda: trivia.active_rounds) if trivia else (lambda: 0),
                },
                ready=lambda: self.is_ready,
                port=int(os.environ.get("SMITELE_STATUS_PORT", DEFAULT_STATUS_PORT)),
            )
            await self.__status_server.start()

        print("Smite-le Bot is ready!")

    @commands.slash_command(
        name="top_fun",
        description="Figure out who's having top fun",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    async def top_fun(self, ctx: discord.ApplicationContext):
        if ctx.user.voice is None:
            await ctx.respond(
                embed=discord.Embed(
                    color=discord.Color.yellow(),
                    description="Nobody is having Top Fun.",
                )
            )

        channel = ctx.user.voice.channel

        member = random.choice(list(channel.voice_states.keys()))

        await ctx.respond(
            embed=discord.Embed(
                color=discord.Color.yellow(),
                description=f"<@{member}> is currently having Top Fun.",
            )
        )

    @commands.slash_command(
        name="build",
        description="Get a Smite build based on winning builds",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    @discord.option(
        name="god_name",
        type=str,
        description="The god to build; taken from your live match if you leave it out",
        default="",
        autocomplete=god_autocomplete,
    )
    @discord.option(
        name="match_queue",
        type=str,
        description="The mode to build for; defaults to Conquest",
        choices=queue_choices(),
        default="",
    )
    @discord.option(
        name="role",
        type=str,
        description="The Conquest role; defaults to where this god is played most",
        choices=[p.value.title() for p in list(PlayerRole)],
        default="",
    )
    @discord.option(
        name="enemies",
        type=str,
        description="Comma-separated enemy gods; read from your live match if you've linked",
        default="",
    )
    @discord.option(
        name="allies",
        type=str,
        description="Comma-separated allied gods, so you don't duplicate the front line",
        default="",
    )
    @discord.option(
        name="high_mmr",
        type=bool,
        description="Whether to limit build search results to high MMR players (2000+)",
        default=False,
    )
    @discord.option(
        name="source",
        type=str,
        description="Where the build comes from; defaults to match data, falling back to the stat model",
        choices=["matches", "stats"],
        default="",
    )
    @game_option
    async def build(
        self,
        ctx: discord.ApplicationContext,
        god_name: str,
        match_queue: str,
        role: str,
        enemies: str,
        allies: str,
        high_mmr: bool,
        source: str,
        game: str,
    ):
        """One build command, which picks how to answer rather than asking.

        There were three. `/build` read the corpus, `/optimize` read the item
        catalogue, `/edge` read a trained model, and a user had to know which
        question they were asking before they could ask it — while the corpus
        one had no answer at all for a god nobody has played lately, and the
        catalogue one had the best presentation.

        So the choice is made here instead of by the person typing. Match data
        when there is any, the stat model when there is not, and `source:` for
        anyone who wants to force it.
        """
        provider = self.provider(ctx, game)
        build_options = BuildOptions(
            build_type=BuildCommandType.ML, provider=provider
        )

        # The lobby is read before anything else, because it can answer the one
        # question this command cannot do without. Mid-game, the god you are
        # playing is the single most tedious thing to retype and the one the
        # bot is most able to just know.
        lobby = None
        if not god_name or not (enemies or allies):
            lobby = await self.__lobby_for(ctx, provider)

        if not god_name and lobby is not None and lobby.own_god_id is not None:
            found = provider.gods.get(lobby.own_god_id)
            if found is not None:
                god_name = found.name

        if not god_name:
            await self.__send_invalid(
                ctx,
                "You aren't currently in a game so I can't give you a build "
                "without a god!",
            )
            return

        try:
            build_options.set_option("-g", god_name)
        except InvalidOptionError:
            await self.__send_invalid(
                ctx,
                f"{god_name} is not a God.",
            )
            return

        # The choice list spans both games, because Discord fixes a command's
        # choices at registration and cannot narrow them once a game is picked.
        # Naming a mode the chosen game does not have is therefore a normal
        # mistake to make, and gets a normal answer.
        #
        # An unset mode means Conquest rather than "every mode at once". See
        # ALL_MODES for what that default cost; the old behaviour is still
        # reachable by asking for it.
        named_a_mode = bool(match_queue) and match_queue != ALL_MODES
        try:
            if named_a_mode:
                build_options.set_option("-q", match_queue)
            elif match_queue != ALL_MODES:
                # `high_mmr` only exists in ranked, so an unset mode there means
                # Ranked Conquest rather than the normal one. Decided here, with
                # the rest of the mode defaulting, instead of being patched on
                # afterwards — the lane default below reads the queue.
                build_options.set_option(
                    "-q", "RANKED_CONQUEST" if high_mmr else "CONQUEST"
                )
        except InvalidOptionError:
            await self.__send_invalid(
                ctx,
                f"**{match_queue}** isn't a "
                f"{provider.game.display_name} game mode.",
            )
            return

        if role is not None and role != "":
            build_options.set_option("-r", role)
        else:
            # No lane either, so answer for the one this god is actually played
            # in. Summing across lanes averages a support build with a mid one
            # and recommends neither; the description names whichever was
            # chosen, so the reader is never guessing which question was
            # answered.
            lane = common_lane(provider, build_options)
            if lane:
                build_options.set_option("-r", lane)

        for option, value, label in (("-e", enemies, "enemies"), ("-a", allies, "allies")):
            if not value:
                continue
            try:
                build_options.set_option(option, value)
            except InvalidOptionError:
                await self.__send_invalid(
                    ctx, f"I don't know one of those {label}: `{value}`."
                )
                return

        # Nobody typed a lobby, so use the one already read above. This is the
        # whole point of /link: naming five enemies by hand is more work than
        # most people will do mid-game, which is why the option existed for a
        # year and was commented out. Silent on every failure — no linked
        # account, not in a match, the service refusing us — because a build
        # without a matchup is the answer this command has always given.
        if lobby is not None and not enemies and not allies:
            build_options.enemies = lobby.enemies or None
            build_options.allies = lobby.allies or None

        if high_mmr:
            if build_options.queue_id is None:
                build_options.set_option("-q", "RANKED_CONQUEST")

            build_options.set_option("-mmr", None)

        error_msg = build_options.validate()

        if error_msg is not None:
            await self.__send_invalid(ctx, error_msg)
            return

        god_builder = GodBuilder(provider.gods, provider.items, provider)

        async with ctx.channel.typing():
            await ctx.respond(
                embed=discord.Embed(
                    color=discord.Color.yellow(),
                    title=(
                        "Reading your live match for a build."
                        if lobby is not None
                        else "Finding you a build with those settings."
                    ),
                )
            )
            try:
                generated = await self.__build_from(
                    god_builder, build_options, source
                )
            except BuildFailedError:
                await self.__send_invalid(
                    ctx,
                    "Couldn't put a build together for that. Try being less specific?",
                )
                return

            await self.__send_generated_build(
                generated,
                ctx,
                provider.gods[build_options.god_id],
                provider=provider,
                no_god_specified=build_options.was_random_god(),
                no_god_specified_override="(You didn't input a god, so I found the best choice given your other inputs)",
            )

    @commands.slash_command(
        name="random_build",
        description="Get a random Smite build!",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    @discord.option(
        name="god_name",
        type=str,
        description="The god to return a build for. If not specified, it'll be random",
        default="",
        autocomplete=god_autocomplete,
    )
    @discord.option(
        name="prioritize",
        type=str,
        description="What items to prioritize for a random build",
        choices=[p.value.lower() for p in list(BuildPrioritization)],
        default="",
    )
    @game_option
    async def random_build(
        self,
        ctx: discord.ApplicationContext,
        god_name: str,
        prioritize: str,
        game: str,
    ):
        provider = self.provider(ctx, game)
        build_options = BuildOptions(
            build_type=BuildCommandType.RANDOM, provider=provider
        )

        try:
            if god_name is not None and god_name != "":
                build_options.set_option("-g", god_name)
        except InvalidOptionError:
            await self.__send_invalid(ctx, f"{god_name} is not a God.")
            return

        if prioritize is not None and prioritize != "":
            build_options.set_option("-p", prioritize)

        error_msg = build_options.validate()

        if error_msg is not None:
            await self.__send_invalid(ctx, error_msg)
            return

        god_builder = GodBuilder(provider.gods, provider.items, provider)

        try:
            await ctx.respond(
                embed=discord.Embed(
                    color=discord.Color.yellow(),
                    title="Randomizing you the perfect build.",
                )
            )

            generated = god_builder.random(build_options)
        except BuildFailedError:
            await self.__send_invalid(
                ctx,
                f"Failed to randomize a build for "
                f"{provider.gods[build_options.god_id].name}.",
            )
            return

        await self.__send_generated_build(
            generated,
            ctx,
            provider.gods[build_options.god_id],
            provider=provider,
            no_god_specified=build_options.was_random_god(),
        )
        return


    async def __build_from(self, god_builder, build_options, source: str):
        """A build from whichever engine can answer, preferring match data.

        The fallback is the reason this is not just `source:` with a default.
        `/build` used to fail outright for a god nobody has played recently, or
        in a lane nobody plays them in, or on a game whose corpus is still thin
        — while `/optimize` sat next to it able to answer all three, if only
        the user knew to ask it. Falling through is what makes the merged
        command strictly better than the two it replaces rather than the union
        of their gaps.
        """
        if source == "stats":
            build_options.build_type = BuildCommandType.OPTIMIZE
            return await god_builder.optimize(build_options)

        try:
            return god_builder.ml(build_options)
        except BuildFailedError:
            if source == "matches":
                # They asked for match data specifically, so "there isn't any"
                # is the answer rather than something to paper over.
                raise
            build_options.build_type = BuildCommandType.OPTIMIZE
            return await god_builder.optimize(build_options)

    async def __lobby_for(self, ctx, provider):
        """The live match of whoever ran this command, if we can find one.

        Costs two calls against someone else's service, so it only runs for a
        user we already know the handle of — an explicit `/link`, or the
        built-in roster. Returns None for every other outcome, including every
        failure, and never raises.
        """
        handle = self.links.handle_for(
            getattr(ctx.author, "id", None), provider.game
        )
        if not handle:
            return None
        try:
            return await live_lobby.lookup(provider, handle)
        except Exception as error:  # noqa: BLE001
            print(f"live lobby lookup failed: {error}", flush=True)
            return None

    @commands.slash_command(
        name="link",
        description="Tell the bot which account you play, so it can read your match",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    @discord.option(
        name="player",
        type=str,
        description="Your Smite 1 player name, or platform:handle for Smite 2",
        required=True,
    )
    @game_option
    async def link(
        self, ctx: discord.ApplicationContext, player: str, game: str
    ) -> None:
        """Remember this user's handle, so nothing else has to ask for it.

        Deliberately not verified against the game before storing. Checking
        costs a request against a quota, a wrong name fails visibly on the next
        command anyway, and a typo that blocks linking is worse than one that
        produces an obvious "no such player".
        """
        provider = self.provider(ctx, game)
        if not player.strip():
            await self.__send_invalid(ctx, "That's an empty name.")
            return

        self.links.link(ctx.author.id, provider.game, player)
        await ctx.respond(
            embed=discord.Embed(
                color=discord.Color.green(),
                description=(
                    f"Linked you to **{player.strip()}** on "
                    f"{provider.game.display_name}. Commands that need a player "
                    f"will use it, and `/build` will read your live match."
                ),
            ),
            ephemeral=True,
        )

    @commands.slash_command(
        name="unlink",
        description="Forget the account you linked",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    @game_option
    async def unlink(self, ctx: discord.ApplicationContext, game: str) -> None:
        """Forget one game's handle, or both when no game was named."""
        chosen = Game.from_display_name(game) if game else None
        removed = self.links.unlink(ctx.author.id, chosen)

        if removed:
            where = chosen.display_name if chosen else "both games"
            description = f"Forgotten, for {where}."
        elif roster.for_game(chosen or DEFAULT_GAME).get(ctx.author.id):
            # Being in roster.py is not something unlinking can undo, and
            # reporting a success that changed nothing would be a lie.
            description = (
                "You hadn't linked an account — you're in the bot's built-in "
                "roster, which `/unlink` can't remove. Use `/link` to override it."
            )
        else:
            description = "You hadn't linked an account."

        await ctx.respond(
            embed=discord.Embed(
                color=discord.Color.yellow(), description=description
            ),
            ephemeral=True,
        )

    @commands.slash_command(
        name="set_game",
        description="Choose which Smite this server's commands default to",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    @commands.has_permissions(manage_guild=True)
    @discord.option(
        name="game",
        type=str,
        description="The game to default to",
        choices=[g.display_name for g in Game],
        required=True,
    )
    async def set_game(self, ctx: discord.ApplicationContext, game: str) -> None:
        """Set the guild default, so nobody has to pass `game:` every time."""
        if ctx.guild_id is None:
            await self.__send_invalid(
                ctx, "There's no server here to set a default for."
            )
            return

        try:
            chosen = Game.from_display_name(game)
        except ValueError:
            await self.__send_invalid(ctx, f"{game} is not a game I know about.")
            return

        if chosen not in self.providers:
            await self.__send_invalid(
                ctx,
                f"{chosen.display_name} isn't available on this bot yet.",
            )
            return

        self.settings.set_game(ctx.guild_id, chosen)
        await ctx.respond(
            embed=discord.Embed(
                color=discord.Color.blue(),
                title=f"Now defaulting to {chosen.display_name}",
                description=(
                    "Commands in this server will answer for "
                    f"**{chosen.display_name}** unless they're given a "
                    "`game:` of their own."
                ),
            )
        )

    @commands.slash_command(
        name="smitele",
        description="Start a game of Smite-le, guessing a god from clues over six rounds",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    @discord.option(
        name="easy",
        type=bool,
        description="Show a list of gods to pick from",
        default=False,
    )
    @discord.option(
        name="god",
        type=str,
        description="Force a specific god (bot owner only)",
        default="",
        autocomplete=god_autocomplete,
    )
    @game_option
    async def smitele(
        self, ctx: discord.ApplicationContext, easy: bool, god: str, game: str
    ) -> None:
        # The game itself runs for minutes and posts to the channel rather than
        # to the interaction, so the interaction is acknowledged privately up
        # front. Without this Discord reports the command as having failed
        # three seconds in, while the game is still going.
        await ctx.respond("Starting Smite-le!", ephemeral=True)
        await self.__smitele(
            ctx, *self.__smitele_args(easy, god), game_option=game
        )

    @staticmethod
    def __smitele_args(easy: bool, god: str) -> Tuple[str, ...]:
        """Map the slash options onto the positional form __smitele parses.

        A god name arrives as separate words because the parser rejoins them
        with underscores to look up the GodId ("Baron Samedi" -> BARON_SAMEDI).
        """
        if god:
            return tuple(god.split())
        return ("easy",) if easy else ()

    @staticmethod
    def __resolve_items(
        provider: GameProvider, item_ids: List[int]
    ) -> List[Item]:
        """Item objects for the model's ids, skipping any it no longer knows.

        The corpus spans patches, so it can name items that have since been
        removed from the game and are absent from the current item list.
        """
        return [
            provider.items[item_id]
            for item_id in item_ids
            if item_id in provider.items
        ]

    # Kept without a caller, deliberately and briefly. `/edge` was the only
    # thing that loaded the model, and the merged `/build` does not score with
    # it yet because the number it produces is an uncalibrated sigmoid — it has
    # never been checked against how often those builds actually win. Deleting
    # the loader would mean rewriting it, including the mtime stamping that
    # fixed the bot serving whichever model it started with.
    def __load_recommender(self, provider: GameProvider):
        """The trained model for one game, reloaded when the trainer replaces it.

        Stamped with the file's mtime rather than loaded once: the trainer
        writes a new model weekly, and caching the first one meant the bot
        served whichever model it happened to start with until something else
        restarted it — every retrain silently ignored. A miss is retried too,
        since the bot can start before any model exists.

        Kept per game because the two have disjoint god and item vocabularies,
        so one game's model cannot score the other's builds.
        """
        directory = paths.game_model_dir(provider.game)
        try:
            stamp = os.path.getmtime(os.path.join(directory, "model.npz"))
        except OSError:
            return None

        cached, cached_stamp = self.__recommender.get(provider.game, (None, None))
        if cached is not None and stamp == cached_stamp:
            return cached

        loaded = BuildRecommender.load(directory)
        if loaded is None:
            # Keep whatever we had: a half-written model during a retrain is
            # not a reason to stop answering.
            return cached
        self.__recommender[provider.game] = (loaded, stamp)
        print(
            f"Loaded {provider.game.display_name} build model "
            f"(AUC {loaded.test_auc:.4f})",
            flush=True,
        )
        return loaded

    @commands.slash_command(
        name="stop",
        description="Stop a running game of Smite-le",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    @discord.option(
        name="session_id",
        type=str,
        description="Stop someone else's session by ID (bot owner only)",
        default="",
    )
    async def stop(self, ctx: discord.ApplicationContext, session_id: str) -> None:
        await ctx.respond("Stopping Smite-le…", ephemeral=True)
        await self.__stop(ctx, *((session_id,) if session_id else ()))

    @commands.command(
        brief="Returns the session ID for the running Smite-le game.",
        description="Returns the session ID for the running Smite-le game. "
        "This command will only function for the bot owner.",
    )
    @commands.is_owner()
    async def sessionid(self, context: commands.Context) -> None:
        game_session_id = hash(
            SmiteleGameContext(context.message.author, context.message.channel)
        )
        if game_session_id in self.__running_sessions:
            await context.message.channel.send(
                embed=discord.Embed(
                    color=discord.Color.gold(),
                    description=f"Running session ID = {game_session_id}",
                )
            )
        else:
            await context.message.channel.send(
                embed=discord.Embed(
                    color=discord.Color.red(), title="No running game session!"
                )
            )

    @commands.command(
        brief="Lists all running sessions of Smite-le.",
        description="Lists all running sessions of Smite-le, "
        "including the player who started the game. This command "
        "will only function for the bot owner.",
    )
    @commands.is_owner()
    async def sessions(self, context: commands.Context) -> None:
        if len(self.__running_sessions) == 0:
            await context.message.channel.send(
                embed=discord.Embed(
                    color=discord.Color.red(), title="No running game sessions!"
                )
            )
        else:
            output_msg = ""
            for game_session_id, game in self.__running_sessions.items():
                output_msg += (
                    f"> {game.context.player.mention} "
                    f"({game.context.channel.mention}): **{game_session_id}**\n"
                )

            await context.message.channel.send(
                embed=discord.Embed(color=discord.Color.gold(), description=output_msg)
            )

    @commands.command(
        brief="Lists Hirez API usage.",
        description="Lists resources currently exhausted according to Hirez API limitations. "
        "This command will only function for the bot owner.",
    )
    @commands.is_owner()
    async def usage(self, context: commands.Context):
        data_used = await self.__smite.get_data_used()
        if any(data_used):
            data_used = data_used[0]
        else:
            await context.channel.send(
                discord.Embed(
                    color=discord.Color.gold(),
                    title="Data Usage",
                    description="Nothing to report.",
                )
            )
            return
        desc = (
            f'Active Sessions: {data_used["Active_Sessions"]}/'
            f'{data_used["Concurrent_Sessions"]}\n'
            f'Total Requests Today: {data_used["Total_Requests_Today"]}/'
            f'{data_used["Request_Limit_Daily"]}\n'
            f'Total Sessions Today: {data_used["Total_Sessions_Today"]}/'
            f'{data_used["Session_Cap"]}\n'
        )
        await context.channel.send(
            embed=discord.Embed(
                color=discord.Color.gold(), title="Data Usage", description=desc
            )
        )

    @commands.command(
        brief="Lists DataFrame memory footprint",
        description="Lists current size and memory footprint of player_matches DataFrame. "
        "This command will only function for the bot owner.",
    )
    @commands.is_owner()
    async def data_info(self, context: commands.Context):
        desc = ""

        if self.__smite.player_matches is None:
            desc = "player_matches is not yet initialized."
        else:
            buffer = io.StringIO()
            self.__smite.player_matches.info(buf=buffer)
            desc = buffer.getvalue()
        await context.channel.send(
            embed=discord.Embed(
                color=discord.Color.gold(),
                title="Player Matches DataFrame Info",
                description=desc,
            )
        )

    @commands.command(
        brief="Resigns the running Smite-le game.",
        description="Resigns the player's current running Smite-le game. "
        "This command will return the answer for the game.",
    )
    async def resign(self, message: discord.Message, *args: tuple) -> None:
        game_session_id = hash(SmiteleGameContext(message.author, message.channel))
        if game_session_id in self.__running_sessions:
            session = self.__running_sessions[game_session_id]
            await self.__send_incorrect("Round Resigned!", True, session)
            self.__try_stop_running_game_session(game_session_id)
        else:
            await message.channel.send(
                embed=discord.Embed(
                    color=discord.Color.red(), title="No running game session!"
                )
            )

    @commands.command(
        aliases=["quit"],
        brief="Closes Smite-le Bot.",
        description="Shuts down Smite-le Bot gracefully. This command "
        "can only be used by the bot owner.",
    )
    @commands.is_owner()
    async def shutdown(self, message: discord.Message) -> None:
        await message.channel.send(
            embed=discord.Embed(
                color=discord.Color.gold(),
                description=f"Closing {self.__bot.user.mention}",
            )
        )
        await self.__bot.change_presence(status=discord.Status.offline)
        await self.__bot.close()

    @commands.command(
        brief="Triggers the bot to join call and play a cheeky noise.",
        description="Using this command will trigger the bot to join the sender's "
        'audio channel and play the audio clip "Cry More" from Cabrakan\'s Nerd Rage skin.',
    )
    async def crymore(self, context: commands.Context):
        cry_more_url = (
            "https://static.wikia.nocookie.net/smite_gamepedia/"
            "images/3/3e/Nerd_Rage_Cabrakan_Other_S.ogg/revision/latest?cb=20170325002129"
        )
        cry_more_file = "crymore.ogg"
        async with aiohttp.ClientSession() as client:
            async with client.get(cry_more_url) as res:
                # If the current player is in a voice channel,
                # connect to it and play the voice line!
                if context.author.voice is not None:
                    with open(cry_more_file, "wb") as voice_file:
                        voice_file.write(await res.content.read())
                    voice_client = await context.author.voice.channel.connect()

                    async def disconnect():
                        await voice_client.disconnect()
                        os.remove(cry_more_file)

                    voice_client.play(
                        discord.FFmpegPCMAudio(source=cry_more_file),
                        after=lambda _: asyncio.run_coroutine_threadsafe(
                            coro=disconnect(), loop=voice_client.loop
                        ).result(),
                    )

    @commands.command(brief="Swog.", description="Swog.")
    async def swog(self, context: commands.Context):
        await context.channel.send(
            embed=discord.Embed(
                color=discord.Color.blue(), title="You've got the wrong bot."
            )
        )

    @staticmethod
    def __parse_opts(args: List[str]) -> Generator[Tuple[str, str], None, None]:
        idx = 0
        while idx < len(args):
            arg = args[idx]
            if arg.startswith("-"):
                eq_idx = None
                try:
                    eq_idx = arg.index("=")
                except ValueError:
                    pass
                option = arg[:eq_idx]
                delimiter = arg[eq_idx + 1] if eq_idx is not None else ""
                value = ""
                if delimiter in ("'", '"'):
                    end_char = ""
                    inner_idx = idx
                    while end_char != delimiter:
                        end_char = args[inner_idx][-1]
                        start = 0 if inner_idx != idx else eq_idx + 2
                        if end_char == delimiter:
                            value += (
                                f'{"" if inner_idx == idx else " "}'
                                f'{args[inner_idx][start:-1].replace(delimiter, "")}'
                            )
                            yield (option, value)
                            idx = inner_idx + 1
                            continue
                        value += f'{"" if inner_idx == idx else " "}{args[inner_idx][start:]}'
                        inner_idx += 1
                    continue
                if eq_idx is not None:
                    value = arg[eq_idx + 1 :]
                elif len(args) > idx + 1:
                    if not args[idx + 1].startswith("-"):
                        value = args[idx + 1]
                        idx += 1
                    else:
                        value = None
                else:
                    value = None
                yield (option, value)
            idx += 1

    def __parse_god_opts(self, args: List[str], provider) -> GodOptions:
        god_options = GodOptions(provider.items, provider)
        for option, value in self.__parse_opts(args):
            god_options.set_option(option, value)
        return god_options

    @commands.command(
        aliases=["i"],
        brief="Fetches information about a given item.",
        description="Given an item name, this command fetches and returns information about the item.",
        usage="item name (required)\n\nExample Usage:\n\n$item breastplate of valor\n",
    )
    async def item(self, message: discord.Message, *args: tuple):
        async def send_invalid(additional_info: str = ""):
            desc = (
                f"Invalid command! {self.__bot.user.mention} "
                "accepts the command `$item item name` or `$i item name `"
            )
            if additional_info != "":
                desc = additional_info
            await message.channel.send(
                embed=discord.Embed(color=discord.Color.red(), description=desc)
            )

        flatten_args = ["".join(arg) for arg in args]

        if not any(flatten_args):
            await send_invalid("No item name provided!")
            return

        provider = self.provider(message)
        item_name = " ".join(flatten_args).lower()
        item: Item | None = None

        for i in provider.items.values():
            if i.name.lower() == item_name:
                item = i
        if item is None:
            await send_invalid(f"{item_name} is not an item!")
            return
        async with message.channel.typing():
            item_embed = discord.Embed(
                color=discord.Color.blue(), title=f"{item.name} Info:"
            )
            item_embed.set_thumbnail(url=item.icon_url)

            stats = "\n"
            if item.type == ItemType.ITEM:
                if not item.active:
                    stats += "**Inactive Item** ❌\n\n"
                elif item.is_starter:
                    stats += "**Starter Item** 1️⃣\n\n"
                elif item.glyph:
                    stats += "**Glyph** ⬆️\n\n"

                for prop in item.item_properties:
                    stats += (
                        f"**{prop.attribute.display_name}**: "
                        f"{int(prop.flat_value or (prop.percent_value * 100))}"
                        f'{"%" if prop.percent_value is not None else ""}\n'
                    )
                if any(item.restricted_roles):
                    stats += (
                        "\n**Can't Build On**:\n"
                        + ", ".join(
                            [
                                f"_{role.name.title()}s_"
                                for role in item.restricted_roles
                            ]
                        )
                        + "\n"
                    )

            header = "**Passive**:\n" if item.type == ItemType.ITEM else ""
            if item.passive is not None and item.passive != "":
                stats += f"\n{header}_{item.passive}_\n"
            elif item.aura is not None and item.aura != "":
                stats += f"\n**Aura**:\n_{item.aura}_\n"
            elif item.description is not None and item.description != "":
                stats += f"\n_{item.description}_\n"

            item_embed.add_field(
                name=f"{item.type.name.title()} Properties:", value=stats
            )

            total_cost = compute_item_price(item, provider.items)
            item_embed.add_field(
                name="Cost:",
                value=f"**Total Cost**: {total_cost:,}\n**Upgrade Cost**: {item.price:,}",
            )

            if item.type == ItemType.ITEM and item.active:
                tree_builder = self.tree_builder(provider)
                with await tree_builder.generate_build_tree(item) as tree_image:
                    file = discord.File(tree_image, filename="tree.png")
                    item_embed.set_image(url="attachment://tree.png")
                    await message.channel.send(file=file, embed=item_embed)
                    return
            else:
                await message.channel.send(embed=item_embed)

    @commands.command(
        aliases=["g"],
        brief="Fetches information about a given god or goddess.",
        description="Given a god or goddess, this command will return the god's stats, "
        "along with other information based on the provided options.",
        usage="[options]\n\nOptions:\n\t**-g (--god)** [Required] - The name of the god or goddess"
        "\n\t**-b (--build)** - A comma separated list of items (IDs or names) to compute god/goddess stats with"
        "\n\t**-l (--level)** - The level to compute stats at"
        "\n\t**-ia (--include-abilities)** - No arguments, prints out ability information if provided.\n\n"
        "Example Usage:\n\n$god --god='Yu Huang' --build='Evolved Book of Thoth, Soul Reaver' --level 15 --include-abilities\n",
    )
    async def god(self, message: discord.Message, *args: tuple):
        async def send_invalid(additional_info: str = ""):
            desc = (
                f"Invalid command! {self.__bot.user.mention} "
                "accepts the command `$god -g god -b='comma "
                "separated build (IDs or names)' -l level`"
            )
            if additional_info != "":
                desc = additional_info
            await message.channel.send(
                embed=discord.Embed(color=discord.Color.red(), description=desc)
            )

        flatten_args = ["".join(arg) for arg in args]

        if not any(flatten_args):
            await send_invalid()
            return

        god_options = self.__parse_god_opts(flatten_args, provider)
        god = provider.gods[god_options.god_id]

        def check_invalid_item(item: Item) -> bool:
            if all(
                [
                    p.attribute.god_type is not None
                    and p.attribute.god_type != god.type
                    for p in item.item_properties
                ]
            ):
                return True
            # Odysseus' Bow
            if item.id == 10482 and god.type == GodType.MAGICAL:
                return True
            # Magic Acorn
            if item.root_item_id == 18703 and god.id != GodId.RATATOSKR:
                return True
            if god.role in item.restricted_roles:
                return True
            return False

        def check_invalid_build(items: List[Item]) -> bool:
            if any(filter(check_invalid_item, items)):
                return True
            glyph_count = 0
            starter_count = 0
            acorn_count = 0

            for i in items:
                if i.glyph:
                    glyph_count += 1
                if i.is_starter:
                    starter_count += 1
                if i.root_item_id == 18703:
                    acorn_count += 1
            return glyph_count > 1 or starter_count > 1 or acorn_count > 1

        if check_invalid_build(god_options.build):
            await send_invalid(f"Build contained an item that {god.name} cannot build.")
            return

        god_embed = discord.Embed(
            color=discord.Color.blue(),
            title=f"{god.name} Stats @ Level {god_options.level}:",
        )

        god_embed.set_thumbnail(url=god.icon_url)

        stats = ""
        for attr in list(ItemAttribute):
            stat_at_level = god.get_stat_at_level(attr, god_options.level)
            if stat_at_level == 0:
                continue
            stats += f"**{attr.display_name}**: {stat_at_level:g}\n"

        god_embed.add_field(name="Base Attributes:", value=stats, inline=True)

        def basic_attack(base: float, per_level: float, scaling: float):
            return (
                f"{(base + ((god_options.level - 1) * per_level)):g} "
                f"@ Level {god_options.level} "
                f"(+{int(scaling * 100)}% of "
                f"{god.type.name.title()} Power)"
            )

        basic_attrs = basic_attack(
            god.stats.basic_attack.base_damage,
            god.stats.basic_attack.per_level,
            god.stats.basic_attack.scaling,
        )

        if god.stats.basic_attack.base_damage_back > 0:
            back = basic_attack(
                god.stats.basic_attack.base_damage_back,
                god.stats.basic_attack.per_level_back,
                god.stats.basic_attack.scaling_back,
            )
            basic_attrs += f" out, {back} back"

        god_embed.add_field(
            name="Basic Attack Attributes:", value=basic_attrs, inline=True
        )

        def get_role_emoji(role: GodRole):
            if role == GodRole.ASSASSIN:
                return "🗡️"
            if role == GodRole.GUARDIAN:
                return "🛡️"
            if role == GodRole.HUNTER:
                return "🏹"
            if role == GodRole.MAGE:
                return "🪄"
            if role == GodRole.WARRIOR:
                return "⚔️"
            return "❓"

        additional_info = (
            f"**Role**: {god.role.name.title()} {get_role_emoji(god.role)}\n"
            f"**Range**: {god.range.name.title()}\n"
            f"**Title**: {god.title}\n"
            f"**Pantheon**: {god.pantheon}\n"
            f'**Pro{"s" if len(god.pros) > 1 else ""}**: '
            f'{", ".join(p.name.replace("_", " ").title() for p in god.pros)}\n'
        )
        god_embed.add_field(name="Additional Info:", value=additional_info)

        if any(god_options.build):
            optimizer = BuildOptimizer(god, [], provider.items)
            god_embed.add_field(
                name="Build Attributes:",
                value=optimizer.get_build_stats_string(
                    god_options.build, level=god_options.level
                ),
                inline=True,
            )
            with await self.__make_build_image(god_options.build) as file:
                dfile = discord.File(file, filename=self.BUILD_IMAGE_FILE)
                god_embed.set_image(url=f"attachment://{self.BUILD_IMAGE_FILE}")
                await message.channel.send(file=dfile, embed=god_embed)
        else:
            await message.channel.send(embed=god_embed)

        if not god_options.include_abilities:
            return

        for idx, ability in enumerate(god.abilities):
            passive = " (Passive)" if idx == 4 else ""
            ability_embed = discord.Embed(
                color=discord.Color.blue(),
                title=f"{god.name} - {ability.name}{passive}",
            )
            ability_embed.set_thumbnail(url=ability.icon_url)

            desc = f"_{ability.description}_\n"
            for prop in ability.ability_properties:
                desc += f"\n**{prop.name}** - {prop.value}"

            ability_embed.add_field(name="Description:", value=desc)

            rank = ""
            for prop in ability.rank_properties:
                rank += f"**{prop.name}** - {prop.rank_values}\n"
            if rank != "":
                ability_embed.add_field(name="Properties:", value=rank)

            if any(ability.cooldown_by_rank):
                cooldown = "/".join([f"{int(c):,}" for c in ability.cooldown_by_rank])
                ability_embed.add_field(name="Cooldown:", value=cooldown)

            if any(ability.cost_by_rank):
                modifier = ability.cost_modifier
                cost = "/".join([f"{int(c):,}" for c in ability.cost_by_rank])
                ability_embed.add_field(
                    name="Cost:", value=f'{cost} {modifier or "Mana"}'
                )

            await message.channel.send(embed=ability_embed)

    def start_bot(self) -> None:
        """
        Using this command instead of just calling self.run() since the discordToken is loaded
        as part of this class.
        """
        self.__bot.run(self.__config["discordToken"])

    async def __send_generated_build(
        self,
        generated: GeneratedBuild,
        ctx: discord.ApplicationContext,
        god: God,
        provider: GameProvider = None,
        no_god_specified: bool = False,
        no_god_specified_override: str = None,
    ):
        build, relics, extended_desc, aspect, path = generated
        game = provider.game if provider is not None else Game.SMITE
        cache_parts = paths.game_cache_parts(game)
        # A forked path is drawn rather than described. An embed has one image
        # slot, so this replaces the grid; anything that goes wrong falls back
        # to it, since a build without a diagram beats no build at all.
        path_bytes = None
        if path is not None:
            try:
                path_bytes = await build_path_image.render(
                    path,
                    relics or [],
                    extras_label="RELICS"
                    if game is Game.SMITE
                    else "STARTER + RELIC",
                )
            except Exception as ex:  # pylint: disable=broad-except
                print(f"Could not draw the build path: {ex}")

        with await self.__make_build_image(build) as build_bytes:
            desc = f"Hey {ctx.user.mention}, {extended_desc}"
            file_bytes = build_bytes

            embed = discord.Embed(
                color=discord.Color.blue(),
                description=desc,
                title=f"Your {god.name} Build Has Arrived!",
            )

            if relics is not None and any(relics):
                relic_bytes = await self.__make_build_image(relics)
                build_image = Image.open(build_bytes)
                relic_image = Image.open(relic_bytes)

                output_image = Image.new(
                    "RGBA",
                    (self.BUILD_TILE_SIZE * (3 + len(relics)), self.BUILD_TILE_SIZE * 2),
                    (250, 250, 250, 0),
                )

                output_image.paste(build_image, (0, 0))
                output_image.paste(
                    relic_image,
                    (self.BUILD_TILE_SIZE * 3, self.BUILD_TILE_SIZE // 2),
                )
                file_bytes = io.BytesIO()
                output_image.save(file_bytes, format="PNG")
                file_bytes.seek(0)

            image_name = self.BUILD_IMAGE_FILE
            if path_bytes is not None:
                file_bytes = path_bytes
                image_name = self.BUILD_PATH_IMAGE_FILE

            files = [discord.File(file_bytes, filename=image_name)]
            embed.set_image(url=f"attachment://{image_name}")
            await self.__attach_thumbnail(
                embed,
                files,
                god.icon_url,
                *cache_parts,
                "gods",
                "icons",
                badge_url=aspect.icon_url if aspect is not None else None,
                badge_parts=(*cache_parts, "gods", "aspects"),
            )
            # Inline, so Discord lays these across one row instead of stacking
            # three full-width blocks. The embed was rendering tall and narrow:
            # a nearly square build image with a column of one-line fields
            # underneath it, all of it in a third of the available width.
            embed.add_field(
                name="Items",
                value=", ".join([item.name for item in build]),
                inline=True,
            )
            if relics is not None and any(relics):
                # In Smite 2 this strip is the starter and the relic. They are
                # different kinds of thing but they share a row, and calling it
                # "Relics" there would name the starter wrongly.
                embed.add_field(
                    name="Relics" if game is Game.SMITE else "Starter & Relic",
                    value=", ".join([item.name for item in relics]),
                    inline=True,
                )
            if aspect is not None:
                embed.add_field(name="Aspect", value=aspect.name, inline=True)
            if no_god_specified:
                embed.set_footer(
                    text=no_god_specified_override
                    or f"(You didn't give me a god, so I picked {god.name} for you)"
                )
            await ctx.respond(files=files, embed=embed)

    async def __attach_thumbnail(
        self,
        embed: discord.Embed,
        files: List[discord.File],
        url: str,
        *cache_parts: str,
        badge_url: str = None,
        badge_parts: Tuple[str, ...] = (),
    ) -> None:
        """Upload an icon and point the embed's thumbnail at the attachment.

        set_thumbnail(url=...) makes Discord's own proxy fetch the URL, and
        Hi-Rez's CDN does not reliably serve it — the thumbnail then silently
        renders as a broken image. Fetching it here reuses the cache and the
        URL fallback, so the bot and Discord never disagree about whether the
        art exists.

        The icon is re-encoded to PNG rather than uploaded as fetched. Embeds
        are rendered through Discord's proxy with ?format=webp, and that
        transcoder returns 415 on the JPEGs Hi-Rez serves — the attachment
        itself is a valid image and downloads fine, it just never renders.
        Re-encoding sidesteps the transcoder's opinion of the source file.
        """
        if not url:
            return

        icon = await art_cache.fetch(url, *cache_parts, url.split("/")[-1])
        if not art_cache.looks_like_image(icon.getvalue()):
            embed.set_thumbnail(url=url)
            return

        try:
            with Image.open(icon) as image:
                composed = image.convert("RGBA")
                if badge_url:
                    composed = await self.__badge_icon(
                        composed, badge_url, *badge_parts
                    )
                normalised = io.BytesIO()
                composed.save(normalised, format="PNG")
                normalised.seek(0)
        except Exception as ex:  # pylint: disable=broad-except
            print(f"Could not re-encode thumbnail {url}: {ex}")
            embed.set_thumbnail(url=url)
            return

        # Attachment names have to be unique within a message.
        name = f"thumb{len(files)}.png"
        files.append(discord.File(normalised, filename=name))
        embed.set_thumbnail(url=f"attachment://{name}")

    # How much of the god's icon the Aspect badge takes up, and how thick its
    # outline is as a fraction of the badge.
    ASPECT_BADGE_SCALE: float = 0.42
    ASPECT_BADGE_BORDER: float = 0.08

    async def __badge_icon(
        self, icon: Image.Image, badge_url: str, *cache_parts: str
    ) -> Image.Image:
        """The god's icon with an Aspect's icon set into its corner.

        One picture rather than two, because the thumbnail is the only image
        slot an embed has and the Aspect is part of which god this is: an
        Anubis with one is not the same character as an Anubis without. The
        badge is outlined so it reads as deliberate at thumbnail size, where
        two art styles butting against each other otherwise looks like a
        rendering fault.

        Failure is not fatal. A missing or undecodable Aspect icon returns the
        god's icon untouched, because a build embed without a badge is a small
        loss and no embed at all is a large one.
        """
        try:
            badge_bytes = await art_cache.fetch(
                badge_url, *cache_parts, art_cache.cache_key(badge_url)
            )
            if not art_cache.looks_like_image(badge_bytes.getvalue()):
                return icon
            with Image.open(badge_bytes) as raw:
                badge = raw.convert("RGBA")
        except Exception as ex:  # pylint: disable=broad-except
            print(f"Could not load Aspect icon {badge_url}: {ex}")
            return icon

        size = max(16, int(min(icon.size) * self.ASPECT_BADGE_SCALE))
        badge = badge.resize((size, size), Image.LANCZOS)

        # A circular mask, so the badge does not need to be square art, plus a
        # ring in the same shape to separate it from whatever it sits on.
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)

        border = max(1, int(size * self.ASPECT_BADGE_BORDER))
        ring = Image.new("RGBA", (size + border * 2, size + border * 2), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse(
            (0, 0, ring.width - 1, ring.height - 1), fill=(18, 18, 22, 235)
        )

        out = icon.copy()
        position = (
            out.width - ring.width,
            out.height - ring.height,
        )
        out.alpha_composite(ring, position)
        out.paste(badge, (position[0] + border, position[1] + border), mask)
        return out

    async def __stop(self, message: discord.Message, *args: tuple) -> None:
        game_session_id = hash(SmiteleGameContext(message.author, message.channel))

        if len(args) > 0:
            try:
                if not await self.__bot.is_owner(message.author):
                    await message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.red(),
                            title="Can't stop another player's game!",
                        )
                    )
                    return
                game_session_id = int(args[0])
            except TypeError:
                await message.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.red(), title="Invalid input for session ID"
                    )
                )
                return

        if self.__try_stop_running_game_session(game_session_id):
            await message.channel.send(
                embed=discord.Embed(
                    color=discord.Color.red(), title="Smite-le round canceled!"
                )
            )
            print(f"Game session with ID {game_session_id} was successfully stopped")
            return
        await message.channel.send(
            embed=discord.Embed(
                color=discord.Color.red(), title="No running game session!"
            )
        )

    def __try_stop_running_game_session(self, game_session_id: int) -> bool:
        if game_session_id in self.__running_sessions:
            self.__running_sessions[game_session_id].cancel()
            del self.__running_sessions[game_session_id]
            return True
        return False

    # Helper function for checking correctness
    @staticmethod
    def __check_answer_message(guess: str, answer: str) -> bool:
        guess = unidecode(guess).lower().replace("-", " ")
        return (
            guess == answer.lower()
            or edit_distance.SequenceMatcher(a=guess, b=answer.lower()).distance() <= 1
        )

    def __update_choices(self, guess: str, game: SmiteleGame) -> None:
        for idx, choice in enumerate(game.choices):
            if self.__check_answer_message(guess, choice[0].name):
                game.choices[idx] = (choice[0], True)

    # Primary command for starting a round of Smite-le!
    async def __smitele(
        self, message: discord.Message, *args: tuple, game_option: str = ""
    ) -> None:
        if message.author == self.__bot.user:
            return

        provider = self.provider(message, game_option)
        if len(provider.gods) == 0:
            desc = f"{self.__bot.user.mention} has not finished initializing."
            await message.channel.send(
                embed=discord.Embed(color=discord.Color.red(), description=desc)
            )
            return

        context = SmiteleGameContext(message.author, message.channel)
        game_session_id = hash(context)

        if game_session_id in self.__running_sessions:
            desc = (
                "Can't start another game, "
                f"**{context.player.mention}** already has a running game!"
            )
            await message.channel.send(
                embed=discord.Embed(color=discord.Color.red(), description=desc)
            )
            return

        easy_mode = False
        god_arg = None
        if any(args):

            async def send_invalid():
                desc = (
                    f"Invalid command! {self.__bot.user.mention} "
                    f"accepts the command `$smitele [easy]` (or `$st [easy]`)"
                )
                await message.channel.send(
                    embed=discord.Embed(color=discord.Color.red(), description=desc)
                )

            god_arg = provider.god_by_name(" ".join("".join(arg) for arg in args))
            if god_arg is None and len(args) > 1:
                await send_invalid()
                return
            if "".join(args[0]) == "easy":
                easy_mode = True
            elif god_arg is not None and not await self.__bot.is_owner(message.author):
                await send_invalid()
                return

        # Fetching a random god from our list of cached gods
        game = SmiteleGame(
            god_arg or random.choice(list(provider.gods.values())),
            context,
            provider=provider,
        )
        if easy_mode:
            game.generate_easy_mode_choices(list(provider.gods.values()))
        self.__running_sessions[game_session_id] = game
        try:
            await game.add_task(
                self.__bot.loop.create_task(self.__run_game_session(game))
            )
        # pylint: disable=broad-except
        except Exception:
            desc = f"{self.__bot.user.mention} encountered a fatal error. Please try again later."
            await message.channel.send(
                embed=discord.Embed(color=discord.Color.red(), description=desc)
            )
            print(f"Fatal exception encountered: {traceback.format_exc()}")
            game.cancel()
        finally:
            if game_session_id in self.__running_sessions:
                del self.__running_sessions[game_session_id]

    async def __run_game_session(self, session: SmiteleGame) -> None:
        # Fetching skins for this god, used in multiple rounds
        skins = [
            Skin.coerce(skin)
            for skin in await session.provider.get_god_skins(session.god.id)
        ]

        build_task = session.add_task(
            self.__bot.loop.create_task(self.__prefetch_build_image(session))
        )
        # The default skin is "Standard <God>" in Smite 1 and "Default" in
        # Smite 2, and a god may have no skins at all if a source is thin. A
        # bare next() raised StopIteration here and killed the session before
        # the first round; the card round degrades on its own if this is None.
        base_skin = next(
            (
                skin
                for skin in skins
                if skin.name
                in (f"Standard {session.god.name}", "Default", session.god.name)
            ),
            next((skin for skin in skins if skin.has_url), None),
        )

        round_methods: Callable[[], Coroutine[Any, Any, bool]] = [
            lambda: self.__send_god_skin(session, skins),
            lambda: self.__send_god_build(session, build_task),
            lambda: self.__send_god_voiceline(session, skins),
            lambda: self.__send_god_ability_icon(session),
            lambda: self.__send_god_title(session),
            lambda: self.__send_god_base_card(session, base_skin),
        ]

        await session.context.channel.send(
            embed=discord.Embed(
                color=discord.Color.blue(),
                title="Smite-le Started!",
                description="Name the god given the clues! You'll have "
                f"{len(round_methods)} attempts.",
            )
        )

        session.current_round = _SmiteleRoundContext(len(round_methods))
        error_rounds = 0
        for idx, method in enumerate(round_methods):
            session.current_round.round_number = idx + 1 - error_rounds
            async with session.context.channel.typing():
                try:
                    if await session.add_task(self.__bot.loop.create_task(method())):
                        return
                except IndexError:
                    error_rounds += 1
                    session.current_round.total_rounds -= 1

    @staticmethod
    def __make_placeholder_tile(name: str, size: int) -> Image.Image:
        """A labelled stand-in for an item whose icon can't be fetched."""
        tile = Image.new("RGBA", (size, size), (49, 51, 56, 255))
        draw = ImageDraw.Draw(tile)
        draw.rectangle([0, 0, size - 1, size - 1], outline=(114, 118, 125, 255))

        font = ImageFont.load_default()
        # Greedy wrap against the tile width, measured in the actual font.
        lines, line = [], ""
        for word in str(name).split():
            candidate = f"{line} {word}".strip()
            if draw.textlength(candidate, font=font) <= size - 8 or not line:
                line = candidate
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
        lines = lines[:5]

        line_height = 11
        top = max(2, (size - len(lines) * line_height) // 2)
        for index, text in enumerate(lines):
            width = draw.textlength(text, font=font)
            draw.text(
                ((size - width) / 2, top + index * line_height),
                text,
                font=font,
                fill=(220, 221, 222, 255),
            )
        return tile

    async def __make_build_image(self, build: List[Item]) -> io.BytesIO:
        # Appending the images into a single build image
        thumb_size = self.BUILD_TILE_SIZE
        with Image.new(
            "RGBA",
            (thumb_size * min(3, len(build)), thumb_size * math.ceil(len(build) / 3)),
            (250, 250, 250, 0),
        ) as output_image:
            for idx, item in enumerate(build):
                row, column = divmod(idx, 3)
                pos_x, pos_y = column * thumb_size, row * thumb_size

                # First requesting and saving the image from the URLs we got
                with await item.get_icon_bytes() as item_bytes:
                    try:
                        with Image.open(item_bytes) as image:
                            # Resize the image if necessary, Hirez doesn't return a consistent size
                            if image.size != (thumb_size, thumb_size):
                                image = image.resize((thumb_size, thumb_size))
                            if image.mode != "RGBA":
                                image = image.convert("RGBA")
                            output_image.paste(image, (pos_x, pos_y))
                    except Exception as ex:
                        # Hi-Rez has pulled the art for some older items and
                        # now serves 403 for them. A named tile keeps the build
                        # readable; a transparent gap just looks broken.
                        print(f"Unable to create an image for {item.name}, {ex}")
                        output_image.paste(
                            self.__make_placeholder_tile(item.name, thumb_size),
                            (pos_x, pos_y),
                        )

            file = io.BytesIO()
            output_image.save(file, format="PNG")
            file.seek(0)
            return file

    def __build_from_aggregate(self, session: SmiteleGame) -> List[Item]:
        """The god's most-won build, out of the corpus.

        Smite 2 has no leaderboard route to scrape a build off, but it has the
        same aggregate `/build` reads, which is a better answer anyway: the
        modal winning build rather than one arbitrary top player's last game.
        """
        stats = session.provider.build_stats
        if stats is None:
            raise IndexError("no build aggregate yet")

        starters = tuple(
            item.id for item in session.provider.items.values() if item.is_starter
        )
        best = stats.best_build(
            god_id=id_value(session.god.id),
            queue_id=None,
            role=None,
            high_mmr=False,
            require_starter=True,
            starter_ids=starters,
        )
        if best is None or not any(best["items"]):
            raise IndexError(f"no recorded build for {session.god.name}")

        build = [
            session.provider.items[i]
            for i in best["items"]
            if i in session.provider.items
        ]
        if not build:
            raise IndexError(f"no recorded build for {session.god.name}")
        return build

    async def __prefetch_build_image(self, session: SmiteleGame) -> io.BytesIO:
        if session.provider.game is not Game.SMITE:
            return await self.__make_build_image(self.__build_from_aggregate(session))

        # Index maps to position in build
        build: List[Item] = []

        # Hirez's route for getting recommended items is highly out of date, so we'll get a
        # top Ranked Conquest player's build
        god_leaderboard = await session.provider.get_god_leaderboard(
            session.god.id, QueueId.RANKED_CONQUEST
        )
        if not god_leaderboard:
            # No leaderboard route on this source. The round is dropped rather
            # than the task dying with an unretrieved IndexError.
            raise IndexError(f"no leaderboard for {session.god.name}")

        while len(build) == 0:
            # Fetching a random player from the leaderboard
            random_player = random.choice(god_leaderboard)
            god_leaderboard.remove(random_player)

            # Scraping their recent match history to try and find a current build
            match_history = await session.provider.get_match_history(
                int(random_player["player_id"])
            )
            for match in match_history:
                if len(build) != 0:
                    break
                items = [int(match[f"ItemId{i}"]) for i in range(1, 7)]
                # Get a full build for this god
                if int(match["GodId"]) == session.god.id.value and all(
                    i != 0 for i in items
                ):
                    for item_id in items:
                        # Luckily `getmatchhistory` includes build info!
                        build.append(session.provider.items[item_id])

        return await self.__make_build_image(build)

    @staticmethod
    def __random_crop(img: Image.Image, into: io.BytesIO) -> None:
        """A random square of a god's art, written as a JPEG.

        Two things the Hi-Rez CDN never made us think about, both of which the
        wiki does. Its art is often RGBA — JPEG has no alpha, and Pillow raises
        rather than dropping it, which is what killed `/smitele game:Smite 2`
        before its first round. And where Hi-Rez serves filled cards, the wiki
        serves cutout renders on transparency, so a uniformly random square is
        frequently all background: a clue with nothing in it. The crop is taken
        from inside the opaque bounding box instead, and flattened onto black.
        """
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            box = img.getchannel("A").getbbox()
            flat = Image.new("RGB", img.size, (0, 0, 0))
            flat.paste(img, mask=img.getchannel("A"))
            img = flat
        else:
            box = None
            img = img.convert("RGB")

        left, top, right, bottom = box or (0, 0, img.width, img.height)
        size = math.floor(img.width / 4.0)
        # A cutout narrower than the crop would give an empty random range; fall
        # back to the whole frame rather than fail.
        if right - left < size or bottom - top < size:
            left, top, right, bottom = 0, 0, img.width, img.height
        x = random.randint(left, right - size)
        y = random.randint(top, bottom - size)
        crop = img.crop((x, y, x + size, y + size))
        if crop.size != (180, 180):
            crop = crop.resize((180, 180))
        crop.save(into, format="JPEG", quality=95)
        into.seek(0)

    async def __send_god_skin(self, session: SmiteleGame, skins: List[Skin]) -> bool:
        # Fetching a random god skin
        skin = random.choice(list(filter(lambda s: s.has_url, skins)))
        session.skin = skin

        with await skin.get_card_bytes() as skin_image:
            with io.BytesIO() as file:
                # Cropping the skin image that we got randomly
                with Image.open(skin_image) as img:
                    self.__random_crop(img, file)

                desc = "Name the god with this skin"
                session.current_round.file_bytes = file
                session.current_round.file_name = self.SKIN_CROP_IMAGE_FILE
                return await self.__send_round_and_wait_wrapper(desc, session)

    async def __send_god_build(
        self, session: SmiteleGame, build_task: "asyncio.Task[io.BytesIO]"
    ) -> bool:
        with await build_task as file:
            desc = (
                "Hint: A top-ranked player of this god recently used this build."
                if session.provider.game is Game.SMITE
                # Smite 2's comes out of the corpus, not one player's last game.
                else "Hint: This is this god's most successful recorded build."
            )
            session.current_round.file_bytes = file
            session.current_round.file_name = self.BUILD_IMAGE_FILE
            return await self.__send_round_and_wait_wrapper(desc, session)

    async def __play_voiceline(self, session: SmiteleGame, audio: bytes) -> bool:
        """Play the clip into the player's voice channel, or upload it."""
        context = session.context
        if context.player.voice is not None:
            with open(self.VOICE_LINE_PATH, "wb") as voice_file:
                voice_file.write(audio)
            voice_client = await context.player.voice.channel.connect()

            async def disconnect():
                await voice_client.disconnect()
                os.remove(self.VOICE_LINE_PATH)

            voice_client.play(
                discord.FFmpegPCMAudio(source=self.VOICE_LINE_PATH),
                after=lambda _: asyncio.run_coroutine_threadsafe(
                    coro=disconnect(), loop=voice_client.loop
                ).result(),
            )
        else:
            with io.BytesIO(audio) as file:
                dis_file = discord.File(file, filename=self.VOICE_LINE_FILE)
                await context.channel.send(file=dis_file)

        session.current_round.reset_file()
        return await self.__send_round_and_wait_wrapper(
            "Whose voice line was that?", session
        )

    async def __send_smite2_voiceline(self, session: SmiteleGame) -> bool:
        """Smite 2's voice line round, off wiki.smite2.com.

        33 of the 88 gods have no voicelines page, and a line whose audio will
        not fetch is no better than none, so both cases raise IndexError and the
        round loop renumbers around them.
        """
        lines = await session.provider.get_god_voicelines(session.god.id)
        if not lines:
            raise IndexError(f"no voicelines page for {session.god.name}")

        random.shuffle(lines)
        async with aiohttp.ClientSession() as client:
            for line in lines:
                try:
                    async with client.get(line.url) as res:
                        if res.status != 200:
                            continue
                        audio = await res.content.read()
                except aiohttp.ClientError:
                    continue
                return await self.__play_voiceline(session, audio)

        raise IndexError(f"no playable voiceline for {session.god.name}")

    async def __send_god_voiceline(
        self, session: SmiteleGame, skins: List[Skin]
    ) -> bool:
        if session.provider.game is not Game.SMITE:
            # The scrape below targets smite.fandom.com, which is Smite 1's
            # wiki. Most of the roster exists in both games, so pointing a
            # Smite 2 round at it would not merely fail to find a page — it
            # would serve Smite 1 Anubis's line as the clue for Smite 2 Anubis.
            # wiki.smite2.com publishes its own, so Smite 2 has its own path.
            return await self.__send_smite2_voiceline(session)

        audio_src = None
        skin_copy = skins.copy()

        def remove_skin(name: str) -> List[Skin]:
            return list(filter(lambda s: s.name != name, skin_copy))

        async with aiohttp.ClientSession() as client:
            while audio_src is None:
                # Getting a random skin to fetch a random voiceline for
                skin = random.choice(skin_copy)
                page_name = ""

                # All of these correspond to just the god's name
                if skin.name in [
                    "Golden",
                    "Legendary",
                    "Diamond",
                    f"Standard {session.god.name}",
                ]:
                    page_name = session.god.name
                else:
                    page_name = f"{skin.name}_{session.god.name}"

                try:
                    # Not all skins have voiceline pages on the Smite wiki,
                    # so retry until we get one that works
                    async with client.get(
                        self.__get_base_smite_wiki(name=page_name.replace(" ", "_"))
                    ) as res:
                        if res.status != 200:
                            skin_copy = remove_skin(skin.name)
                            continue
                        smite_wiki = BeautifulSoup(
                            await res.content.read(), "html.parser"
                        )
                        # BeautifulSoup is amazing
                        audio_blocks = smite_wiki.find_all("audio")

                        # Exclude the first voice line, which is line played
                        # when the god is locked in (says their name)
                        audio_src = random.choice(audio_blocks).source.get("src")
                except (ValueError, IndexError):
                    skin_copy = remove_skin(skin.name)
            async with client.get(audio_src) as res:
                return await self.__play_voiceline(session, await res.content.read())

    async def __send_god_ability_icon(self, session: SmiteleGame) -> bool:
        saved_image = False
        # Try each ability once rather than spinning. This was `while not
        # saved_image` around a bare except, so a god whose art was entirely
        # unreachable — every Smite 2 god, before their icons were wired up —
        # looped forever printing failures and hung the game.
        candidates = [a for a in session.god.abilities if a.icon_url]
        random.shuffle(candidates)
        with io.BytesIO() as ability_bytes:
            for ability in candidates:
                if saved_image:
                    break
                try:
                    with await ability.get_icon_bytes() as file:
                        image = Image.open(file)
                        # Again, not all images that Hirez sends are a consistent size
                        if image.size != (64, 64):
                            image.thumbnail((64, 64))
                        if image.mode != "RGB":
                            image = image.convert("RGB")
                        image.save(ability_bytes, format="JPEG", quality=95)
                        ability_bytes.seek(0)
                        saved_image = True
                except Exception as ex:
                    # aiohttp isn't able to fetch for every ability image URL
                    print(
                        f"Unable to create an image for {session.god.name}'s "
                        f"{ability.name}, {ex}"
                    )
            if not saved_image:
                # IndexError is the signal the round loop already understands:
                # drop this round and renumber the rest.
                raise IndexError(
                    f"no usable ability art for {session.god.name}"
                )
            desc = "Hint: Here's one of the god's abilities"
            session.current_round.file_bytes = ability_bytes
            session.current_round.file_name = self.ABILITY_IMAGE_FILE
            return await self.__send_round_and_wait_wrapper(desc, session)

    async def __send_god_title(self, session: SmiteleGame) -> bool:
        session.current_round.reset_file()
        return await self.__send_round_and_wait_wrapper(
            f"The god has this title:\n```{session.god.title.title()}```", session
        )

    async def __send_god_base_card(self, session: SmiteleGame, base_skin: Skin) -> bool:
        with io.BytesIO() as crop_file:
            with await base_skin.get_card_bytes() as card_bytes:
                with Image.open(card_bytes) as img:
                    self.__random_crop(img, crop_file)

                desc = "Hint: This is a crop of the god's base skin"
                session.current_round.file_bytes = crop_file
                session.current_round.file_name = self.GOD_CROP_IMAGE_FILE
                return await self.__send_round_and_wait_wrapper(desc, session)

    # Loops until exp time, editing the message embed with a countdown
    async def __countdown_loop(
        self, message: discord.Message, exp: float, embed: discord.Embed
    ) -> None:
        while time.time() < exp:
            await asyncio.sleep(1)
            rem = math.ceil(exp - time.time())
            if rem >= 0:
                embed.set_field_at(
                    0,
                    name="Time Remaining:",
                    value=f'_{rem} second{"s" if rem != 1 else ""}_',
                )
                await message.edit(embed=embed)

    async def __send_incorrect(
        self, desc: str, last_round: bool, session: SmiteleGame
    ) -> None:
        answer_image: discord.File = None
        embed: discord.Embed = None
        if not last_round:
            desc += "\n\nNext round coming up shortly."
            embed = discord.Embed(color=discord.Color.red(), description=desc)
        else:
            async with session.context.channel.typing():
                desc += f" The answer was **{session.god.name}**."
                answer_image = discord.File(
                    await session.skin.get_card_bytes(),
                    filename=f"{session.god.name}.jpg",
                )
                embed = discord.Embed(color=discord.Color.red(), description=desc)
                embed.set_image(url=f"attachment://{session.god.name}.jpg")

        await session.context.channel.send(file=answer_image, embed=embed)

    def __send_round_and_wait_wrapper(
        self, description: str, session: SmiteleGame
    ) -> "asyncio.Task[bool]":
        return session.add_task(
            self.__bot.loop.create_task(
                self.__send_round_and_wait_for_answer(description, session)
            )
        )

    # Helper function for sending the repeated round messages to Discord
    async def __send_round_and_wait_for_answer(
        self, description: str, session: SmiteleGame
    ) -> bool:
        context = session.context
        round_ctx = session.current_round

        embed = discord.Embed(color=discord.Color.blue(), description=description)
        embed.title = f"Round {round_ctx.round_number}:"
        embed.add_field(name="Time Remaining:", value="_20 seconds_")
        if len(self.__running_sessions) > 1:
            embed.add_field(name="Player:", value=f"{context.player.mention}")

        # If we have an image file, this is how it gets attached to the embed
        picture: discord.File = None
        if round_ctx.has_file():
            picture = discord.File(round_ctx.file_bytes, filename=round_ctx.file_name)
            embed.set_image(url=f"attachment://{round_ctx.file_name}")

        exp = time.time() + 20
        sent = await context.channel.send(file=picture, embed=embed)
        task = session.add_task(
            self.__bot.loop.create_task(self.__countdown_loop(sent, exp, embed))
        )
        if session.easy_mode:
            desc = ""
            for idx, choice in enumerate(session.choices):
                wrap = "~~" if choice[1] else "**"
                desc += f"**{idx + 1}**. {wrap}{choice[0].name}{wrap}\n"
            await context.channel.send(
                embed=discord.Embed(
                    color=discord.Color.blue(), title="Choices:", description=desc
                )
            )
        try:
            msg = await asyncio.wait_for(
                self.__wait_for_message(sent.id, session), timeout=20
            )
            if self.__check_answer_message(msg.content, session.god.name):
                answer_time = time.time() - (exp - 20)
                task.cancel()
                await msg.add_reaction("💯")
                async with context.channel.typing():
                    # These emojis are from my Discord server so I'll need to update these to be
                    # more universal. :D
                    ans_description = (
                        f"✅ Correct, **{context.player.mention}**! "
                        f"You got it in {round(answer_time)} seconds. "
                        f"The answer was **{session.god.name}**. "
                        "<:frogchamp:566686914858713108>"
                    )

                    embed = discord.Embed(
                        color=discord.Color.green(), description=ans_description
                    )
                    file_name = f"{session.god.name}.jpg"
                    picture = discord.File(
                        await session.skin.get_card_bytes(), filename=file_name
                    )
                    embed.set_image(url=f"attachment://{file_name}")

                    await context.channel.send(file=picture, embed=embed)
                    return True
            if session.easy_mode:
                self.__update_choices(msg.content, session)
            task.cancel()
            await msg.add_reaction("❌")
            inc_description = f"❌ Incorrect, **{context.player.mention}**."
            await self.__send_incorrect(
                inc_description, round_ctx.is_last_round(), session
            )
            return False
        except asyncio.TimeoutError:
            inc_description = "❌⏲️ Time's up! <:killmyself:472184572407447573>"
            await self.__send_incorrect(
                inc_description, round_ctx.is_last_round(), session
            )
            return False

    async def __wait_for_message(
        self, last_message_id: int, game: SmiteleGame
    ) -> discord.Message:
        channel = game.context.channel
        while (
            channel.last_message_id == last_message_id
            or not self.__validate_message(game)
            or not await self.__check_answer_is_god(channel.last_message, game)
        ):
            await asyncio.sleep(0)
        return channel.last_message

    def __validate_message(self, game: SmiteleGame) -> bool:
        channel = game.context.channel
        return (
            channel.last_message.author != self.__bot.user
            and not channel.last_message.content.startswith("$")
            and channel.last_message.author == game.context.player
        )

    async def __check_answer_is_god(
        self, guess: discord.Message, game: SmiteleGame
    ) -> bool:
        if any(
            self.__check_answer_message(guess.content, god.name)
            for god in list(game.provider.gods.values())
        ):
            return True
        await guess.add_reaction("❓")
        desc = f"**{guess.content}** is not a known god name!"
        await game.context.channel.send(
            embed=discord.Embed(color=discord.Color.red(), description=desc)
        )
        return False

    async def __send_invalid(self, ctx: discord.ApplicationContext, error_info: str):
        await ctx.respond(
            embed=discord.Embed(color=discord.Color.red(), description=error_info),
        )


class SmiteBotHelpCommand(commands.MinimalHelpCommand):
    async def send_pages(self):
        destination = self.get_destination()
        embed = discord.Embed(color=discord.Color.blurple(), description="")
        for page in self.paginator.pages:
            embed.description += page
        await destination.send(embed=embed)


if __name__ == "__main__":
    intents = discord.Intents.default()
    # pylint: disable=assigning-non-slot
    intents.message_content = True
    bot = commands.Bot(command_prefix="$", intents=intents)

    # One provider per game, built up front. A game with no provider simply is
    # not offered — Providers derives the `game:` choices from what is
    # registered — so a wiki outage degrades to a Smite-1-only bot rather than
    # to a broken one.
    provider = SmiteProvider()
    asyncio.run(provider.create())

    smite2_provider = Smite2Provider()
    asyncio.run(smite2_provider.create())

    settings = GuildSettings()
    registered = [provider]
    if smite2_provider.gods and smite2_provider.items:
        registered.append(smite2_provider)
    else:
        print(
            "Smite 2 static data unavailable; registering Smite 1 only.", flush=True
        )
    providers = Providers(*registered, settings=settings)

    player_stats = PlayerStats(providers)
    smitele = Smitele(bot, providers, settings)
    smite_triva = SmiteTrivia(bot, providers)
    bot.add_cog(smitele)
    bot.add_cog(smite_triva)
    bot.add_cog(player_stats)
    bot.help_command = SmiteBotHelpCommand()
    smitele.start_bot()
