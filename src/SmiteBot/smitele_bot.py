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
from typing import Any, Callable, Coroutine, Dict, List, Set, Tuple

import aiohttp
import discord
import edit_distance
from bs4 import BeautifulSoup
from discord.ext import commands
from PIL import Image
from unidecode import unidecode

from build_optimizer import BuildOptimizer
from god import God
from god_types import GodId, GodType
from item import Item, ItemType
from skin import Skin
from SmiteProvider import SmiteProvider
from smitetrivia import SmiteTrivia
from HirezAPI import Smite, PlayerRole, QueueId, TierId

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
    SKIN_IMAGE_FILE: str = 'skin.jpg'
    SKIN_CROP_IMAGE_FILE: str = 'crop.jpg'
    VOICE_LINE_FILE: str = 'voice.ogg'

    __bot: commands.Bot

    __gods: Dict[GodId, God]

    # Cached config values
    __config: dict = None

    __items: Dict[int, Item]

    # Mapping of session IDs to running games
    __running_sessions: Dict[int, SmiteleGame]

    __smite_client: SmiteProvider

    # A helper lambda for hitting a random Smite wiki voicelines route
    __get_base_smite_wiki: Callable[[commands.Cog, str], str] = \
        lambda self, name: f'https://smite.fandom.com/wiki/{name}_voicelines'

    def __init__(self, _bot: commands.Bot, provider: SmiteProvider) -> None:
        # Setting our intents so that Discord knows what our bot is going to do
        self.__bot = _bot
        self.__smite_client = provider
        self.__gods = provider.gods
        self.__items = provider.items
        self.__running_sessions = {}

        if self.__config is None:
            try:
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as file:
                    self.__config = json.load(file)

                    if not 'discordToken' in self.__config:
                        raise RuntimeError(f'{self.CONFIG_FILE} '\
                            'was missing value for "discordToken."')
            except (FileNotFoundError, JSONDecodeError) as exc:
                raise RuntimeError(f'Failed to load {self.CONFIG_FILE}. Does this file exist?') \
                    from exc

        self.__smite_client = Smite(self.__config['hirezAuthKey'], self.__config['hirezDevId'])

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        activity = discord.Game(name='Smite', type=3)
        await self.__bot.change_presence(status=discord.Status.online, activity=activity)
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

    @commands.command(aliases=['w'])
    async def worshippers(self, message: discord.Message, *args: tuple):
        async def send_invalid(additional_info: str = 'Invalid command!', include_command_info: bool = True):
            desc = f'{additional_info}'
            if include_command_info:
                    desc += f' {self.__bot.user.mention} accepts the command `$worshippers [playername] [godname]` '\
                    '(or `$w [playername] [godname]`)'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))
        flatten_args = [''.join(arg) for arg in args]
        if not any(flatten_args):
            await send_invalid()
            return

        player_name = flatten_args[0]
        god_name: str | None = None
        god_id: GodId | None = None
        if len(flatten_args) > 1:
            god_name = ' '.join(flatten_args[1:])

            try:
                god_id = GodId[god_name.upper().replace(' ', '_')\
                    .replace("'", '')]
            except KeyError:
                await send_invalid(f'{god_name} is not a valid god!')
                return

        players = await self.__smite_client.get_player_id_by_name(player_name)
        if not any(players):
            await send_invalid('No players with that name found!', False)
            return
        if players[0]['privacy_flag'] == 'y':
            await send_invalid(f'{player_name} has their profile hidden... <:reeratbig:849771936509722634>', False)
            return

        god_ranks = await self.__smite_client.get_god_ranks(players[0]['player_id'])
        stats = {
            GodId(int(god['god_id'])):{
                'assists': int(god['Assists']),
                'deaths': int(god['Deaths']),
                'kills': int(god['Kills']),
                'losses': int(god['Losses']),
                'rank': int(god['Rank']),
                'wins': int(god['Wins']),
                'worshippers': int(god['Worshippers']),
                'minions': int(god['MinionKills']),
            } for god in god_ranks
        }

        player_details = (await self.__smite_client.get_player(players[0]['player_id']))[0]
        stats_embed = discord.Embed(
                color=discord.Color.blue(), title=f'{player_details["Name"]}\'s {self.__gods[god_id].name if god_id is not None else "Overall"} Stats')
        if god_id is not None:
            if god_id not in stats:
                await send_invalid(f'{player_name} doesn\'t have any worshippers for {self.__gods[god_id].name}!', False)
                return

            god_stats = stats[god_id]
            kills = god_stats['kills']
            assists = god_stats['assists']
            deaths = god_stats['deaths']
            avg_kda = (kills + (assists / 2)) / (deaths if deaths > 0 else 1)
            kda = f'• _Kills_: {kills:,}\n• _Deaths_: {deaths:,}\n• _Assists_: {assists:,}'\
                f'\n• _Avg. KDA_: {avg_kda:.2f}\n• _Minion Kills_: {god_stats["minions"]:,}'
            wins = god_stats['wins']
            losses = god_stats['losses']
            wlr = f'• _Wins_: {wins:,}\n• _Losses_: {losses:,}\n• _Win Percent_: {int((wins / (wins + losses)) * 100)}%'
            worshippers = f'_Worshippers_: {god_stats["worshippers"]:,} (_Rank {god_stats["rank"]:,}_)'

            stats_embed.add_field(name='KDA', value=kda)
            stats_embed.add_field(name='Win/Loss Ratio', value=wlr)
            stats_embed.add_field(name='Worshippers', value=worshippers)
            stats_embed.set_thumbnail(url=self.__gods[god_id].icon_url)

            await message.channel.send(embed=stats_embed)
            return
        
        total_kills = sum(god['kills'] for _, god in stats.items())
        total_assists = sum(god['assists'] for _, god in stats.items())
        total_deaths = sum(god['deaths'] for _, god in stats.items())
        total_avg_kda = (total_kills + (total_assists / 2)) / (total_deaths if total_deaths > 0 else 1)
        total_minions = sum(god['minions'] for _, god in stats.items())
        total_kda = f'• _Total Kills_: {total_kills:,}\n• _Total Deaths_: {total_deaths:,}\n• _Total Assists_: {total_assists:,}'\
            f'\n• _Overall Avg. KDA_: {total_avg_kda:.2f}\n• _Total Minion Kills_: {total_minions:,}'

        total_wins = sum(god['wins'] for _, god in stats.items())
        total_losses = sum(god['losses'] for _, god in stats.items())
        total_wlr = f'• _Total Wins_: {total_wins:,}\n• _Total Losses_: {total_losses:,}\n• _Overall Win Percent_: {int((total_wins / (total_wins + total_losses)) * 100)}%'

        total_worshippers = sum(god['worshippers'] for _, god in stats.items())
        total_worshippers_str = f'_Total Worshippers_: {total_worshippers:,}'

        stats_embed.add_field(name='Overall KDA', value=total_kda)
        stats_embed.add_field(name='Overall Win/Loss Ratio', value=total_wlr)
        stats_embed.add_field(name='Overall Worshippers', value=total_worshippers_str)
        stats_embed.set_thumbnail(url=player_details['Avatar_URL'])

        await message.channel.send(embed=stats_embed)

    @commands.command(aliases=['live', 'current'])
    async def livematch(self, message: discord.Message, *args: tuple):
        async def send_invalid(additional_info: str = 'Invalid command!', include_command_info: bool = True):
            desc = f'{additional_info}'
            if include_command_info:
                    desc += f' {self.__bot.user.mention} accepts the command `$livematch [playername]` '\
                    '(or `$live [playername]`)'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))
        flatten_args = [''.join(arg) for arg in args]
        if not any(flatten_args) or len(flatten_args) > 1:
            await send_invalid()
            return

        player_name = flatten_args[0]
        players = await self.__smite_client.get_player_id_by_name(player_name)
        if not any(players):
            await send_invalid('No players with that name found!', False)
            return
        if players[0]['privacy_flag'] == 'y':
            await send_invalid(f'{player_name} has their profile hidden... <:reeratbig:849771936509722634>', False)
            return
        player_status = await self.__smite_client.get_player_status(players[0]['player_id'])
        if int(player_status[0]['status']) != 3:
            await send_invalid(f'{player_name} is not currently in a game!', False)
            return
        live_match = await self.__smite_client.get_match_player_details(player_status[0]['Match'])
        try:
            queue_id = QueueId(int(live_match[0]['Queue']))
        except (KeyError, ValueError):
            print(f'Unsupported queue type: {live_match[0]["Queue"]}')
            await send_invalid('Unfortunately, the match type this player is playing is not currently supported.', False)
            return
        team_order = list(filter(lambda p: int(p['taskForce']) == 1, live_match))
        team_chaos = list(filter(lambda p: int(p['taskForce']) == 2, live_match))

        def get_rank_string(tier_id: TierId, mmr: float) -> str:
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
            return f'{emoji} **{tier_name}** ({int(mmr)} MMR)'

        is_ranked = queue_id in (
            QueueId.RANKED_CONQUEST,
            QueueId.RANKED_CONQUEST_CONTROLLER,
            QueueId.RANKED_DUEL,
            QueueId.RANKED_DUEL_CONTROLLER,
            QueueId.RANKED_JOUST,
            QueueId.RANKED_JOUST_CONTROLLER)

        def create_team_output(team_list: list) -> str:
            output = ''
            for player in team_list:
                ranked = ''
                if is_ranked:
                    ranked += f' - {get_rank_string(TierId(int(player["Tier"])), float(player["Rank_Stat"]))}'
                player_name = player['playerName']
                if player_name == '':
                    player_name = 'Hidden Player'
                output += f'• **{player_name}** ({player["GodName"]}){ranked}\n'
            return output

        players_embed = discord.Embed(
                color=discord.Color.blue(),
                title=f'{player_name}\'s Live '\
                      f'{queue_id.name.replace("_", " ").title()} Details')

        players_embed.add_field(name='🔵 Order Side', value=create_team_output(team_order))
        players_embed.add_field(name='🔴 Chaos Side', value=create_team_output(team_chaos))

        await message.channel.send(embed=players_embed)

    @commands.command()
    async def crymore(self, context: commands.Context):
        cry_more_url = 'https://static.wikia.nocookie.net/smite_gamepedia/'\
            'images/3/3e/Nerd_Rage_Cabrakan_Other_S.ogg/revision/latest?cb=20170325002129'
        cry_more_file = 'crymore.ogg'
        async with aiohttp.ClientSession() as client:
            async with client.get(cry_more_url) as res:
                # If the current player is in a voice channel,
                # connect to it and play the voice line!
                if context.author.voice is not None:
                    with open(cry_more_file, 'wb') as voice_file:
                        voice_file.write(await res.content.read())
                    voice_client = await context.author.voice.channel.connect()

                    async def disconnect():
                        await voice_client.disconnect()
                        os.remove(cry_more_file)

                    voice_client.play(discord.FFmpegPCMAudio(source=cry_more_file), \
                        after=lambda _: asyncio.run_coroutine_threadsafe(\
                            coro=disconnect(), loop=voice_client.loop).result())

    @commands.command()
    async def swog(self, context: commands.Context):
        await context.channel.send(
            embed=discord.Embed(
                color=discord.Color.blue(), title='You\'ve got the wrong bot.'))

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
        try:
            god = self.__gods[god_id]
        except KeyError:
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
        game = SmiteleGame(random.choice(list(self.__gods.values())), context)
        if easy_mode:
            game.generate_easy_mode_choices(list(self.__gods.values()))
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
            desc = 'Hint: A top-ranked player of this god recently used this build.'
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
        if any(self.__check_answer_message(guess.content, god.name) for god in list(self.__gods.values())):
            return True
        await guess.add_reaction('❓')
        desc = f'**{guess.content}** is not a known god name!'
        await game.context.channel.send(\
            embed=discord.Embed(color=discord.Color.red(), description=desc))
        return False

if __name__ == '__main__':
    intents = discord.Intents.default()
    # pylint: disable=assigning-non-slot
    intents.message_content = True
    bot = commands.Bot(command_prefix='$', intents=intents)
    provider = SmiteProvider()
    asyncio.run(provider.create())
    smitele = Smitele(bot, provider)
    smite_triva = SmiteTrivia(bot, provider.gods, provider.items)
    asyncio.run(bot.add_cog(smitele))
    asyncio.run(bot.add_cog(smite_triva))
    smitele.start_bot()
