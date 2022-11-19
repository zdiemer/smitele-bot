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
from json.decoder import JSONDecodeError
from typing import Awaitable, Callable, Dict, List, Tuple

import aiohttp
import discord
import editdistance
from bs4 import BeautifulSoup
from discord.ext import commands
from PIL import Image
from unidecode import unidecode

from HirezAPI import Smite, QueueId
from god import God
from item import Item
from skin import Skin

class SmiteleGameContext(object):
    """A class for holding Discord context for a Smitele Game.

    SmiteleGameContext holds all of the contextual (non-game) objects
    required for a game session to be ran. This includes the player (discord.User)
    and the discord.TextChannel where the game was initiated.

    Attributes:
        player: A discord.User which represents the user for a SmiteleGame
        channel: A discord.TextChannel which represents the channel where a game is running
    """
    player: discord.User
    channel: discord.TextChannel

    def __init__(self, player: discord.User, channel: discord.TextChannel):
        """Inits SmiteleGameContext with a player and a channel"""
        self.player = player
        self.channel = channel

    def __key(self):
        """Internal method for returning a hash key"""
        return (self.player.id, self.channel.id)

    def __hash__(self):
        """Internal method for hashing a SmiteleGameContext"""
        return hash(self.__key())

    def __eq__(self, other):
        """Internal method for equality comparison"""
        if isinstance(other, SmiteleGameContext):
            # pylint: disable=protected-access
            return self.__key() == other.__key()
        return NotImplemented

class SmiteleGame(object):
    """A class for holding all information about a running Smitele Game.

    SmiteleGame is a class for holding onto a combination of Discord context
    and Smitele specific context. The Discord context indicates who and where the
    game is being played, while the Smitele specific context hosts the answer for
    the current game.

    Attributes:
        context: A SmiteleGameContext object holding Discord related context
        god: A God object which indicates the answer to this particular game
    """
    context: SmiteleGameContext
    god: God
    __tasks: List[asyncio.Task] = []

    def __init__(self, answer: God, context: SmiteleGameContext):
        """Inits SmiteleGame given an answer God and context"""
        self.god = answer
        self.context = context

    def cancel(self) -> None:
        """Cancels a running SmiteleGame.

        This method cancels a running SmiteleGame by ending its related
        sub-tasks.
        """
        for task in self.__tasks:
            if task is not None and not task.done():
                task.cancel()
        self.__tasks.clear()

    def get_session_id(self) -> int:
        """Fetches a session ID for the SmiteleGame.

        This method returns a game session ID, which corresponds to a hash
        of the context.

        Returns:
            An integer session ID
        """
        return hash(self.context)

    def add_task(self, task: asyncio.Task) -> None:
        """Adds a task to the running SmiteleGame.

        This method appends a task to the running SmiteleGame, allowing a
        sub-task to be canceled if the game is also canceled.

        Args:
            task: An asyncio.Task to be appended to this game's task list

        Returns:
            The task that was added to the list
        """
        self.__tasks.append(task)
        return task

class _SmiteleRoundContext(object):
    """A helper context object for controlling round state.

    This class is used for referencing the context of a given round, allowing
    for easier access to some shared state on a round by round basis.

    Attributes:
        TOTAL_ROUNDS: A constant indicating the total number of rounds in a game.
        file_bytes: If a file is to be attached to this round's output, this represents its bytes.
        file_name: If a file is to be attahced to this round's output, this represents its name.
        round_number: The round that this context represents.
    """
    TOTAL_ROUNDS = 6

    file_bytes: io.BytesIO
    file_name: str
    round_number: int

    def __init__(self, round_number: int, file_bytes: io.BytesIO = None, file_name: str = None):
        """Inits _SmiteleRoundContext given a round number, and optional file info."""
        self.file_bytes = file_bytes
        self.file_name = file_name
        self.round_number = round_number

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
        return self.round_number == self.TOTAL_ROUNDS

