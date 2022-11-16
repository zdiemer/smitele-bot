"""Smite-le Bot - A Discord bot for playing Smite-le, the Smite and Wordle inspired game

This module implements a Discord bot which allows users to play a six round game in Discord
channels.
"""

import asyncio
import hashlib
import json
import math
import random
import time
from datetime import datetime

import discord
import editdistance
import requests
from bs4 import BeautifulSoup
from discord.ext import commands
from PIL import Image
from unidecode import unidecode

class SmiteleBot(commands.Bot):
    """SmiteleBot implements wrapped Discord and Hirez functionality"""
    # Used in Round 4, this is the file name for the ability icon that will be saved and shown
    ABILITY_IMAGE_FILE = 'ability.jpg'

    # Hirez's API has several base URLs based on games
    BASE_HIREZ_URL = 'https://api.smitegame.com/smiteapi.svc'

    # Used in Round 2, this is the build that gets scraped from recent matches
    BUILD_IMAGE_FILE = 'build.png'

    # Default config file name, this stores your Discord token, Hirez Dev ID, and Hirez Auth Key
    CONFIG_FILE = 'config.json'

    # Used throughout, this pulls the default card art for the god
    GOD_IMAGE_FILE = 'god.jpg'

    # This is a cropped image of the god card art used in Round 6
    GOD_CROP_IMAGE_FILE = 'godCrop.jpg'

    # This is a cached version of the getgods route through the Hirez API
    GODS_FILE = 'gods.json'

    # This is a cached version of the getitems route through the Hirez API
    ITEMS_FILE = 'items.json'

    # Corresponds to English
    LANGUAGE_CODE_EN = '1'

    # Default file name for saving the session token. This session token is required by Hirez.
    SESSION_FILE = 'session'

    # Used in Round 1, this is the full image of the skin that then gets cropped
    SKIN_IMAGE_FILE = 'skin.jpg'

    # The cropped image of the skin mentioned above
    SKIN_CROP_IMAGE_FILE = 'crop.jpg'

    # This is a random voice line pulled from the Smite wiki
    VOICE_LINE_FILE = 'voice.ogg'

    # Cached answer for the round
    answer_god_name = ''

    # The current player, which will be a Discord player
    current_player = None

    # Cached config values
    __config = None

    # Cached getgods in memory
    __gods = []

    # Cached getitms in memory
    __items = []

    # Cached session in memory
    __session_id = None

    # A helper lambda for hitting a random Smite wiki voicelines route
    __get_base_smite_wiki = lambda self, name: f'https://smite.fandom.com/wiki/{name}_voicelines'

    # A helper lambda for constructing the item image file names used in constructing the build
    # image
    __get_item_image_file = lambda self, item_id: f'item{item_id}.jpg'

    # A helper lambda for getting a time string in the format the Hirez API expects
    __get_time_string_utcnow = lambda self: str(datetime.utcnow().strftime('%Y%m%d%H%M%S'))

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
                        raise Exception(f'{self.CONFIG_FILE} was missing value for "discordToken."')
                    if not 'hirezDevId' in self.__config:
                        raise Exception(f'{self.CONFIG_FILE} was missing value for "hirezDevId."')
                    if not 'hirezAuthKey' in self.__config:
                        raise Exception(f'{self.CONFIG_FILE} was missing value for "hirezAuthKey."')
            except Exception:
                raise Exception(f'Failed to load {self.CONFIG_FILE}. Does this file exist?')

        @self.event
        # pylint: disable=unused-variable
        async def on_ready():
            activity = discord.Game(name='Smite', type=3)
            await self.change_presence(status=discord.Status.online, activity=activity)

            try:
                with open(self.SESSION_FILE, 'r') as file:
                    print('Session ID loaded')
                    session_id = file.read()
                    if session_id is not None and session_id != '':
                        self.__session_id = session_id
                    else:
                        raise MissingFileError
            except MissingFileError:
                print('Session ID did not exist, will be loaded on demand')

            self.__try_init_gods()
            self.__try_init_items()

            print('Smite-le Bot is ready!')


        @self.command()
        @commands.max_concurrency(number=1)
        async def smitele(message, *args):
            await self.__smitele(message, *args)

        @self.command()
        # pylint: disable=unused-variable,invalid-name
        async def st(message, *args):
            await smitele(message, *args)

    def start_bot(self):
        """
        Using this command instead of just calling self.run() since the discordToken is loaded
        as part of this class.
        """
        self.run(self.__config['discordToken'])

    # Primary command for starting a round of Smite-le!
    async def __smitele(self, message, *args):
        if message.author == self.user:
            return

        self.current_player = message.author

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
        god = self.__gods[random.randint(0, len(self.__gods) - 1)]
        self.answer_god_name = god['Name']

        res = requests.get(god['godCard_URL'])
        open(self.GOD_IMAGE_FILE, 'wb').write(res.content)

        # Helper function for checking correctness
        def check(msg):
            # Slightly hacky way for the player to stop the running game
            if msg.content.startswith('$stop'):
                loop = asyncio.get_running_loop()
                loop.create_task(message.channel.send(embed=discord.Embed(\
                    color=discord.Color.red(), \
                    title='Smite-le round canceled!')))
                raise StoppedError

            guess = unidecode(msg.content).lower().replace('-', ' ')
            return guess == self.answer_god_name.lower() or \
                editdistance.eval(guess, self.answer_god_name.lower()) <= 2

        # Round 1

        # Fetching skins for this god, and then fetching a random god skin
        skins = self.__make_hirez_request('getgodskins', god['id'])
        skin = skins[random.randint(0, len(skins) - 1)]
        skin_url = skin['godSkin_URL']

        # Some of Hirez's responses don't contain a URL for the given skin, retry until we get one
        while not skin_url.startswith('http'):
            print(f'Skin had a malformed or missing URL: {skin}')
            skin_url = skins[random.randint(0, len(skins) - 1)]['godSkin_URL']

        res = requests.get(skin_url)
        open(self.SKIN_IMAGE_FILE, 'wb').write(res.content)

        # Cropping the skin image that we got randomly
        with Image.open(self.SKIN_IMAGE_FILE) as img:
            size = 180
            width, height = img.size
            left = random.randint(0, width - size)
            top = random.randint(0, height - size)
            crop_image = img.crop((left, top, left + size, top + size))
            crop_image.save(self.SKIN_CROP_IMAGE_FILE)

        with open(self.SKIN_CROP_IMAGE_FILE, 'rb') as file:
            desc = 'Name the god with this skin'
            if await self.__send_round_message(round_number=1, \
                description=desc, check=check, message=message, \
                file=file, file_name=self.SKIN_CROP_IMAGE_FILE):
                return

        # Round 2
        build = []

        # Hirez's route for getting recommended items is highly out of date, so we'll get a
        # top Ranked Conquest player's build
        god_leaderboard = self.__make_hirez_request('getgodleaderboard', f'{god["id"]}/451', False)

        while len(build) == 0:
            # Fetching a random player from the leaderboard
            random_player = god_leaderboard[random.randint(0, len(god_leaderboard) - 1)]
            print(f'Finding a build from player {random_player["player_id"]}...')

            # Scraping their recent match history to try and find a current build
            match_history = self.__make_hirez_request('getmatchhistory', \
                random_player['player_id'], include_language_code=False)
            for match in match_history:
                if match['GodId'] == god['id']:
                    for i in range(1, 7):
                        # Luckily `getmatchhistory` includes build info!
                        if match[f'ItemId{i}'] != 0:
                            build.append(match[f'ItemId{i}'])
                    break

        # This is a horribly inefficient loop, I'll fix it later
        item_urls = {}
        for idx, item_id in enumerate(build):
            for item in self.__items:
                if item['ItemId'] == item_id:
                    item_urls[idx] = item['itemIcon_URL']

        # Appending the images into a single build image
        images = []
        thumb_size = 128
        total_width = 0
        total_height = 128
        for key in sorted(item_urls):
            # First requesting and saving the image from the URLs we got
            file_name = self.__get_item_image_file(item_id=key)
            res = requests.get(item_urls[key])
            open(file_name, 'wb').write(res.content)
            try:
                # Sometimes we don't get a full build, so this sets the final size of the build
                # image
                if key <= 2:
                    total_width += thumb_size
                if key == 3:
                    total_height += thumb_size
                image = Image.open(file_name)
                # Resize the image if necessary, Hirez doesn't return a consistent size
                if image.size != (thumb_size, thumb_size):
                    image.thumbnail((thumb_size, thumb_size))
                image.save(file_name)
                images.append(image)
            except Exception:
                print(f'Unable to create an image with the URL {item_urls[key]}')

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
        output_image.save(self.BUILD_IMAGE_FILE)

        with open(self.BUILD_IMAGE_FILE, 'rb') as file:
            desc = 'Hint: A top-ranked player of this god recently used this build in Ranked '\
                'Conquest.'
            if await self.__send_round_message(round_number=2, description=desc, check=check, \
                message=message, file=file, file_name=self.BUILD_IMAGE_FILE):
                return

        # Round 3
        audio_src = None

        while audio_src is None:
            # Getting a random skin to fetch a random voiceline for
            random_skin = skins[random.randint(0, len(skins) - 1)]
            skin_name = random_skin['skin_name']
            page_name = ''

            # All of these correspond to just the god's name
            if skin_name in ['Golden', 'Legendary', 'Diamond', f'Standard {god["Name"]}']:
                page_name = god['Name']
            else:
                page_name = f'{skin_name}_{god["Name"]}'

            try:
                # Not all skins have voiceline pages on the Smite wiki, so retry until we get one
                # that works
                res = requests.get(self.__get_base_smite_wiki(name=page_name.replace(' ', '_')))
                smite_wiki = BeautifulSoup(res.content, 'html.parser')
                # BeautifulSoup is amazing
                audio_blocks = smite_wiki.find_all('audio')

                # Exclude the first voice line, which is line played when the god is locked in
                # (says their name)
                audio_src = audio_blocks[random.randint(1, len(audio_blocks) - 1)].source.get('src')
            except ValueError:
                print(f'Unable to fetch voicelines for {god["Name"]}\'s skin: {skin_name}')
        res = requests.get(audio_src)
        open(self.VOICE_LINE_FILE, 'wb').write(res.content)

        # If the current player is in a voice channel, connect to it and play the voice line!
        if self.current_player.voice is not None:
            client = await self.current_player.voice.channel.connect()
            client.play(discord.FFmpegPCMAudio(source=self.VOICE_LINE_FILE), \
                after=lambda _: asyncio.run_coroutine_threadsafe(\
                    coro=client.disconnect(), loop=client.loop).result())
        else:
            # Otherwise, just upload the voice file to Discord
            with open(self.VOICE_LINE_FILE, 'rb') as file:
                dis_file = discord.File(file)
                await message.channel.send(file=dis_file)

        if await self.__send_round_message(round_number=3, \
            description='Whose voice line was that?', check=check, message=message):
            return

        # Round 4
        saved_image = False
        while not saved_image:
            try:
                # Some gods actually have more than this (e.g. King Arthur, Merlin).
                # I may add support for their additional abilities later
                ability_url = god[f'godAbility{random.randint(1, 5)}_URL']
                res = requests.get(ability_url)
                open(self.ABILITY_IMAGE_FILE, 'wb').write(res.content)
                image = Image.open(self.ABILITY_IMAGE_FILE)
                # Again, not all images that Hirez sends are a consistent size
                if image.size != (64, 64):
                    image.thumbnail((64, 64))
                image.save(self.ABILITY_IMAGE_FILE)
                images.append(image)
                saved_image = True
            except Exception:
                # requests isn't able to fetch for every ability image URL
                print(f'Unable to create an image with the URL {ability_url}')
        with open(self.ABILITY_IMAGE_FILE, 'rb') as file:
            if await self.__send_round_message(round_number=4, \
                description='Hint: Here\'s one of the god\'s abilities', check=check, \
                message=message, file=file, file_name=self.ABILITY_IMAGE_FILE):
                return

        # Round 5
        if await self.__send_round_message(round_number=5, \
            description=f'The god has this title:\n```{god["Title"]}```', \
            check=check, message=message):
            return

        # Round 6
        with Image.open(self.GOD_IMAGE_FILE) as img:
            size = 180
            width, height = img.size
            left = random.randint(0, width - size)
            top = random.randint(0, height - size)
            crop_image = img.crop((left, top, left + size, top + size))
            crop_image.save(self.GOD_CROP_IMAGE_FILE)

        with open(self.GOD_IMAGE_FILE, 'rb') as ans_f:
            with open(self.GOD_CROP_IMAGE_FILE, 'rb') as file:
                desc = 'Final hint: This is a crop of the god\'s base skin'
                if await self.__send_round_message(round_number=6, description=desc, check=check, \
                    message=message, file=file, file_name=self.GOD_CROP_IMAGE_FILE, \
                    last_round=True, answer_file=ans_f, answer_file_name=self.GOD_IMAGE_FILE):
                    return

    # Loops until exp time, editing the message embed with a countdown
    async def __countdown_loop(self, message, exp, embed):
        while time.time() < exp:
            await asyncio.sleep(1)
            rem = math.ceil(exp-time.time())
            embed.set_field_at(\
                0, name='Time Remaining:', value=f'_{rem} second{"s" if rem != 1 else ""}_')
            await message.edit(embed=embed)

    # Helper function for hitting the Hirez API, creates the required signature field
    def __create_signature(self, route, time_string):
        sig_input = \
            f'{self.__config["hirezDevId"]}{route}{self.__config["hirezAuthKey"]}{time_string}'
        return hashlib.md5(sig_input.encode()).hexdigest()

    # Helper function for sending the repeated round messages to Discord
    async def __send_round_message(self, round_number, description, message, check, file=None, \
            file_name=None, last_round=False, answer_file=None, answer_file_name=None):
        embed = discord.Embed(color=discord.Color.blue(), description=description)
        embed.title = f'Round {round_number}:'
        embed.add_field(name='Time Remaining:', value='_20 seconds_')

        # If we have an image file, this is how it gets attached to the embed
        picture = None
        if file is not None and file_name is not None:
            picture = discord.File(file)
            embed.set_image(url=f'attachment://{file_name}')

        async def send_incorrect(incorrect_desc):
            answer_image = None
            if not last_round:
                incorrect_desc += '\n\nNext round coming up shortly.'
            elif answer_file is not None and answer_file_name is not None:
                incorrect_desc += f' The answer was **{self.answer_god_name}**.'
                answer_image = discord.File(answer_file)

            await message.channel.send(file=answer_image, \
                embed=discord.Embed(color=discord.Color.red(), description=incorrect_desc))

        exp = time.time() + 20
        task = asyncio.get_running_loop().create_task(self.__countdown_loop(\
            await message.channel.send(file=picture, embed=embed), exp, embed))
        try:
            while True:
                msg = await self.wait_for('message', timeout=20)
                if self.current_player != msg.author:
                    desc = f'🛑 The current player is **{self.current_player.display_name}**.'
                    await message.channel.send(\
                        embed=discord.Embed(color=discord.Color.red(), description=desc))
                    continue
                if check(msg):
                    answer_time = time.time() - (exp - 20)
                    task.cancel()
                    # These emojis are from my Discord server so I'll need to update these to be
                    # more universal. :D
                    ans_description = f'✅ Correct, **{msg.author.display_name}**! '\
                        f'You got it in {round(answer_time)} seconds. '\
                        f'The answer was **{self.answer_god_name}**. <:frogchamp:566686914858713108>'

                    await message.channel.send(embed=discord.Embed(color=discord.Color.green(), \
                        description=ans_description))
                    return True
                else:
                    task.cancel()
                    inc_description = f'❌ Incorrect, **{msg.author.display_name}**.'
                    await send_incorrect(inc_description)
                    return False
        except asyncio.TimeoutError:
            inc_description = f'❌⏲️ Time\'s up! <:killmyself:472184572407447573>'
            await send_incorrect(inc_description)
            return False
        except StoppedError:
            task.cancel()
            return True

    # Helper function for starting a new session through the Hirez API
    def __start_session(self):
        print('Refreshing session token')
        time_string = self.__get_time_string_utcnow()

        res = requests.get(f'{self.BASE_HIREZ_URL}/createsessionJson/'\
            f'{self.__config["hirezDevId"]}/'\
            f'{self.__create_signature("createsession", time_string)}/{time_string}')
        self.__session_id = res.json()['session_id']

        with open(self.SESSION_FILE, 'w') as file:
            file.write(self.__session_id)

    # Helper function for downloading the gods to JSON
    def __try_init_gods(self):
        with open(self.GODS_FILE, 'r+') as file:
            try:
                self.__gods = json.load(file)
            except Exception:
                self.__gods = self.__make_hirez_request('getgods')
                json.dump(self.__gods, file)

    # Helper function for downloading the items to JSON
    def __try_init_items(self):
        with open(self.ITEMS_FILE, 'r+') as file:
            try:
                self.__items = json.load(file)
            except Exception:
                self.__items = self.__make_hirez_request('getitems')
                json.dump(self.__items, file)

    # Ensure our session token is still valid
    def __validate_session(self):
        if self.__session_id is None:
            return False
        time_string = self.__get_time_string_utcnow()
        res = requests.get(f'{self.BASE_HIREZ_URL}/testsessionJson/'\
            f'{self.__config["hirezDevId"]}/'\
            f'{self.__create_signature("testsession", time_string)}'\
            f'/{self.__session_id}/{time_string}')
        return res.status_code == 200 and 'This was a successful test' in res.json()

    # Quick wrapper for hitting the Hirez API
    def __make_hirez_request(self, route, argument = None, include_language_code = True):
        if not self.__validate_session():
            self.__start_session()
        time_string = self.__get_time_string_utcnow()
        url = f'{self.BASE_HIREZ_URL}/{route}Json/{self.__config["hirezDevId"]}'\
            f'/{self.__create_signature(route, time_string)}'\
            f'/{self.__session_id}/{time_string}'

        if argument is not None:
            url += f'/{argument}'
        if include_language_code:
            url += f'/{self.LANGUAGE_CODE_EN}'

        return requests.get(url).json()

class MissingFileError(Exception):
    pass

class StoppedError(Exception):
    pass

bot = SmiteleBot()
bot.start_bot()