"""Smite-le Bot - A Discord bot for playing Smite-le, the Smite and Wordle inspired game.

This module implements a Discord bot which allows users to play a six round game in Discord
channels. It requires some things in order to get this running: ffmpeg being installed on the
host machine, access to the Hirez API, and a Discord token.

Hirez API keys and Discord token are loaded via a config.json file, see the README for more
details.

TODO: Implement an environment variable method for loading secrets

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
from PIL import Image
from unidecode import unidecode

from build_optimizer import BuildOptimizer
from god import God
from god_builder import (
    BuildCommandType,
    BuildFailedError,
    BuildPrioritization,
    BuildOptions,
    GodBuilder,
)
from god_types import GodId, GodRole, GodType
from item import Item, ItemAttribute, ItemType
from player_stats import PlayerStats
from skin import Skin
from SmiteProvider import SmiteProvider
from smitetrivia import SmiteTrivia
from HirezAPI import PlayerRole, QueueId
from item_tree_builder import ItemTreeBuilder


class InvalidOptionError(Exception):
    pass


class GodOptions:
    god_id: GodId
    build: List[Item]
    level: int
    include_abilities: bool
    __items: Dict[int, Item]

    def __init__(self, items: Dict[int, Item]):
        self.god_id = None
        self.build = []
        self.level = 20
        self.include_abilities = False
        self.__items = items

    def set_option(self, option: str, value: str):
        if option in ("-g", "--god"):
            self.god_id = GodId[
                value.upper().replace(" ", "_").replace("'", "")
            ]  # handles Chang'e case
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

    def __init__(self, answer: God, context: SmiteleGameContext) -> None:
        """Inits SmiteleGame given an answer God and context"""
        self.god = answer
        self.context = context
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
    CONFIG_FILE: str = "config.json"
    GOD_IMAGE_FILE: str = "god.jpg"
    GOD_CROP_IMAGE_FILE: str = "godCrop.jpg"
    SKIN_IMAGE_FILE: str = "skin.jpg"
    SKIN_CROP_IMAGE_FILE: str = "crop.jpg"
    VOICE_LINE_FILE: str = "voice.ogg"

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

    # A helper lambda for hitting a random Smite wiki voicelines route
    __get_base_smite_wiki: Callable[[commands.Cog, str], str] = (
        lambda self, name: f"https://smite.fandom.com/wiki/{name}_voicelines"
    )

    def __init__(self, _bot: commands.Bot, _provider: SmiteProvider) -> None:
        # Setting our intents so that Discord knows what our bot is going to do
        self.__bot = _bot
        self.__smite_client = _provider
        self.__gods = _provider.gods
        self.__items = _provider.items
        self.__running_sessions = {}
        self.__tree_builder = ItemTreeBuilder(self.__items)
        self.__dataframe_refresher_running = False

        if self.__config is None:
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as file:
                    self.__config = json.load(file)

                    if not "discordToken" in self.__config:
                        raise RuntimeError(
                            f"{self.CONFIG_FILE} "
                            'was missing value for "discordToken."'
                        )
            except (FileNotFoundError, JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Failed to load {self.CONFIG_FILE}. Does this file exist?"
                ) from exc

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        activity = discord.Game(name="Smite", type=3)
        await self.__bot.change_presence(
            status=discord.Status.online, activity=activity
        )

        if not self.__dataframe_refresher_running:
            self.__bot.loop.create_task(self.__smite_client.load_dataframe())
            self.__dataframe_refresher_running = True

        print("Smite-le Bot is ready!")

    @commands.slash_command(
        name="top_fun",
        description="Figure out who's having top fun",
        guild_ids=[845718807509991445, 396874836250722316],
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
        guild_ids=[845718807509991445, 396874836250722316],
    )
    @discord.option(
        name="god_name",
        type=str,
        description="The god to return a build for",
        required=True,
    )
    @discord.option(
        name="match_queue",
        type=str,
        description="The queue to get results for, defaults to any queue",
        choices=[
            q.display_name
            for q in list(
                filter(
                    lambda _q: (QueueId.is_normal(_q) or QueueId.is_ranked(_q))
                    and _q
                    not in (
                        QueueId.UNDER_30_ARENA,
                        QueueId.UNDER_30_CONQUEST,
                        QueueId.UNDER_30_JOUST,
                    ),
                    list(QueueId),
                )
            )
        ],
        default="",
    )
    @discord.option(
        name="role",
        type=str,
        description="The Conquest role to find a build for",
        choices=[p.value.title() for p in list(PlayerRole)],
        default="",
    )
    # @discord.option(
    #     name="enemies",
    #     type=str,
    #     description="A comma-separated list of enemies which the player is up against",
    #     default="",
    # )
    # @discord.option(
    #     name="allies",
    #     type=str,
    #     description="A comma-separated list of allies which the player is teamed with",
    #     default="",
    # )
    @discord.option(
        name="high_mmr",
        type=bool,
        description="Whether to limit build search results to high MMR players (2000+)",
        default=False,
    )
    async def build(
        self,
        ctx: discord.ApplicationContext,
        god_name: str,
        match_queue: str,
        role: str,
        # enemies: str,
        # allies: str,
        high_mmr: bool,
    ):
        build_options = BuildOptions(build_type=BuildCommandType.ML)

        try:
            if god_name is not None and god_name != "":
                build_options.set_option("-g", god_name)
        except InvalidOptionError:
            await self.__send_invalid(
                ctx,
                f"{god_name} is not a God.",
            )
            return

        if match_queue is not None and match_queue != "":
            build_options.set_option("-q", match_queue)

        if role is not None and role != "":
            build_options.set_option("-r", role)

        # try:
        #     if enemies is not None and enemies != "":
        #         build_options.set_option("-e", enemies)
        # except InvalidOptionError:
        #     await self.__send_invalid(
        #         ctx,
        #         f'One of "{enemies}" is not a God.',
        #     )
        #     return

        # try:
        #     if allies is not None and allies != "":
        #         build_options.set_option("-a", allies)
        # except InvalidOptionError:
        #     await self.__send_invalid(
        #         ctx,
        #         f'One of "{enemies}" is not a God.',
        #     )
        #     return

        if high_mmr:
            if build_options.queue_id is None:
                build_options.set_option("-q", QueueId.RANKED_CONQUEST.name)

            build_options.set_option("-mmr", None)

        error_msg = build_options.validate()

        if error_msg is not None:
            await self.__send_invalid(ctx, error_msg)
            return

        god_builder = GodBuilder(self.__gods, self.__items, self.__smite_client)

        async with ctx.channel.typing():
            try:
                await ctx.respond(
                    embed=discord.Embed(
                        color=discord.Color.yellow(),
                        title="Finding you a build with those settings.",
                    )
                )
                build, relics, desc = god_builder.ml(build_options)
            except BuildFailedError:
                await self.__send_invalid(
                    ctx,
                    "Failed to find a matching build with those options. Try being less specific?",
                )
                return

            await self.__send_generated_build(
                build,
                ctx,
                desc,
                self.__gods[build_options.god_id],
                relics,
                no_god_specified=build_options.was_random_god(),
                no_god_specified_override="(You didn't input a god, so I found the best choice given your other inputs)",
            )

    @commands.slash_command(
        name="random_build",
        description="Get a random Smite build!",
        guild_ids=[845718807509991445, 396874836250722316],
    )
    @discord.option(
        name="god_name",
        type=str,
        description="The god to return a build for. If not specified, it'll be random",
        default="",
    )
    @discord.option(
        name="prioritize",
        type=str,
        description="What items to prioritize for a random build",
        choices=[p.value.lower() for p in list(BuildPrioritization)],
        default="",
    )
    async def random_build(
        self, ctx: discord.ApplicationContext, god_name: str, prioritize: str
    ):
        build_options = BuildOptions(build_type=BuildCommandType.RANDOM)

        if god_name is not None and god_name != "":
            build_options.set_option("-g", god_name)

        if prioritize is not None and prioritize != "":
            build_options.set_option("-p", prioritize)

        error_msg = build_options.validate()

        if error_msg is not None:
            await self.__send_invalid(ctx, error_msg)
            return

        god_builder = GodBuilder(self.__gods, self.__items, self.__smite_client)

        try:
            await ctx.respond(
                embed=discord.Embed(
                    color=discord.Color.yellow(),
                    title="Randomizing you the perfect build.",
                )
            )

            build, relics, desc = god_builder.random(build_options)
        except BuildFailedError:
            await self.__send_invalid(
                ctx,
                f"Failed to randomize a build for {self.__gods[build_options.god_id].name}.",
            )
            return

        await self.__send_generated_build(
            build,
            ctx,
            desc,
            self.__gods[build_options.god_id],
            relics,
            no_god_specified=build_options.was_random_god(),
        )
        return

    @commands.command(
        aliases=["smite-le", "st"],
        brief="Starts a game of Smite-le.",
        description="Starts a game of Smite-le. This is a five round game where "
        "you must guess the god or goddess given different information per round.",
        usage="[options]\n\nOptions:\n\t**easy** - gives you a list of gods to guess from\n\n"
        "Example Usage:\n\n$smitele easy\n",
    )
    async def smitele(self, message: discord.Message, *args: tuple) -> None:
        await self.__smitele(message, *args)

    @commands.command(
        brief="Stops a running game of Smite-le.",
        description="This command allows you to stop a running game of Smite-le. "
        "If you're not the bot owner, you'll only be able to stop your own game.",
        usage="[session_id]",
    )
    async def stop(self, message: discord.Message, *args: tuple) -> None:
        await self.__stop(message, *args)

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
        data_used = await self.__smite_client.get_data_used()
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

        if self.__smite_client.player_matches is None:
            desc = "player_matches is not yet initialized."
        else:
            buffer = io.StringIO()
            self.__smite_client.player_matches.info(buf=buffer)
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

    def __parse_god_opts(self, args: List[str]) -> GodOptions:
        god_options = GodOptions(self.__items)
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

        item_name = " ".join(flatten_args).lower()
        item: Item | None = None

        for i in self.__items.values():
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

            optimizer = BuildOptimizer(self.__gods[GodId.AGNI], [], self.__items)
            total_cost = optimizer.compute_item_price(item)
            item_embed.add_field(
                name="Cost:",
                value=f"**Total Cost**: {total_cost:,}\n**Upgrade Cost**: {item.price:,}",
            )

            if item.type == ItemType.ITEM and item.active:
                with await self.__tree_builder.generate_build_tree(item) as tree_image:
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

        god_options = self.__parse_god_opts(flatten_args)
        god = self.__gods[god_options.god_id]

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
            optimizer = BuildOptimizer(god, [], self.__items)
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
        build: List[Item],
        ctx: discord.ApplicationContext,
        extended_desc: str,
        god: God,
        relics: List[Item] = None,
        no_god_specified: bool = False,
        no_god_specified_override: str = None,
    ):
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
                    (96 * (3 + len(relics)), 96 * 2),
                    (250, 250, 250, 0),
                )

                output_image.paste(build_image, (0, 0))
                output_image.paste(relic_image, (288, 48))
                file_bytes = io.BytesIO()
                output_image.save(file_bytes, format="PNG")
                file_bytes.seek(0)

            file = discord.File(file_bytes, filename=self.BUILD_IMAGE_FILE)
            embed.set_image(url=f"attachment://{self.BUILD_IMAGE_FILE}")
            embed.set_thumbnail(url=god.icon_url)
            embed.add_field(
                name="Items", value=", ".join([item.name for item in build])
            )
            if relics is not None and any(relics):
                embed.add_field(
                    name="Relics", value=", ".join([item.name for item in relics])
                )
            if no_god_specified:
                embed.set_footer(
                    text=no_god_specified_override
                    or f"(You didn't give me a god, so I picked {god.name} for you)"
                )
            await ctx.respond(file=file, embed=embed)

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
    async def __smitele(self, message: discord.Message, *args: tuple) -> None:
        if message.author == self.__bot.user:
            return

        if len(self.__gods) == 0:
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

            try:
                god_arg = self.__gods[
                    GodId[
                        "_".join(["".join(arg) for arg in args])
                        .upper()
                        .replace("'", "")
                    ]
                ]  # handles Chang'e case
            except KeyError:
                pass
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
            god_arg or random.choice(list(self.__gods.values())), context
        )
        if easy_mode:
            game.generate_easy_mode_choices(list(self.__gods.values()))
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
            Skin.from_json(skin)
            for skin in await self.__smite_client.get_god_skins(session.god.id)
        ]

        build_task = session.add_task(
            self.__bot.loop.create_task(self.__prefetch_build_image(session))
        )
        base_skin = next(
            skin for skin in skins if skin.name == f"Standard {session.god.name}"
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

    async def __make_build_image(self, build: List[Item]) -> io.BytesIO:
        # Appending the images into a single build image
        thumb_size = 96
        with Image.new(
            "RGBA",
            (thumb_size * min(3, len(build)), thumb_size * math.ceil(len(build) / 3)),
            (250, 250, 250, 0),
        ) as output_image:
            pos_x, pos_y = (0, 0)
            for idx, item in enumerate(build):
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
                            if idx != 2:
                                pos_x += thumb_size
                            if idx == 2:
                                pos_x, pos_y = (0, thumb_size)
                    except Exception as ex:
                        print(f"Unable to create an image for {item.name}, {ex}")

            file = io.BytesIO()
            output_image.save(file, format="PNG")
            file.seek(0)
            return file

    async def __prefetch_build_image(self, session: SmiteleGame) -> io.BytesIO:
        # Index maps to position in build
        build: List[Item] = []

        # Hirez's route for getting recommended items is highly out of date, so we'll get a
        # top Ranked Conquest player's build
        god_leaderboard = await self.__smite_client.get_god_leaderboard(
            session.god.id, QueueId.RANKED_CONQUEST
        )

        while len(build) == 0:
            # Fetching a random player from the leaderboard
            random_player = random.choice(god_leaderboard)
            god_leaderboard.remove(random_player)

            # Scraping their recent match history to try and find a current build
            match_history = await self.__smite_client.get_match_history(
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
                        build.append(self.__items[item_id])

        return await self.__make_build_image(build)

    async def __send_god_skin(self, session: SmiteleGame, skins: List[Skin]) -> bool:
        # Fetching a random god skin
        skin = random.choice(list(filter(lambda s: s.has_url, skins)))
        session.skin = skin

        with await skin.get_card_bytes() as skin_image:
            with io.BytesIO() as file:
                # Cropping the skin image that we got randomly
                with Image.open(skin_image) as img:
                    width, height = img.size
                    size = math.floor(width / 4.0)
                    left = random.randint(0, width - size)
                    top = random.randint(0, height - size)
                    crop_image = img.crop((left, top, left + size, top + size))
                    if crop_image.size != (180, 180):
                        crop_image = crop_image.resize((180, 180))
                    crop_image.save(file, format="JPEG", quality=95)
                    file.seek(0)

                desc = "Name the god with this skin"
                session.current_round.file_bytes = file
                session.current_round.file_name = self.SKIN_CROP_IMAGE_FILE
                return await self.__send_round_and_wait_wrapper(desc, session)

    async def __send_god_build(
        self, session: SmiteleGame, build_task: "asyncio.Task[io.BytesIO]"
    ) -> bool:
        with await build_task as file:
            desc = "Hint: A top-ranked player of this god recently used this build."
            session.current_round.file_bytes = file
            session.current_round.file_name = self.BUILD_IMAGE_FILE
            return await self.__send_round_and_wait_wrapper(desc, session)

    async def __send_god_voiceline(
        self, session: SmiteleGame, skins: List[Skin]
    ) -> bool:
        context = session.context
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
                # If the current player is in a voice channel,
                # connect to it and play the voice line!
                if context.player.voice is not None:
                    with open(self.VOICE_LINE_FILE, "wb") as voice_file:
                        voice_file.write(await res.content.read())
                    voice_client = await context.player.voice.channel.connect()

                    async def disconnect():
                        await voice_client.disconnect()
                        os.remove(self.VOICE_LINE_FILE)

                    voice_client.play(
                        discord.FFmpegPCMAudio(source=self.VOICE_LINE_FILE),
                        after=lambda _: asyncio.run_coroutine_threadsafe(
                            coro=disconnect(), loop=voice_client.loop
                        ).result(),
                    )
                else:
                    # Otherwise, just upload the voice file to Discord
                    with io.BytesIO(await res.content.read()) as file:
                        dis_file = discord.File(file, filename=self.VOICE_LINE_FILE)
                        await context.channel.send(file=dis_file)

                session.current_round.reset_file()
                return await self.__send_round_and_wait_wrapper(
                    "Whose voice line was that?", session
                )

    async def __send_god_ability_icon(self, session: SmiteleGame) -> bool:
        saved_image = False
        with io.BytesIO() as ability_bytes:
            while not saved_image:
                try:
                    # Some gods actually have more than this (e.g. King Arthur, Merlin).
                    # I may add support for their additional abilities later
                    ability = random.choice(session.god.abilities)
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
                    width, height = img.size
                    size = math.floor(width / 4.0)
                    left = random.randint(0, width - size)
                    top = random.randint(0, height - size)
                    crop_image = img.crop((left, top, left + size, top + size))
                    if crop_image.size != (180, 180):
                        crop_image = crop_image.resize((180, 180))
                    crop_image.save(crop_file, format="JPEG", quality=95)
                    crop_file.seek(0)

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
            for god in list(self.__gods.values())
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
    provider = SmiteProvider()
    asyncio.run(provider.create())
    player_stats = PlayerStats(provider)
    smitele = Smitele(bot, provider)
    smite_triva = SmiteTrivia(bot, provider)
    bot.add_cog(smitele)
    bot.add_cog(smite_triva)
    bot.add_cog(player_stats)
    bot.help_command = SmiteBotHelpCommand()
    smitele.start_bot()