class SmiteleBot(commands.Bot):
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
    BUILD_IMAGE_FILE: str = 'build.png'
    CONFIG_FILE: str = 'config.json'
    GOD_IMAGE_FILE: str = 'god.jpg'
    GOD_CROP_IMAGE_FILE: str = 'godCrop.jpg'
    GODS_FILE: str = 'gods.json'
    ITEMS_FILE: str = 'items.json'
    SKIN_IMAGE_FILE: str = 'skin.jpg'
    SKIN_CROP_IMAGE_FILE: str = 'crop.jpg'
    SMITE_PATCH_VERSION_FILE: str = 'version'
    VOICE_LINE_FILE: str = 'voice.ogg'

    # Cached config values
    __config: dict = None

    # Cached getgods in memory
    __gods: List[God] = []

    # Wrapper client around Hirez API
    __smite_client: Smite

    # Cached getitems in memory
    __items: List[Item] = []

    # Mapping of session IDs to running games
    __running_sessions: Dict[int, SmiteleGame] = {}

    # A helper lambda for hitting a random Smite wiki voicelines route
    __get_base_smite_wiki: Callable[[commands.Bot, str], str] = \
        lambda self, name: f'https://smite.fandom.com/wiki/{name}_voicelines'

    def __init__(self):
        # Setting our intents so that Discord knows what our bot is going to do
        intents = discord.Intents.default()
        intents.messages = True
        intents.voice_states = True
        super().__init__(command_prefix='$', intents=intents)

        if self.__config is None:
            try:
                with open(self.CONFIG_FILE, 'r') as file:
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
            except JSONDecodeError:
                raise RuntimeError(f'Failed to load {self.CONFIG_FILE}. Does this file exist?')

        self.__smite_client = Smite(self.__config['hirezAuthKey'], self.__config['hirezDevId'])

        @self.event
        # pylint: disable=unused-variable
        async def on_ready():
            activity = discord.Game(name='Smite', type=3)
            await self.change_presence(status=discord.Status.online, activity=activity)

            should_refresh, current_patch = await self.__should_refresh()

            gods = await self.__load_cache(self.GODS_FILE, \
                self.__smite_client.get_gods if should_refresh else None)
            self.__gods = [God.from_json(god) for god in gods]

            items = await self.__load_cache(self.ITEMS_FILE, \
                self.__smite_client.get_items if should_refresh else None)
            self.__items = [Item.from_json(item) for item in items]

            if should_refresh:
                with open(self.SMITE_PATCH_VERSION_FILE, 'w') as file:
                    file.write(str(current_patch))

            print('Smite-le Bot is ready!')


        @self.command()
        async def smitele(message: discord.Message, *args: tuple):
            await self.__smitele(message, *args)

        @self.command()
        # pylint: disable=unused-variable,invalid-name
        async def st(message: discord.Message, *args: tuple):
            await smitele(message, *args)

        @self.command()
        # pylint: disable=unused-variable,invalid-name
        async def stop(message: discord.Message, *args: tuple):
            await self.__stop(message, *args)

        @self.command()
        @commands.is_owner()
        # pylint: disable=unused-variable,invalid-name
        async def sessionid(message: discord.Message, *args: tuple):
            game_session_id = hash(SmiteleGameContext(message.author, message.channel))
            if len(args) > 0:
                game_session_id = hash((''.join(args[0]), message.channel.id))

            if game_session_id in self.__running_sessions:
                await message.channel.send(embed=discord.Embed(color=discord.Color.gold(), \
                    description=f'Running session ID = {game_session_id}'))
            else:
                await message.channel.send(embed=discord.Embed(color=discord.Color.gold(), \
                    description='No running game session'))

        @self.command()
        @commands.is_owner()
        # pylint: disable=unused-variable,invalid-name,unused-argument
        async def sessions(message: discord.Message, *args: tuple):
            if len(self.__running_sessions) == 0:
                await message.channel.send(embed=discord.Embed(color=discord.Color.gold(), \
                    description='No running game sessions'))
            else:
                output_msg = ''
                for game_session_id in self.__running_sessions:
                    context = self.__running_sessions[game_session_id].context
                    output_msg += f'> {context.player.mention} '\
                        f'({context.channel.mention}): **{game_session_id}**\n'

                await message.channel.send(embed=discord.Embed(color=discord.Color.gold(), \
                    description=output_msg))

        @self.command()
        # pylint: disable=unused-variable,invalid-name,unused-argument
        async def resign(message: discord.Message, *args: tuple):
            game_session_id = hash(SmiteleGameContext(message.author, message.channel))
            if game_session_id in self.__running_sessions:
                session = self.__running_sessions[game_session_id]
                await self.__send_incorrect('Round Resigned!', True, session)
                self.__try_stop_running_game_session(game_session_id)
            else:
                await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                    description='No running game session!'))

        @self.command(aliases=["quit"])
        @commands.is_owner()
        # pylint: disable=unused-variable,invalid-name,unused-argument
        async def shutdown(message: discord.Message):
            await message.channel.send(embed=discord.Embed(color=discord.Color.gold(), \
                description='Closing SmiteleBot'))
            await self.change_presence(status=discord.Status.offline)
            await self.close()

    def start_bot(self):
        """
        Using this command instead of just calling self.run() since the discordToken is loaded
        as part of this class.
        """
        self.run(self.__config['discordToken'])

    async def __stop(self, message: discord.Message, *args: tuple):
        game_session_id = hash(SmiteleGameContext(message.author, message.channel))

        if len(args) > 0:
            try:
                if not await self.is_owner(message.author):
                    await message.channel.send(embed=discord.Embed(\
                        color=discord.Color.red(), \
                        title='Can\'t stop another player\'s game!'))
                    return
                game_session_id = int(''.join(args[0]))
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
    def __check_answer_message(message: discord.Message, answer: str) -> bool:
        guess = unidecode(message.content).lower().replace('-', ' ')
        return guess == answer.lower() or \
            editdistance.eval(guess, answer.lower()) <= 2

    # Primary command for starting a round of Smite-le!
    async def __smitele(self, message: discord.Message, *args: tuple) -> None:
        if message.author == self.user:
            return

        if len(self.__gods) == 0:
            desc = 'Smite-le Bot has not finished initializing.'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))
            return

        context = SmiteleGameContext(message.author, message.channel)

        if hash(context) in self.__running_sessions:
            desc = 'Can\'t start another game, '\
                f'**{context.player.mention}** already has a running game!'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))
            return

        if args is not None:
            if len(args) > 0:
                desc = 'Invalid command! This bot accepts the command `$smitele` (or `$st`)'
                await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                    description=desc))
                return

        await message.channel.send(embed=discord.Embed(color=discord.Color.blue(), \
            title='Smite-le Started!', \
            description='Name the god given the clues! You\'ll have six attempts.'))

        # Fetching a random god from our list of cached gods
        game = SmiteleGame(random.choice(self.__gods), context)
        try:
            await game.add_task(asyncio.get_running_loop().create_task(\
                self.__run_game_session(game)))
        # pylint: disable=broad-except
        except Exception as ex:
            desc = f'{self.user.mention} encountered a fatal error. Please try again later.'
            await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
                description=desc))
            print(f'Fatal exception encountered: {ex}')
            game.cancel()

    async def __run_game_session(self, session: SmiteleGame) -> None:
        game_session_id = session.get_session_id()
        self.__running_sessions[game_session_id] = session

        # Fetching skins for this god, used in multiple rounds
        skins = [Skin.from_json(skin) for skin in \
            await self.__smite_client.get_god_skins(session.god.id)]

        build_task = session.add_task(asyncio.get_event_loop().create_task(self.__prefetch_build_image(session)))

        # Round 1
        if await self.__round_one(session, skins):
            del self.__running_sessions[game_session_id]
            return

        # Round 2
        if await self.__round_two(session, build_task):
            del self.__running_sessions[game_session_id]
            return

        # Round 3
        if await self.__round_three(session, skins):
            del self.__running_sessions[game_session_id]
            return

        # Round 4
        if await self.__round_four(session):
            del self.__running_sessions[game_session_id]
            return

        # Round 5
        if await self.__round_five(session):
            del self.__running_sessions[game_session_id]
            return

        # Round 6
        base_skin = next(skin for skin in skins if skin.name == f'Standard {session.god.name}')
        await self.__round_six(session, base_skin)
        del self.__running_sessions[game_session_id]

    async def __prefetch_build_image(self, session: SmiteleGame) -> io.BytesIO:
        # Maps item ID -> position in build
        build: Dict[int, int] = {}

        # Hirez's route for getting recommended items is highly out of date, so we'll get a
        # top Ranked Conquest player's build
        god_leaderboard = await self.__smite_client.get_god_leaderboard(\
            session.god.id, QueueId.RANKED_CONQUEST)

        while len(build) == 0:
            # Fetching a random player from the leaderboard
            random_player = random.choice(god_leaderboard)

            # Scraping their recent match history to try and find a current build
            match_history = await self.__smite_client.get_match_history(\
                int(random_player['player_id']))
            for match in match_history:
                # Get a full build for this god
                if int(match['GodId']) == session.god.id.value and int(match['ItemId6']) != 0:
                    for i in range(1, 7):
                        # Luckily `getmatchhistory` includes build info!
                        build[int(match[f'ItemId{i}'])] = i

        # Maps position in build -> item
        items: Dict[int, Item] = {}
        for item in self.__items:
            if item.id in build:
                items[build[item.id]] = item

        # Appending the images into a single build image
        images: List[Image.Image] = []
        thumb_size = 128
        total_width = 0
        total_height = 128
        for key in sorted(items):
            # First requesting and saving the image from the URLs we got
            item_bytes = await items[key].get_icon_bytes()
            try:
                # Sometimes we don't get a full build, so this sets the final size of the build
                # image
                if key <= 3:
                    total_width += thumb_size
                if key == 4:
                    total_height += thumb_size
                image = Image.open(item_bytes)
                # Resize the image if necessary, Hirez doesn't return a consistent size
                if image.size != (thumb_size, thumb_size):
                    image.thumbnail((thumb_size, thumb_size))
                images.append(image)
            except Exception as ex:
                print(f'Unable to create an image for {items[key].name}, {ex}')

        # To be fancy, we'll give our output build image an alpha channel, in case some images are
        # missing ;)
        output_image = Image.new('RGBA', (total_width, total_height), (250, 250, 250, 0))
        pos_x, pos_y = (0, 0)

        # Enumerating through the images that successfully saved and resized, then pasting them to
        # our new canvas
        for idx, img in enumerate(images):
            output_image.paste(img, (pos_x, pos_y))
            if idx != 2:
                pos_x += img.size[0]
            if idx == 2:
                pos_x, pos_y = (0, img.size[1])
            img.close()

        file = io.BytesIO()
        output_image.save(file, format='PNG')
        file.seek(0)
        return file

    async def __round_one(self, session: SmiteleGame, skins: List[Skin]) -> bool:
        # Fetching a random god skin
        skin = random.choice(list(filter(lambda s: s.has_url, skins)))

        with await skin.get_card_bytes() as skin_image:
            with io.BytesIO() as file:
                # Cropping the skin image that we got randomly
                with Image.open(skin_image) as img:
                    size = 180
                    width, height = img.size
                    left = random.randint(0, width - size)
                    top = random.randint(0, height - size)
                    crop_image = img.crop((left, top, left + size, top + size))
                    crop_image.save(file, format='JPEG')
                    file.seek(0)

                desc = 'Name the god with this skin'
                return await self.__send_round_message(desc, session, \
                    _SmiteleRoundContext(1, file, self.SKIN_CROP_IMAGE_FILE))

    async def __round_two(self, session: SmiteleGame, build_task: 'asyncio.Task[io.BytesIO]') -> bool:
        with await build_task as file:
            desc = 'Hint: A top-ranked player of this god recently used this build in Ranked '\
                'Conquest.'
            return await self.__send_round_message(desc, session, \
                _SmiteleRoundContext(2, file, self.BUILD_IMAGE_FILE))

    async def __round_three(self, session: SmiteleGame, skins: List[Skin]) -> bool:
        context = session.context
        audio_src = None
        skin_copy = skins.copy()

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
                # Not all skins have voiceline pages on the Smite wiki, so retry until we get one
                # that works
                async with aiohttp.ClientSession() as client:
                    async with client.get(self.__get_base_smite_wiki(name=page_name.replace(' ', '_'))) as res:
                        smite_wiki = BeautifulSoup(await res.content.read(), 'html.parser')
                        # BeautifulSoup is amazing
                        audio_blocks = smite_wiki.find_all('audio')

                        # Exclude the first voice line, which is line played
                        # when the god is locked in (says their name)
                        audio_src = random.choice(audio_blocks).source.get('src')
            except (ValueError, IndexError):
                print(f'Unable to fetch voicelines for {session.god.name}\'s skin: {skin.name}')
                skin_copy = list(filter(lambda s: s.name != skin.name, skin_copy))
        async with aiohttp.ClientSession() as client:
            async with client.get(audio_src) as res:
                # If the current player is in a voice channel,
                # connect to it and play the voice line!
                if context.player.voice is not None:
                    with open(self.VOICE_LINE_FILE, 'wb') as voice_file:
                        voice_file.write(await res.content.read())
                    client = await context.player.voice.channel.connect()

                    async def disconnect():
                        await client.disconnect()
                        os.remove(self.VOICE_LINE_FILE)

                    client.play(discord.FFmpegPCMAudio(source=self.VOICE_LINE_FILE), \
                        after=lambda _: asyncio.run_coroutine_threadsafe(\
                            coro=disconnect(), loop=client.loop).result())
                else:
                    # Otherwise, just upload the voice file to Discord
                    with io.BytesIO(await res.content.read()) as file:
                        dis_file = discord.File(file, filename=self.VOICE_LINE_FILE)
                        await context.channel.send(file=dis_file)

                return await self.__send_round_message('Whose voice line was that?', \
                    session, _SmiteleRoundContext(3))

    async def __round_four(self, session: SmiteleGame) -> bool:
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
                        image.save(ability_bytes, format='JPEG')
                        ability_bytes.seek(0)
                        saved_image = True
                except Exception:
                    # requests isn't able to fetch for every ability image URL
                    print(f'Unable to create an image for {session.god.name}\'s {ability.name}')
            desc = 'Hint: Here\'s one of the god\'s abilities'
            return await self.__send_round_message(desc, session, \
                _SmiteleRoundContext(4, ability_bytes, self.ABILITY_IMAGE_FILE))

    async def __round_five(self, session: SmiteleGame) -> bool:
        return await self.__send_round_message(\
                f'The god has this title:\n```{session.god.title}```', \
                session, _SmiteleRoundContext(5))

    async def __round_six(self, session: SmiteleGame, base_skin: Skin):
        with io.BytesIO() as crop_file:
            with await base_skin.get_card_bytes() as card_bytes:
                with Image.open(card_bytes) as img:
                    size = 180
                    width, height = img.size
                    left = random.randint(0, width - size)
                    top = random.randint(0, height - size)
                    crop_image = img.crop((left, top, left + size, top + size))
                    crop_image.save(crop_file, format='JPEG')
                    crop_file.seek(0)

                desc = 'Final hint: This is a crop of the god\'s base skin'
                await self.__send_round_message(desc, session, \
                    _SmiteleRoundContext(6, crop_file, self.GOD_CROP_IMAGE_FILE))

    # Loops until exp time, editing the message embed with a countdown
    async def __countdown_loop(self, message: discord.Message, exp: float, embed: discord.Embed):
        while time.time() < exp:
            await asyncio.sleep(1)
            rem = math.ceil(exp-time.time())
            if rem >= 0:
                embed.set_field_at(\
                    0, name='Time Remaining:', value=f'_{rem} second{"s" if rem != 1 else ""}_')
                await message.edit(embed=embed)

    async def __send_incorrect(self, desc: str, last_round: bool, session: SmiteleGame) -> None:
        answer_image: discord.File = None
        if not last_round:
            desc += '\n\nNext round coming up shortly.'
        else:
            desc += f' The answer was **{session.god.name}**.'
            answer_image = discord.File(await session.god.get_card_bytes(), \
                filename=f'{session.god.name}.jpg')

        await session.context.channel.send(file=answer_image, \
            embed=discord.Embed(color=discord.Color.red(), description=desc))


    # Helper function for sending the repeated round messages to Discord
    async def __send_round_message(self, description: str, \
            session: SmiteleGame, round_ctx: _SmiteleRoundContext) -> bool:
        context = session.context

        embed = discord.Embed(color=discord.Color.blue(), description=description)
        cur_player = \
            f'(**{context.player.mention}**\'s Game) ' if len(self.__running_sessions) > 1 else ''
        embed.title = f'{cur_player}Round {round_ctx.round_number}:'
        embed.add_field(name='Time Remaining:', value='_20 seconds_')

        # If we have an image file, this is how it gets attached to the embed
        picture: discord.File = None
        if round_ctx.has_file():
            picture = discord.File(round_ctx.file_bytes, filename=round_ctx.file_name)
            embed.set_image(url=f'attachment://{round_ctx.file_name}')

        exp = time.time() + 20
        sent = await context.channel.send(file=picture, embed=embed)
        task = session.add_task(asyncio.get_running_loop().create_task(self.__countdown_loop(\
            sent, exp, embed)))
        try:
            while True:
                msg = await self.wait_for('message', timeout=20)
                if msg.channel != context.channel:
                    continue
                if self.user == msg.author:
                    continue
                if msg.content.startswith('$'):
                    continue
                if context.player != msg.author:
                    continue
                if self.__check_answer_message(msg, session.god.name):
                    answer_time = time.time() - (exp - 20)
                    task.cancel()
                    # These emojis are from my Discord server so I'll need to update these to be
                    # more universal. :D
                    ans_description = f'✅ Correct, **{context.player.mention}**! '\
                        f'You got it in {round(answer_time)} seconds. '\
                        f'The answer was **{session.god.name}**. '\
                        '<:frogchamp:566686914858713108>'

                    await context.channel.send(embed=discord.Embed(color=discord.Color.green(), \
                        description=ans_description))
                    return True
                task.cancel()
                inc_description = f'❌ Incorrect, **{context.player.mention}**.'
                await self.__send_incorrect(inc_description, round_ctx.is_last_round(), session)
                return False
        except asyncio.TimeoutError:
            inc_description = '❌⏲️ Time\'s up! <:killmyself:472184572407447573>'
            await self.__send_incorrect(inc_description, round_ctx.is_last_round(), session)
            return False

    async def __write_to_file_from_api(self, file: io.TextIOWrapper, \
            refresher: Callable[[], Awaitable[any]]) -> any:
        tmp = await refresher()
        json.dump(tmp, file)
        return tmp

    async def __load_cache(self, file_name: str, \
            refresher: Callable[[], Awaitable[any]] = None) -> List[any]:
        tmp = []
        if refresher is not None:
            with open(file_name, 'w') as file:
                tmp = await self.__write_to_file_from_api(file, refresher)
        else:
            try:
                with open(file_name, 'r+') as file:
                    tmp = json.load(file)
            except (FileNotFoundError, JSONDecodeError):
                with open(file_name, 'w') as file:
                    tmp = await self.__write_to_file_from_api(file, refresher)
        return tmp

    async def __should_refresh(self) -> Tuple[bool, float]:
        current_patch = float((await self.__smite_client.get_patch_info())['version_string'])
        try:
            with open(self.SMITE_PATCH_VERSION_FILE, 'r+') as file:
                cached_version = float(file.read())
                if current_patch > cached_version:
                    print(f'Current local cache ({cached_version}) '\
                          f'is out of date ({current_patch}), refreshing')
                    return (True, current_patch)
        except (FileNotFoundError, ValueError):
            print('Failed to open version cache, refreshing')
            return (True, current_patch)

        return (False, None)

if __name__ == '__main__':
    bot = SmiteleBot()
    bot.start_bot()
