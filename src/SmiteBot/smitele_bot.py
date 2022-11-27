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

import asyncio
import io
import json
import math
import os
import random
import time
import traceback
from json.decoder import JSONDecodeError
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Set, Tuple

import aiohttp
import discord
import edit_distance
from bs4 import BeautifulSoup
from discord.ext import commands
from PIL import Image
from unidecode import unidecode

from HirezAPI import Smite, PlayerRole, QueueId, TierId
from ability import Ability
from god import God
from god_types import GodId, GodType
from item import Item, ItemType, ItemAttribute
from skin import Skin

from build_optimizer import BuildOptimizer

class StoppedError(Exception):
    pass

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

    __total_rounds: int

    def __init__(self, total_rounds: int) -> None:
        """Inits _SmiteleRoundContext given a number of rounds"""
        self.__total_rounds = total_rounds

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
        return self.round_number == self.__total_rounds

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
    ABILITY_IMAGE_FILE: str = 'ability.jpg'
    BUILD_IMAGE_FILE: str = 'build.jpg'
    CONFIG_FILE: str = 'config.json'
    GOD_IMAGE_FILE: str = 'god.jpg'
    GOD_CROP_IMAGE_FILE: str = 'godCrop.jpg'
    GODS_FILE: str = 'gods.json'
    ITEMS_FILE: str = 'items.json'
    SKIN_IMAGE_FILE: str = 'skin.jpg'
    SKIN_CROP_IMAGE_FILE: str = 'crop.jpg'
    SMITE_PATCH_VERSION_FILE: str = 'version'
    VOICE_LINE_FILE: str = 'voice.ogg'

    __bot: commands.Bot

    # Cached config values
    __config: dict = None

    # Cached getgods in memory
    __gods: List[God]

    # Wrapper client around Hirez API
    __smite_client: Smite

    # Cached getitems in memory
    __items: Dict[int, Item]

    # Mapping of session IDs to running games
    __running_sessions: Dict[int, SmiteleGame]

    # A helper lambda for hitting a random Smite wiki voicelines route
    __get_base_smite_wiki: Callable[[commands.Cog, str], str] = \
        lambda self, name: f'https://smite.fandom.com/wiki/{name}_voicelines'

    def __init__(self, _bot: commands.Bot) -> None:
        # Setting our intents so that Discord knows what our bot is going to do
        self.__bot = _bot
        self.__gods = []
        self.__items = {}
        self.__running_sessions = {}

        if self.__config is None:
            try:
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as file:
                    self.__config = json.load(file)

                    if not 'discordToken' in self.__config:
                        raise RuntimeError(f'{self.CONFIG_FILE} '\
                            'was missing value for "discordToken."')
                    if not 'hirezDevId' in self.__config:
                        raise RuntimeError(f'{self.CONFIG_FILE} '\
                            'was missing value for "hirezDevId."')
                    if not 'hirezAuthKey' in self.__config:
                        raise RuntimeError(f'{self.CONFIG_FILE} '\
                            'was missing value for "hirezAuthKey."')
            except (FileNotFoundError, JSONDecodeError) as exc:
                raise RuntimeError(f'Failed to load {self.CONFIG_FILE}. Does this file exist?') \
                    from exc

        self.__smite_client = Smite(self.__config['hirezAuthKey'], self.__config['hirezDevId'])

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        activity = discord.Game(name='Smite', type=3)
        await self.__bot.change_presence(status=discord.Status.online, activity=activity)

        should_refresh, current_patch = await self.__should_refresh()

        gods = await self.__load_cache(self.GODS_FILE, \
            self.__smite_client.get_gods if should_refresh else None)
        self.__gods = [God.from_json(god) for god in gods]

        items = await self.__load_cache(self.ITEMS_FILE, \
            self.__smite_client.get_items if should_refresh else None)

        for i in items:
            item = Item.from_json(i)
            self.__items[item.id] = item

        if should_refresh:
            with open(self.SMITE_PATCH_VERSION_FILE, 'w', encoding='utf-8') as file:
                file.write(str(current_patch))

        print('Smite-le Bot is ready!')

    @commands.command(aliases=["smite-le", "st"])
    async def smitele(self, message: discord.Message, *args: tuple) -> None:
        await self.__smitele(message, *args)

    @commands.command()
    async def stop(self, message: discord.Message, *args: tuple) -> None:
        await self.__stop(message, *args)

    @commands.command()
    @commands.is_owner()
    async def sessionid(self, message: discord.Message, *args: tuple) -> None:
        game_session_id = hash(SmiteleGameContext(message.author, message.channel))
        if game_session_id in self.__running_sessions:
            await message.channel.send(embed=discord.Embed(color=discord.Color.gold(), \
                description=f'Running session ID = {game_session_id}'))
        else:
            await message.channel.send(embed=discord.Embed(\
                color=discord.Color.red(), \
                title='No running game session!'))

    @commands.command()
    @commands.is_owner()
    async def sessions(self, message: discord.Message, *args: tuple) -> None:
        if len(self.__running_sessions) == 0:
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                title='No running game sessions!'))
        else:
            output_msg = ''
            for game_session_id, game in self.__running_sessions.items():
                context = game.context
                output_msg += f'> {context.player.mention} '\
                    f'({context.channel.mention}): **{game_session_id}**\n'

            await message.channel.send(embed=discord.Embed(color=discord.Color.gold(), \
                description=output_msg))

    @commands.command()
    @commands.is_owner()
    async def usage(self, context: commands.Context):
        data_used = await self.__smite_client.get_data_used()
        if any(data_used):
            data_used = data_used[0]
        else:
            await context.channel.send(discord.Embed(
                color=discord.Color.gold(), title='Data Usage', description='Nothing to report.'))
            return
        desc = f'Active Sessions: {data_used["Active_Sessions"]}/{data_used["Concurrent_Sessions"]}\n'\
            f'Total Requests Today: {data_used["Total_Requests_Today"]}/{data_used["Request_Limit_Daily"]}\n'\
            f'Total Sessions Today: {data_used["Total_Sessions_Today"]}/{data_used["Session_Cap"]}\n'
        await context.channel.send(embed=discord.Embed(
            color=discord.Color.gold(), title='Data Usage', description=desc))

    @commands.command()
    async def resign(self, message: discord.Message, *args: tuple) -> None:
        game_session_id = hash(SmiteleGameContext(message.author, message.channel))
        if game_session_id in self.__running_sessions:
            session = self.__running_sessions[game_session_id]
            await self.__send_incorrect('Round Resigned!', True, session)
            self.__try_stop_running_game_session(game_session_id)
        else:
            await message.channel.send(embed=discord.Embed(\
                color=discord.Color.red(), \
                title='No running game session!'))

    @commands.command(aliases=["quit"])
    @commands.is_owner()
    async def shutdown(self, message: discord.Message) -> None:
        await message.channel.send(embed=discord.Embed(color=discord.Color.gold(), \
            description=f'Closing {self.__bot.user.mention}'))
        await self.__bot.change_presence(status=discord.Status.offline)
        await self.__bot.close()

    @commands.command(aliases=["trivia"])
    async def smitetrivia(self, message, *args):
        await self.__smitetrivia(message, *args)

    @commands.command()
    async def scores(self, ctx):
        await self.__scores(ctx)

    @commands.command(aliases=["sr"])
    async def rank(self, message: discord.Message, *args: tuple) -> None:
        if not any(args) or len(args) > 1:
            desc = f'Invalid command! {self.__bot.user.mention} '\
                    f'accepts the command `$rank [playername]` (or `$sr [playername]`)'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))
            return
        player_name = ''.join(args[0])
        players = await self.__smite_client.get_player_id_by_name(player_name)
        if not any(players):
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description='No players with that name found!'))
            return
        if players[0]['privacy_flag'] == 'y':
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=f'{player_name} has their profile hidden... <:reeratbig:849771936509722634>'))
            return
        player_id = players[0]['player_id']
        player = (await self.__smite_client.get_player(player_id))[0]
        def get_rank_string(queue_id: QueueId, tier_id: TierId, mmr: float) -> str:
            emoji = '🥉' if tier_id.value <= 5 \
                else '🥈' if tier_id.value <= 10 \
                else '🥇' if tier_id.value <= 15 \
                else '🏅' if tier_id.value <= 20 \
                else '💎' if tier_id.value <= 25 \
                else '🏆' if tier_id.value == 26 else '💯'
            tier_name = tier_id.name.replace('_', ' ').title()\
                .replace('Iv', 'IV')\
                .replace('Iii', 'III')\
                .replace('Ii', 'II')
            return f'{queue_id.name.lower().replace("_", " ").title().replace("Controller", "(🎮)")}: '\
                   f'{emoji} **{tier_name}** ({int(mmr)} MMR)\n'
        tier = player['RankedConquest']['Tier']
        ranked_conquest = get_rank_string(QueueId.RANKED_CONQUEST, TierId(int(tier)), player["Rank_Stat_Conquest"]) if tier != 0 else ''
        tier = player['RankedDuel']['Tier']
        ranked_duel = get_rank_string(QueueId.RANKED_DUEL, TierId(int(tier)), player["Rank_Stat_Duel"]) if tier != 0 else ''
        tier = player['RankedJoust']['Tier']
        ranked_joust = get_rank_string(QueueId.RANKED_JOUST, TierId(int(tier)), player["Rank_Stat_Joust"]) if tier != 0 else ''
        tier = player['RankedConquestController']['Tier']
        ranked_conquest_controller = get_rank_string(QueueId.RANKED_CONQUEST_CONTROLLER, TierId(int(tier)), player["Rank_Stat_Conquest_Controller"]) if tier != 0 else ''
        tier = player['RankedDuelController']['Tier']
        ranked_duel_controller = get_rank_string(QueueId.RANKED_DUEL_CONTROLLER, TierId(int(tier)), player["Rank_Stat_Duel_Controller"]) if tier != 0 else ''
        tier = player['RankedJoustController']['Tier']
        ranked_joust_controller = get_rank_string(QueueId.RANKED_JOUST_CONTROLLER, TierId(int(tier)), player["Rank_Stat_Joust_Controller"]) if tier != 0 else ''
        desc = f'{ranked_conquest}{ranked_duel}{ranked_joust}{ranked_conquest_controller}{ranked_duel_controller}{ranked_joust_controller}'
        if desc == '':
            await message.channel.send(embed=discord.Embed(color=discord.Color.yellow(), \
            description=f'{player["Name"]} has no ranks...'))
            return
        await message.channel.send(embed=discord.Embed(color=discord.Color.blue(), \
            description=desc, title=f'{player["Name"]} Ranks:'))

    @commands.command(aliases=['b'])
    @commands.max_concurrency(1, per=commands.BucketType.guild)
    async def build(self, message: discord.Message, *args: tuple):
        async def send_invalid(additional_info: str = 'Invalid command!'):
            desc = f'{additional_info} {self.__bot.user.mention} '\
                    'accepts the command `$build [godname] [random [power|defense]|top|optimize statname]` '\
                    '(or `$b [godname] [random [power|defense]|top|optimize statname]`)'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))

        no_god_arg = not any(args)
        flatten_args = [''.join(arg) for arg in args]
        option_index: int = None

        for idx, arg in enumerate(flatten_args):
            if arg in ('random', 'top', 'optimize'):
                option_index = idx
                break

        god: God = None
        god_id: GodId = None
        if no_god_arg:
            god_id = random.choice(list(GodId))
        else:
            try:
                god_name: str
                if option_index is not None:
                    god_name = ' '.join(flatten_args[0:option_index])
                else:
                    god_name = ' '.join(flatten_args[0:])
                god_id = GodId[god_name.upper().replace(' ', '_')\
                    .replace("'", '')] # handles Chang'e case
            except KeyError:
                await send_invalid(f'{god_name} is not a valid god!')
                return
        for g in self.__gods:
            if g.id == god_id:
                god = g
                break
        if god is None:
            await send_invalid(f'{god_id.name.title()} not mapped to a god!')
            return

        await message.channel.typing()

        option = flatten_args[option_index] if option_index is not None else None

        items_for_god = list(filter(
            lambda item: item.type == ItemType.ITEM and item.active and
                # Filter out acorns from non-Ratatoskr gods
                (item.root_item_id != 18703 or god.id == GodId.RATATOSKR) and
                # Filter out Odysseus' Bow from non-physical gods
                (item.id != 10482 or god.type == GodType.PHYSICAL) and
                # Filter out any items that have restricted roles that intersect
                # with the current god's role
                (not any(item.restricted_roles) or
                    god.role not in item.restricted_roles) and
                # Elucidate god type from item properties and check intersection
                (any(p.attribute.god_type == god.type
                    for p in item.item_properties) or
                all(p.attribute.god_type is None
                    for p in item.item_properties)), self.__items.values()))

        optimizer = BuildOptimizer(god, items_for_god, self.__items)

        if option is None or option == 'random':
            prioritize = flatten_args[option_index + 1] \
                if option_index is not None and len(flatten_args) > option_index + 1 else None
            if prioritize is not None and prioritize not in ('power', 'defense'):
                await send_invalid(f'{prioritize} is not a valid option for random!')
                return
            build = []

            unfiltered_items = items_for_god # Needed for Ratatoskr
            if prioritize is not None:
                items_for_god = optimizer.filter_prioritize(items_for_god, prioritize)
            # Filter to just tier 3 items
            items = optimizer.filter_evolution_parents(
                optimizer.filter_acorns(
                    optimizer.filter_tiers(items_for_god)))

            # Ratatoskr always has his acorn!
            should_include_starter = random.randint(0, 1)
            should_include_glyph = bool(random.randint(0, 1))
            is_ratatoskr = god.id == GodId.RATATOSKR
            build_size = 6 - should_include_starter - int(is_ratatoskr)

            # Add a glyph... maybe!!
            if should_include_glyph:
                glyphs = optimizer.get_glyphs(items_for_god)
                glyph = random.choice(glyphs)
                build.append(glyph)
                build_size = build_size - 1
                items = optimizer.filter_glyph_parent(items, glyph)

            # Add build_size random items from our tier 3 items, then shuffle the build order
            build.extend(random.sample(items, build_size))
            random.shuffle(build)

            # Special casing Ratatoskr's acorn. Gotta have it!
            if is_ratatoskr:
                acorns = optimizer.filter_tiers(
                    optimizer.get_ratatoskr_acorn(unfiltered_items))
                build.insert(0, random.choice(acorns))

            # Adding a starter to the beginning of the build if random demands it
            if bool(should_include_starter):
                starter_evolutions = optimizer.get_starters(items_for_god)
                build.insert(0 + int(is_ratatoskr), random.choice(starter_evolutions))

            # If we decided to not have a random glyph, but still included a
            # direct parent of a glyph... upgrade it anyway! Random!
            if not should_include_glyph:
                parent_idx, glyph = optimizer.get_glyph_parent_if_no_glyphs(build)
                if glyph is not None:
                    build[parent_idx] = glyph

            desc = 'here\'s your random build!\n\n'\
                   f'{optimizer.get_build_stats_string(build)}'

            await self.__send_generated_build(
                build, message, desc, god, no_god_arg)
            return
        if option == 'top':
            role: str | PlayerRole = flatten_args[option_index + 1] \
                if option_index is not None and len(flatten_args) > option_index + 1 else None
            try:
                if role is not None:
                    role = PlayerRole(role.lower())
                    desc = f'Trying to find a game with {god.name} in '\
                           f'{role.value.title()}... This may take a while...'
                    await message.channel.send(
                        embed=discord.Embed(color=discord.Color.blue(), description=desc))
            except KeyError:
                await send_invalid(f'{role} is not a valid role!')
                return
            build = []
            god_leaderboard = await self.__smite_client.get_god_leaderboard(\
                god.id, QueueId.RANKED_CONQUEST)

            build_match = None
            while len(build) == 0:
                try:
                    # Fetching a random player from the leaderboard
                    random_player = random.choice(god_leaderboard)
                    god_leaderboard.remove(random_player)
                except IndexError:
                    if role is not None:
                        desc = f'Failed to find any games with {god.name} in {role.value.title()}!'
                        await message.channel.send(
                            embed=discord.Embed(color=discord.Color.red(), description=desc))
                    return

                # Scraping their recent match history to try and find a current build
                match_history = await self.__smite_client.get_match_history(\
                    int(random_player['player_id']))
                for match in match_history:
                    if len(build) != 0:
                        break
                    if role is not None:
                        match_role = match['Role']
                        if match_role is not None and match_role.lower() != role.value:
                            continue
                    build_match = match
                    # Get a full build for this god
                    if int(match['GodId']) == god.id.value and int(match['ItemId6']) != 0:
                        for i in range(1, 7):
                            # Luckily `getmatchhistory` includes build info!
                            item_id = int(match[f'ItemId{i}'])
                            item = self.__items[item_id]
                            if item.tier < 3 and \
                                (item.parent_item_id is None or \
                                    not self.__items[item.parent_item_id].is_starter):
                                build = []
                                break
                            build.append(self.__items[item_id])
            desc = f'here\'s your build, '\
                f'courtesy of #{random_player["rank"]} {god.name} '\
                f'**{random_player["player_name"]}**! '\
                f'({"they won!" if build_match["Win_Status"] == "Win" else "they lost..."})\n\n'\
                f'They were playing {role.value.title() if role is not None else match["Queue"]} '\
                f'and they went {build_match["Kills"]}/'\
                f'{build_match["Deaths"]}/{build_match["Assists"]}!\n\n'\
                f'{optimizer.get_build_stats_string(build)}'
            await self.__send_generated_build(build, message, desc, god)
            return
        if option == 'optimize':
            stat_name = ''
            if len(flatten_args) > option_index:
                stat_name = ' '.join(arg for arg in flatten_args[option_index + 1:])
                if stat_name != '':
                    try:
                        optimizer.set_stat(stat_name)
                    except ValueError as ex:
                        await send_invalid(str(ex))
                        return
            async def send_failed():
                prioritizing_stat = f' prioritizing {stat_name}' if stat_name != '' else ''
                desc = f'Failed to optimize a build for {god.name}{prioritizing_stat}!'
                await message.channel.send(
                    embed=discord.Embed(color=discord.Color.red(), description=desc))
            try:
                vowels = ('A', 'E', 'I', 'O', 'U')
                desc = f'Optimizing a{"n" if god.name.startswith(vowels) else ""} '\
                       f'{god.name} build for you... This may take a while...'
                await message.channel.send(
                    embed=discord.Embed(color=discord.Color.blue(), description=desc))
                await message.channel.typing()
                builds, iterations = await optimizer.optimize()
            except ValueError:
                traceback.print_exc()
                await send_failed()
                return
            if not any(builds):
                await send_failed()
                return
            build = random.choice(builds)
            optimized_for = f', optimized for {stat_name}' if stat_name != '' else ''
            desc = f'here\'s your number crunched build{optimized_for}! '\
                   f'I tried **{iterations:,}** builds and found '\
                   f'**{len(builds):,}** viable builds. '\
                   f'Here\'s one of them, hopefully it\'s a winner!\n\n'\
                   f'{optimizer.get_build_stats_string(build)}'
            await self.__send_generated_build(build, message, desc, god)
            return

    def start_bot(self) -> None:
        """
        Using this command instead of just calling self.run() since the discordToken is loaded
        as part of this class.
        """
        self.__bot.run(self.__config['discordToken'])

    async def __send_generated_build(self, build: List[Item], \
            message: discord.Message, extended_desc: str, god: God, no_god_specified: bool = False):
        with await self.__make_build_image(build) as build_image:
            desc = f'Hey {message.author.mention}, {extended_desc}'
            embed = discord.Embed(color=discord.Color.blue(), \
                description=desc, title=f'Your {god.name} Build Has Arrived!')
            file = discord.File(build_image, filename=self.BUILD_IMAGE_FILE)
            embed.set_image(url=f'attachment://{self.BUILD_IMAGE_FILE}')
            embed.set_thumbnail(url=god.icon_url)
            embed.add_field(name='Items', value=', '.join([item.name for item in build]))
            if no_god_specified:
                embed.set_footer(
                    text=f'(You didn\'t give me a god, so I picked {god.name} for you)')
            await message.channel.send(file=file, embed=embed)

    async def __stop(self, message: discord.Message, *args: tuple) -> None:
        game_session_id = hash(SmiteleGameContext(message.author, message.channel))

        if len(args) > 0:
            try:
                if not await self.__bot.is_owner(message.author):
                    await message.channel.send(embed=discord.Embed(\
                        color=discord.Color.red(), \
                        title='Can\'t stop another player\'s game!'))
                    return
                game_session_id = int(args[0])
            except TypeError:
                await message.channel.send(embed=discord.Embed(\
                    color=discord.Color.red(), \
                    title='Invalid input for session ID'))
                return

        if self.__try_stop_running_game_session(game_session_id):
            await message.channel.send(embed=discord.Embed(\
                color=discord.Color.red(), \
                title='Smite-le round canceled!'))
            print(f'Game session with ID {game_session_id} was successfully stopped')
            return
        await message.channel.send(embed=discord.Embed(\
            color=discord.Color.red(), \
            title='No running game session!'))

    def __try_stop_running_game_session(self, game_session_id: int) -> bool:
        if game_session_id in self.__running_sessions:
            self.__running_sessions[game_session_id].cancel()
            del self.__running_sessions[game_session_id]
            return True
        return False

    # Helper function for checking correctness
    @staticmethod
    def __check_answer_message(guess: str, answer: str) -> bool:
        guess = unidecode(guess).lower().replace('-', ' ')
        return guess == answer.lower() or \
            edit_distance.SequenceMatcher(a=guess, b=answer.lower()).distance() <= 1

    def __update_choices(self, guess: str, game: SmiteleGame) -> None:
        for idx, choice in enumerate(game.choices):
            if self.__check_answer_message(guess, choice[0].name):
                game.choices[idx] = (choice[0], True)

    # Primary command for starting a round of Smite-le!
    async def __smitele(self, message: discord.Message, *args: tuple) -> None:
        if message.author == self.__bot.user:
            return

        if len(self.__gods) == 0:
            desc = f'{self.__bot.user.mention} has not finished initializing.'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))
            return

        context = SmiteleGameContext(message.author, message.channel)
        game_session_id = hash(context)

        if game_session_id in self.__running_sessions:
            desc = 'Can\'t start another game, '\
                f'**{context.player.mention}** already has a running game!'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))
            return

        easy_mode = False
        if any(args):
            async def send_invalid():
                desc = f'Invalid command! {self.__bot.user.mention} '\
                       f'accepts the command `$smitele [easy]` (or `$st [easy]`)'
                await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                    description=desc))
            if len(args) > 1:
                await send_invalid()
                return
            if ''.join(args[0]) != 'easy':
                await send_invalid()
                return
            easy_mode = True

        # Fetching a random god from our list of cached gods
        game = SmiteleGame(random.choice(self.__gods), context)
        if easy_mode:
            game.generate_easy_mode_choices(self.__gods)
        self.__running_sessions[game_session_id] = game
        try:
            await game.add_task(self.__bot.loop.create_task(\
                self.__run_game_session(game)))
        # pylint: disable=broad-except
        except Exception:
            desc = f'{self.__bot.user.mention} encountered a fatal error. Please try again later.'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))
            print(f'Fatal exception encountered: {traceback.format_exc()}')
            game.cancel()
        finally:
            if game_session_id in self.__running_sessions:
                del self.__running_sessions[game_session_id]

    async def __run_game_session(self, session: SmiteleGame) -> None:
        # Fetching skins for this god, used in multiple rounds
        skins = [Skin.from_json(skin) for skin in \
            await self.__smite_client.get_god_skins(session.god.id)]

        build_task = session.add_task(self.__bot.loop\
            .create_task(self.__prefetch_build_image(session)))
        base_skin = next(skin for skin in skins if skin.name == f'Standard {session.god.name}')

        round_methods: Callable[[], Coroutine[Any, Any, bool]] = [
            lambda: self.__send_god_skin(session, skins),
            lambda: self.__send_god_build(session, build_task),
            lambda: self.__send_god_voiceline(session, skins),
            lambda: self.__send_god_ability_icon(session),
            lambda: self.__send_god_title(session),
            lambda: self.__send_god_base_card(session, base_skin)
        ]

        await session.context.channel.send(embed=discord.Embed(color=discord.Color.blue(), \
            title='Smite-le Started!', \
            description='Name the god given the clues! You\'ll have '\
                       f'{len(round_methods)} attempts.'))

        session.current_round = _SmiteleRoundContext(len(round_methods))
        for idx, method in enumerate(round_methods):
            session.current_round.round_number = idx + 1
            await session.context.channel.typing()
            if await session.add_task(self.__bot.loop.create_task(method())):
                return

    async def __make_build_image(self, build: List[Item]) -> io.BytesIO:
        # Appending the images into a single build image
        thumb_size = 96
        with Image.new('RGB', (thumb_size*3, thumb_size*2), (250, 250, 250)) as output_image:
            pos_x, pos_y = (0, 0)
            for idx, item in enumerate(build):
                # First requesting and saving the image from the URLs we got
                with await item.get_icon_bytes() as item_bytes:
                    try:
                        with Image.open(item_bytes) as image:
                            # Resize the image if necessary, Hirez doesn't return a consistent size
                            if image.size != (thumb_size, thumb_size):
                                image = image.resize((thumb_size, thumb_size))
                            if image.mode != 'RGB':
                                image = image.convert('RGB')
                            output_image.paste(image, (pos_x, pos_y))
                            if idx != 2:
                                pos_x += thumb_size
                            if idx == 2:
                                pos_x, pos_y = (0, thumb_size)
                    except Exception as ex:
                        print(f'Unable to create an image for {item.name}, {ex}')

            file = io.BytesIO()
            output_image.save(file, format='JPEG', quality=95)
            file.seek(0)
            return file

    async def __prefetch_build_image(self, session: SmiteleGame) -> io.BytesIO:
        # Index maps to position in build
        build: List[Item] = []

        # Hirez's route for getting recommended items is highly out of date, so we'll get a
        # top Ranked Conquest player's build
        god_leaderboard = await self.__smite_client.get_god_leaderboard(\
            session.god.id, QueueId.RANKED_CONQUEST)

        while len(build) == 0:
            # Fetching a random player from the leaderboard
            random_player = random.choice(god_leaderboard)
            god_leaderboard.remove(random_player)

            # Scraping their recent match history to try and find a current build
            match_history = await self.__smite_client.get_match_history(\
                int(random_player['player_id']))
            for match in match_history:
                if len(build) != 0:
                    break
                # Get a full build for this god
                if int(match['GodId']) == session.god.id.value and int(match['ItemId6']) != 0:
                    for i in range(1, 7):
                        # Luckily `getmatchhistory` includes build info!
                        item_id = int(match[f'ItemId{i}'])
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
                    crop_image.save(file, format='JPEG', quality=95)
                    file.seek(0)

                desc = 'Name the god with this skin'
                session.current_round.file_bytes = file
                session.current_round.file_name = self.SKIN_CROP_IMAGE_FILE
                return await self.__send_round_and_wait_wrapper(desc, session)

    async def __send_god_build(self, session: SmiteleGame, \
            build_task: 'asyncio.Task[io.BytesIO]') -> bool:
        with await build_task as file:
            desc = 'Hint: A top-ranked player of this god recently used this build in Ranked '\
                'Conquest.'
            session.current_round.file_bytes = file
            session.current_round.file_name = self.BUILD_IMAGE_FILE
            return await self.__send_round_and_wait_wrapper(desc, session)

    async def __send_god_voiceline(self, session: SmiteleGame, skins: List[Skin]) -> bool:
        context = session.context
        audio_src = None
        skin_copy = skins.copy()
        def remove_skin(name: str) -> List[Skin]:
            return list(filter(lambda s: s.name != name, skin_copy))

        async with aiohttp.ClientSession() as client:
            while audio_src is None:
                # Getting a random skin to fetch a random voiceline for
                skin = random.choice(skin_copy)
                page_name = ''

                # All of these correspond to just the god's name
                if skin.name in ['Golden', 'Legendary', 'Diamond', f'Standard {session.god.name}']:
                    page_name = session.god.name
                else:
                    page_name = f'{skin.name}_{session.god.name}'

                try:
                    # Not all skins have voiceline pages on the Smite wiki,
                    # so retry until we get one that works
                    async with client.get(self.__get_base_smite_wiki(\
                            name=page_name.replace(' ', '_'))) as res:
                        if res.status != 200:
                            skin_copy = remove_skin(skin.name)
                            continue
                        smite_wiki = BeautifulSoup(await res.content.read(), 'html.parser')
                        # BeautifulSoup is amazing
                        audio_blocks = smite_wiki.find_all('audio')

                        # Exclude the first voice line, which is line played
                        # when the god is locked in (says their name)
                        audio_src = random.choice(audio_blocks).source.get('src')
                except (ValueError, IndexError):
                    skin_copy = remove_skin(skin.name)
            async with client.get(audio_src) as res:
                # If the current player is in a voice channel,
                # connect to it and play the voice line!
                if context.player.voice is not None:
                    with open(self.VOICE_LINE_FILE, 'wb') as voice_file:
                        voice_file.write(await res.content.read())
                    voice_client = await context.player.voice.channel.connect()

                    async def disconnect():
                        await voice_client.disconnect()
                        os.remove(self.VOICE_LINE_FILE)

                    voice_client.play(discord.FFmpegPCMAudio(source=self.VOICE_LINE_FILE), \
                        after=lambda _: asyncio.run_coroutine_threadsafe(\
                            coro=disconnect(), loop=voice_client.loop).result())
                else:
                    # Otherwise, just upload the voice file to Discord
                    with io.BytesIO(await res.content.read()) as file:
                        dis_file = discord.File(file, filename=self.VOICE_LINE_FILE)
                        await context.channel.send(file=dis_file)

                session.current_round.reset_file()
                return await self.__send_round_and_wait_wrapper('Whose voice line was that?', \
                    session)

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
                        if image.mode != 'RGB':
                            image = image.convert('RGB')
                        image.save(ability_bytes, format='JPEG', quality=95)
                        ability_bytes.seek(0)
                        saved_image = True
                except Exception as ex:
                    # aiohttp isn't able to fetch for every ability image URL
                    print(f'Unable to create an image for {session.god.name}\'s '\
                          f'{ability.name}, {ex}')
            desc = 'Hint: Here\'s one of the god\'s abilities'
            session.current_round.file_bytes = ability_bytes
            session.current_round.file_name = self.ABILITY_IMAGE_FILE
            return await self.__send_round_and_wait_wrapper(desc, session)

    async def __send_god_title(self, session: SmiteleGame) -> bool:
        session.current_round.reset_file()
        return await self.__send_round_and_wait_wrapper(\
                f'The god has this title:\n```{session.god.title.title()}```', session)

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
                    crop_image.save(crop_file, format='JPEG', quality=95)
                    crop_file.seek(0)

                desc = 'Hint: This is a crop of the god\'s base skin'
                session.current_round.file_bytes = crop_file
                session.current_round.file_name = self.GOD_CROP_IMAGE_FILE
                return await self.__send_round_and_wait_wrapper(desc, session)

    # Loops until exp time, editing the message embed with a countdown
    async def __countdown_loop(self, message: discord.Message, \
            exp: float, embed: discord.Embed) -> None:
        while time.time() < exp:
            await asyncio.sleep(1)
            rem = math.ceil(exp-time.time())
            if rem >= 0:
                embed.set_field_at(\
                    0, name='Time Remaining:', value=f'_{rem} second{"s" if rem != 1 else ""}_')
                await message.edit(embed=embed)

    async def __send_incorrect(self, desc: str, last_round: bool, session: SmiteleGame) -> None:
        answer_image: discord.File = None
        embed: discord.Embed = None
        if not last_round:
            desc += '\n\nNext round coming up shortly.'
            embed = discord.Embed(color=discord.Color.red(), description=desc)
        else:
            await session.context.channel.typing()
            desc += f' The answer was **{session.god.name}**.'
            answer_image = discord.File(await session.skin.get_card_bytes(), \
                filename=f'{session.god.name}.jpg')
            embed = discord.Embed(color=discord.Color.red(), description=desc)
            embed.set_image(url=f'attachment://{session.god.name}.jpg')

        await session.context.channel.send(file=answer_image, embed=embed)

    def __send_round_and_wait_wrapper(self, description: str, \
            session: SmiteleGame) -> 'asyncio.Task[bool]':
        return session.add_task(\
            self.__bot.loop.create_task(\
                self.__send_round_and_wait_for_answer(description, session)))

    # Helper function for sending the repeated round messages to Discord
    async def __send_round_and_wait_for_answer(self, description: str, \
            session: SmiteleGame) -> bool:
        context = session.context
        round_ctx = session.current_round

        embed = discord.Embed(color=discord.Color.blue(), description=description)
        embed.title = f'Round {round_ctx.round_number}:'
        embed.add_field(name='Time Remaining:', value='_20 seconds_')
        if len(self.__running_sessions) > 1:
            embed.add_field(name='Player:', value=f'{context.player.mention}')

        # If we have an image file, this is how it gets attached to the embed
        picture: discord.File = None
        if round_ctx.has_file():
            picture = discord.File(round_ctx.file_bytes, filename=round_ctx.file_name)
            embed.set_image(url=f'attachment://{round_ctx.file_name}')

        exp = time.time() + 20
        sent = await context.channel.send(file=picture, embed=embed)
        task = session.add_task(self.__bot.loop.create_task(self.__countdown_loop(\
            sent, exp, embed)))
        if session.easy_mode:
            desc = ''
            for idx, choice in enumerate(session.choices):
                wrap = '~~' if choice[1] else '**'
                desc += f'**{idx + 1}**. {wrap}{choice[0].name}{wrap}\n'
            await context.channel.send(embed=discord.Embed(\
                color=discord.Color.blue(), title='Choices:', description=desc))
        try:
            msg = await asyncio.wait_for(\
                self.__wait_for_message(sent.id, session), timeout=20)
            if self.__check_answer_message(msg.content, session.god.name):
                answer_time = time.time() - (exp - 20)
                task.cancel()
                await msg.add_reaction('💯')
                await context.channel.typing()
                # These emojis are from my Discord server so I'll need to update these to be
                # more universal. :D
                ans_description = f'✅ Correct, **{context.player.mention}**! '\
                    f'You got it in {round(answer_time)} seconds. '\
                    f'The answer was **{session.god.name}**. '\
                    '<:frogchamp:566686914858713108>'

                embed = discord.Embed(color=discord.Color.green(), \
                    description=ans_description)
                file_name = f'{session.god.name}.jpg'
                picture = discord.File(await session.skin.get_card_bytes(), filename=file_name)
                embed.set_image(url=f'attachment://{file_name}')

                await context.channel.send(file=picture, embed=embed)
                return True
            if session.easy_mode:
                self.__update_choices(msg.content, session)
            task.cancel()
            await msg.add_reaction('❌')
            inc_description = f'❌ Incorrect, **{context.player.mention}**.'
            await self.__send_incorrect(inc_description, round_ctx.is_last_round(), session)
            return False
        except asyncio.TimeoutError:
            inc_description = '❌⏲️ Time\'s up! <:killmyself:472184572407447573>'
            await self.__send_incorrect(inc_description, round_ctx.is_last_round(), session)
            return False

    async def __wait_for_message(self, last_message_id: int, \
            game: SmiteleGame) -> discord.Message:
        channel = game.context.channel
        while channel.last_message_id == last_message_id or \
                not self.__validate_message(game) or \
                not await self.__check_answer_is_god(channel.last_message, game):
            await asyncio.sleep(0)
        return channel.last_message

    def __validate_message(self, game: SmiteleGame) -> bool:
        channel = game.context.channel
        return channel.last_message.author != self.__bot.user and \
                not channel.last_message.content.startswith('$') and \
                channel.last_message.author == game.context.player

    async def __check_answer_is_god(self, guess: discord.Message, game: SmiteleGame) -> bool:
        if any(self.__check_answer_message(guess.content, god.name) for god in self.__gods):
            return True
        await guess.add_reaction('❓')
        desc = f'**{guess.content}** is not a known god name!'
        await game.context.channel.send(\
            embed=discord.Embed(color=discord.Color.red(), description=desc))
        return False

    async def __write_to_file_from_api(self, file: io.TextIOWrapper, \
            refresher: Callable[[], Awaitable[Any]]) -> Any:
        tmp = await refresher()
        json.dump(tmp, file)
        return tmp

    async def __load_cache(self, file_name: str, \
            refresher: Callable[[], Awaitable[Any]] = None) -> List[Any]:
        tmp: List[Any] = []
        if refresher is not None:
            with open(file_name, 'w', encoding='utf-8') as file:
                tmp = await self.__write_to_file_from_api(file, refresher)
        else:
            try:
                with open(file_name, 'r+', encoding='utf-8') as file:
                    tmp = json.load(file)
            except (FileNotFoundError, JSONDecodeError):
                with open(file_name, 'w', encoding='utf-8') as file:
                    tmp = await self.__write_to_file_from_api(file, refresher)
        return tmp

    async def __should_refresh(self) -> Tuple[bool, float]:
        current_patch = float((await self.__smite_client.get_patch_info())['version_string'])
        try:
            with open(self.SMITE_PATCH_VERSION_FILE, 'r+', encoding='utf-8') as file:
                cached_version = float(file.read())
                if current_patch > cached_version:
                    print(f'Current local cache ({cached_version}) '\
                          f'is out of date ({current_patch}), refreshing')
                    return (True, current_patch)
        except (FileNotFoundError, ValueError):
            print('Failed to open version cache, refreshing')
            return (True, current_patch)

        return (False, None)

    async def __smitetrivia(self, message, *args):
        async def countdown_loop(message, exp, embed):
            while time.time() < exp:
                await asyncio.sleep(1)
                rem = math.ceil(exp-time.time())
                embed.set_field_at(0, name="Time Remaining:", value=f'_{rem} second{"s" if rem != 1 else ""}_')
                await message.edit(embed=embed)

        consumables_questions: List[Callable[[Item], dict]] = [
            lambda c: {
                "question": discord.Embed(description=f'How much does {"an" if c.name[0].lower() in "aeiou" else "a"} **{c.name}** cost?'),
                "answer": c.price,
                "id": f'{c.name}-1'
            },
            # lambda c: ((lambda _c, i: {
            #     "question": discord.Embed(description=f'How much **{_c["effects"][i]["stat"]}** does {"an" if c["name"][0].lower() in "aeiou" else "a"} **{_c["name"]}** provide?'),
            #     "answer": _c['effects'][i]['value'] if _c['effects'][i]['type'] == "flat" else f'{_c["effects"][i]["value"]}%',
            #     "id": f'{_c["name"]}{_c["effects"][i]["stat"]}-1'
            # })(c, random.randrange(len(c['effects'])))) if 'effects' in c.keys() else None,
            lambda c: {
                "question": discord.Embed(description=f'Name the consumable with this description: \n\n`{c.passive}`'),
                "answer": c.name,
                "id": f'{c.name}-2'
            } if c.passive is not None and c.passive != '' else {},
            # lambda c: {
            #     "question": discord.Embed(description=f'How long (in seconds) do the effects of {"an" if c.name[0].lower() in "aeiou" else "a"} **{c.name}** last?'),
            #     "answer": c.duration,
            #     "id": f'{c.name}-3'
            # } if 'duration' in c.keys() else None,
            lambda c: {
                "question": discord.Embed(description="What consumable is this?").set_image(url=c.icon_url),
                "answer": c.name,
                "id": f'{c.name}-4'
            },
            lambda _: {
                "question": discord.Embed(description="What is the range of a **Ward**?"),
                "answer": 45,
                "id": "consumables-5"
            },
            lambda _: {
                "question": discord.Embed(description="How long does a **Ward** last (in seconds)?"),
                "answer": 180,
                "id": "consumables-6"
            },
            lambda _: {
                "question": discord.Embed(description="How much True damage does **Hand of the Gods** do to Jungle Monsters?"),
                "answer": 200,
                "id": "consumables-7"
            },
            # lambda c: {
            #     "question": discord.Embed(description=f'What level must you be to purchase {"an" if c.name[0].lower() in "aeiou" else "a"} **{c.name}**?'),
            #     "answer": c.level,
            #     "id": f'{c.name}-8'
            # } if 'level' in c.keys() else None,
        ]

        relics_questions: List[Callable[[Item], dict]] = [
            # lambda relic: ((lambda r, i: {
            #     "question": discord.Embed(
            #         description=f'**{r["name"]} {r["effects"][i]["stat"]}** {"against" if r["effects"][i]["target"] == "enemies" else "of"} {r["effects"][i]["target"]} by how much?'),
            #     "answer": r['effects'][i]['value'] if r['effects'][i]['type'] == 'flat' else f'{r["effects"][i]["value"]}%',
            #     "id": f'{r["name"]}{r["effects"][i]["stat"]}-1'
            # })(relic, random.randrange(len(relic['effects'])))) if 'effects' in relic.keys() else None,
            lambda relic: {
                "question": discord.Embed(description=f'Name the relic with this description: \n\n`{relic.passive}`'),
                "answer": relic.name,
                "id": f'{relic.name}-1'
            } if relic.passive is not None and relic.passive != '' else {},
            # lambda relic: {
            #     "question": discord.Embed(description=f'What is the range of the relic **{relic["name"]}**?'),
            #     "answer": relic['range'],
            #     "id": f'{relic["name"]}-2'
            # } if 'range' in relic.keys() else None,
            # lambda relic: {
            #     "question": discord.Embed(description=f'What is the cooldown (in seconds) on the relic **{relic["name"]}**?'),
            #     "answer": relic['cooldown'],
            #     "id": f'{relic["name"]}-3'
            # } if 'cooldown' in relic.keys() else None,
            # lambda relic: {
            #     "question": discord.Embed(description=f'How long (in seconds) does the relic **{relic["name"]}** last?'),
            #     "answer": relic['duration'],
            #     "id": f'{relic["name"]}-4'
            # } if 'duration' in relic.keys() else None,
            lambda relic: {
                "question": discord.Embed(description="What relic is this?").set_image(url=relic.icon_url),
                "answer": relic.name,
                "id": f'{relic.name}-5'
            }
        ]

        ability_questions: List[Callable[[God, Ability], dict]] = [
            lambda god, ability: {
                "question": discord.Embed(description=f'Name **{god.name}**\'s ability with this description: \n\n`{ability.description}`'),
                "answer": ability.name,
                "id": f'{ability.name}-1'
            },
            # lambda god, ability: {
            #     "question": discord.Embed(description=f'Name **{god.name}**\'s ability with these properties: \n\n`{ability.ability_properties}`'),
            #     "answer": ability.name,
            #     "id": f'{ability.name}-2'
            # },
            lambda _, ability: {
                "question": discord.Embed(description="What ability is this?").set_image(url=ability.icon_url),
                "answer": ability.name,
                "id": f'{ability.name}-4'
            },
        ]

        god_questions: List[Callable[[God], dict]] = [
            lambda god: {
                "question": discord.Embed(description=f'Name the god with this lore: \n\n```{god.lore.replace(god.name, "_____")}```'),
                "answer": god.name,
                "id": f'{god.name}-1'
            },
            lambda god: {
                "question": discord.Embed(description=f'What pantheon is **{god.name}** a part of?'),
                "answer": god.pantheon,
                "id": f'{god.pantheon}-2'
            },
            lambda god: random.choice(ability_questions)(god, random.choice(god.abilities))
        ]

        def compute_price(item: Item):
            price = item.price
            parent_id = item.parent_item_id
            while parent_id is not None:
                parent = self.__items[parent_id]
                price += parent.price
                parent_id = parent.parent_item_id
            return price

        items_questions: List[Callable[[Item], dict]] = [
            lambda item: {
                "question": discord.Embed(description=f'How much does **{item.name}** cost?'),
                "answer": compute_price(item),
                "id": f'{item.name}-1'
            } if item.price > 1 else {},
            lambda item: ((lambda i, statIdx: {
                "question": discord.Embed(description=f'{"How much" if i.item_properties[statIdx].flat_value is not None else "What percent"} **{i.item_properties[statIdx].attribute.display_name}** does **{i.name}** provide?'),
                "answer": int(i.item_properties[statIdx].flat_value) if i.item_properties[statIdx].flat_value is not None else f'{int(i.item_properties[statIdx].percent_value * 100)}%',
                "id": f'{i.name}{i.item_properties[statIdx].attribute.value}-1'
            })(item, random.randrange(len(item.item_properties)))),
            lambda item: ((lambda i, statIdx: {
                "question": discord.Embed(description=f'{"How much" if i.item_properties[statIdx].flat_value is not None else "What percent"} **{i.item_properties[statIdx].attribute.display_name}** does this item provide?').set_image(url=item.icon_url),
                "answer": int(i.item_properties[statIdx].flat_value) if i.item_properties[statIdx].flat_value is not None else f'{int(i.item_properties[statIdx].percent_value * 100)}%',
                "id": f'{i.name}{i.item_properties[statIdx].attribute.value}-1'
            })(item, random.randrange(len(item.item_properties)))),
            lambda item: {
                    "question": discord.Embed(description=f'Name the item with this {"passive" if item.passive is not None and item.passive != "" else "aura"}:\n\n`{item.passive if item.passive is not None and item.passive != "" else item.aura}`'),
                    "answer": item.name,
                    "id": f'{item.name}-2'
                } if (item.passive is not None and item.passive != '') or (item.aura is not None and item.aura != '') else {
                    "question": discord.Embed(description=f'Name the item with this description:\n\n`{item.description}`'),
                    "answer": item.name,
                    "id": f'{item.name}-2'
                } if item.tier >= 3 and item.description is not None and item.description != '' else {},
            lambda item: {
                "question": discord.Embed(description=f'How much does it cost to upgrade **{self.__items[item.parent_item_id].name}** into **{item.name}**?'),
                "answer": item.price,
                "id": f'{item.name}-3'
            } if item.parent_item_id is not None else {},
            lambda item: {
                "question": discord.Embed(description="What item is this?").set_image(url=item.icon_url),
                "answer": item.name,
                "id": f'{item.name}-4'
            },
            # lambda item: {
            #     "question": discord.Embed(description=f'How many stacks does it take to fully stack **{item["name"]}**?'),
            #     "answer": item['stacks']['max'],
            #     "id": f'{item["name"]}-5'
            # } if 'stacks' in item else None,
            # lambda item: ((lambda i, stat: {
            #     "question": discord.Embed(description=f'{"How much" if i["stacks"]["per_stack"][stat]["type"] == "flat" else "What percent"} **{i["stacks"]["per_stack"][stat]["stat"]}** does one stack on **{i["name"]}** provide?'),
            #     "answer": i["stacks"]["per_stack"][stat]["value"],
            #     "id": f'{item["name"]}{i["stacks"]["per_stack"][stat]["stat"]}-6'
            # })(item, random.randrange(len(item['stacks']['per_stack'])))) if 'stacks' in item and 'per_stack' in item['stacks'] else None,
            # lambda item: ((lambda i, stat: {
            #     "question": discord.Embed(description=f'{"How much" if i["stacks"]["evolved"]["effects"][stat]["type"] == "flat" else "What percent"} **{i["stacks"]["evolved"]["effects"][stat]["stat"]}** does **Evolved {i["name"]}** provide?'),
            #     "answer": i["stacks"]["evolved"]["effects"][stat]["value"],
            #     "id": f'{item["name"]}{i["stacks"]["evolved"]["effects"][stat]["stat"]}-7'
            # })(item, random.randrange(len(item['stacks']['evolved']["effects"])))) if 'stacks' in item and 'evolved' in item['stacks'] and 'effects' in item['stacks']['evolved'] else None,
            # lambda item: {
            #     "question": discord.Embed(description=f'Name the **Evolved** item with this passive:\n\n`{item["stacks"]["evolved"]["passive"]}`'),
            #     "answer": item['name'],
            #     "id": f'{item["name"]}-8'
            # } if 'stacks' in item and 'evolved' in item['stacks'] and 'passive' in item['stacks']['evolved'] else None,
            # lambda item: ((lambda i, stat: {
            #     "question": discord.Embed(description=f'{"How much" if i["stacks"]["per_stack"][stat]["type"] == "flat" else "What percent"} **{i["stacks"]["per_stack"][stat]["stat"]}** does fully stacked **{i["name"]}** provide (including base stats)?'),
            #     "answer": (float(i["stacks"]["per_stack"][stat]["value"]) * int(i["stacks"]["max"])) + (int([s["value"] for s in i["effects"] if s["stat"] == i["stacks"]["per_stack"][stat]["stat"]][::] or 1) if "effects" in i else 1),
            #     "id": f'{item["name"]}{i["stacks"]["per_stack"][stat]["stat"]}-6'
            # })(item, random.randrange(len(item['stacks']['per_stack'])))) if 'stacks' in item and 'per_stack' in item['stacks'] and 'evolved' not in item['stacks'] else None,
            # lambda item: {
            #     "question": discord.Embed(description=f'What items does {item["name"]} upgrade directly into ({len(item["upgrades"])} answers)?'),
            #     "answer": [items[u["upgradeId"]]["name"] for u in item["upgrades"]]
            # } if 'upgrades' in item else None,
        ]
        if message.author == self.__bot.user:
            return

        items = []
        consumables = []
        relics = []

        for i in self.__items.values():
            if not i.active:
                continue

            if i.type == ItemType.CONSUMABLE:
                consumables.append(i)
            if i.type == ItemType.ITEM:
                items.append(i)
            if i.type == ItemType.RELIC:
                relics.append(i)

        question_mapping = {
            "items": {
                "values": items,
                "questions": items_questions
            },
            "consumables": {
                "values": consumables,
                "questions": consumables_questions
            },
            "relics": {
                "values": relics,
                "questions": relics_questions
            },
            "gods": {
                "values": self.__gods,
                "questions": god_questions
            }
        }

        question_count = 1
        input_category = None
        correct_answers = {}
        was_stopped = False
        asked_questions = set()

        if args is not None:
            args = [''.join(arg) for arg in args]
            if len(args) > 2:
                await message.channel.send(embed=discord.Embed(color=discord.Color.red(), description="Invalid command! This bot accepts the command `$smitetrivia` (or `$st`) with optional count and category arguments, e.g. `$smitetrivia 10 items`"))
                return
            if len(args) == 2:
                try:
                    question_count = int(args[0])
                    if question_count > 20:
                        await message.channel.send(embed=discord.Embed(color=discord.Color.red(), description="The maximum allowed questions per round is 20."))
                        return
                except ValueError:
                    await message.channel.send(embed=discord.Embed(color=discord.Color.red(), description="Question count must be a number."))
                    return

                if args[1] not in question_mapping.keys():
                    await message.channel.send(embed=discord.Embed(color=discord.Color.red(), description=f'\'{args[1]}\' is not a valid question category.'))
                    return
                input_category = args[1]
            elif len(args) == 1:
                try:
                    question_count = int(args[0])
                    if question_count > 20:
                        await message.channel.send(embed=discord.Embed(color=discord.Color.red(), description="The maximum allowed questions per round is 20."))
                        return
                except ValueError:
                    await message.channel.send(embed=discord.Embed(color=discord.Color.red(), description="Question count must be a number."))
                    return
        for q in range(question_count):
            question: dict = {}
            category = input_category

            if category is None:
                category = random.choices(list(question_mapping.keys()), weights=[10, 1, 2, 10])[0]

            question_pool = question_mapping[category]["questions"]
            input_objects = question_mapping[category]["values"]
            input_object = random.choice(list(input_objects))
                
            while not any(question) or question['id'] in asked_questions:
                question = random.choice(question_pool)(input_object)

            asked_questions.add(question['id'])

            question['question'].title = f'❔ _Question **{q+1}** of **{question_count}**_' if question_count > 1 else "❔ _Question_"
            question['question'].color = discord.Color.blue()
            question['question'].add_field(name="Time Remaining:", value="_20 seconds_")
            answers = {}

            def check(m: discord.Message):
                correct = False
                if m.author == self.__bot.user:
                    return False

                if m.content.startswith("%stop"):
                    loop = asyncio.get_running_loop()
                    loop.create_task(message.channel.send(embed=discord.Embed(color=discord.Color.red(), description="Trivia round canceled!")))
                    raise StoppedError

                if m.author not in answers.keys():
                    answers[m.author] = {
                        "answered": 1,
                        "warned": False
                    }
                else:
                    answers[m.author]["answered"] += 1

                answer = str(question['answer']).lower().replace("-", " ")
                correct = answer == unidecode(m.content).lower().replace("-", " ")

                if not correct and not answer.replace("%", "").isdigit():
                    if answer.startswith("the") and not m.content.lower().startswith("the"):
                        answer = answer.replace("the ", "")

                    correct = edit_distance.SequenceMatcher(a=answer, b=m.content.lower()).distance() <= 2
                elif not correct and answer.replace("%", "").isdigit() and m.content.replace("%", "").isdigit() and answers[m.author]["answered"] < 3:
                    guess = int(m.content.replace("%", ""))
                    answer_number = int(answer.replace("%", ""))
                    loop = asyncio.get_running_loop()

                    if guess < answer_number:
                        loop.create_task(message.channel.send(
                            embed=discord.Embed(
                                color=discord.Color.blue(),
                                description=f'Not quite, {m.author.mention}, try a higher guess. ↗️')))
                    else:
                        loop.create_task(message.channel.send(
                            embed=discord.Embed(
                                color=discord.Color.blue(),
                                description=f'Not quite, {m.author.mention}, try a lower guess. ↘️')))

                if correct and answers[m.author]["answered"] <= 3:
                    return correct

                if answers[m.author]["answered"] >= 3 and not answers[m.author]["warned"]:
                    loop = asyncio.get_running_loop()
                    loop.create_task(message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.red(),
                            description=f'{m.author.mention}, you\'ve reached your maximum number of guesses. <:noshot:782396496104128573> Try again next question!')))
                    answers[m.author]["warned"] = True
                    return False

            exp = time.time() + 20
            task = asyncio.get_running_loop().create_task(countdown_loop(await message.channel.send(embed=question['question']), exp, question['question']))
            try:
                msg = await self.__bot.wait_for('message', check=check, timeout=20)
                answer_time = time.time() - (exp - 20)
                task.cancel()
                description = f'✅ Correct, **{msg.author.display_name}**! You got it in {round(answer_time)} seconds. The answer was **{question["answer"]}**. <:frogchamp:566686914858713108>'
                if q < question_count - 1:
                    description += "\n\nNext question coming up in 5 seconds."
                    
                await message.channel.send(embed=discord.Embed(color=discord.Color.green(), description=description))

                if msg.author.id not in correct_answers:
                    correct_answers[msg.author.id] = 1
                else:
                    correct_answers[msg.author.id] += 1
                if q < question_count - 1:
                    await asyncio.sleep(5)
            except asyncio.TimeoutError:
                description = f'❌⏲️ Time\'s up! The answer was **{question["answer"]}**. <:killmyself:472184572407447573>'
                if q < question_count - 1:
                    description += "\n\nNext question coming up in 5 seconds."
                    
                await message.channel.send(embed=discord.Embed(color=discord.Color.red(), description=description))
                if q < question_count - 1:
                    await asyncio.sleep(5)
            except StoppedError:
                was_stopped = True
                task.cancel()
                break

        if not was_stopped and bool(correct_answers):
            description = [f'**{idx + 1}**. _{(await self.__bot.fetch_user(u[0])).display_name}_ (Score: **{u[1]}**) {"<:mleh:472905075208093717>" if idx == 0 else ""}' for idx, u in enumerate(sorted(correct_answers.items(), key=lambda i: i[1], reverse=True))]
            embed = discord.Embed(color=discord.Color.blue(), title="**Round Summary:**", description=str.join("\n", description))
            await message.channel.send(embed=embed)

            current_scores = {}
            try:
                with open("scores.json", "r") as f:
                    current_scores = json.load(f)
            except (FileNotFoundError, JSONDecodeError):
                pass
            if current_scores:
                for u in correct_answers.keys():
                    if str(u) not in current_scores:
                        current_scores[str(u)] = correct_answers[u]
                    else:
                        current_scores[str(u)] += correct_answers[u]
            else:
                current_scores = correct_answers

            with open("scores.json", "w") as f:
                json.dump(current_scores, f)

    async def __scores(self, ctx):
        try:
            with open("scores.json", "r") as f:
                current_scores = json.load(f)
                current_scores = sorted(current_scores.items(), key=lambda i: i[1], reverse=True)
                description = [f'**{idx + 1}**. _{(await self.__bot.fetch_user(u[0])).display_name}_ (Score: **{u[1]}**) {"<:mleh:472905075208093717>" if idx == 0 else ""}' for idx, u in enumerate(current_scores)]
                embed = discord.Embed(color=discord.Color.blue(), title="**Leaderboard:**", description=str.join("\n", description)).set_thumbnail(url=(await self.__bot.fetch_user(current_scores[0][0])).display_avatar.url)
                await ctx.channel.send(embed=embed)
        except (FileNotFoundError, JSONDecodeError):
            await ctx.channel.send(embed=discord.Embed(color=discord.Color.blue(), title="No scores recorded yet!"))

if __name__ == '__main__':
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix='$', intents=intents)
    smitele = Smitele(bot)
    asyncio.run(bot.add_cog(smitele))
    smitele.start_bot()
