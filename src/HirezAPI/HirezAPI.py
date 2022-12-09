# pylint: disable=invalid-name
"""HirezAPI implements a simple wrapper around Hirez's public API.

This module contains a class which wraps the API and simplifies making calls
by handling session tokens and other such requirements invisibly behind
Pythonic methods.
"""

import hashlib
from datetime import datetime
from enum import Enum
from json.decoder import JSONDecodeError
from typing import Any

import aiohttp

from god_types import GodId

HIREZ_DATE_FORMAT = '%m/%d/%Y %I:%M:%S %p'

class _Base:
    """_Base implements base Hirez API functionality.

    This class has the basic methods in Hirez's API, along with wrapper functionality
    for making API requests. It implements the non-game specific APIs.
    """
    SESSION_FILE = 'session'

    MAX_RETRIES = 3

    __auth_key: str
    __base_url: str
    __dev_id: str
    __should_keep_alive: bool
    __session_id: str = ''
    __save_session: bool

    def __init__(self, base_url: str, auth_key: str = None, \
            dev_id: str = None, keep_alive: bool = True, save_session: bool = True):
        self.__auth_key = auth_key
        self.__base_url = base_url
        self.__dev_id = dev_id
        self.__should_keep_alive = keep_alive
        self.__save_session = save_session
        self.__try_load_session()

    async def ping(self) -> Any:
        """Pings Hirez's API"""
        return await self._make_request('ping')

    async def create_session(self) -> bytes:
        route = 'createsession'
        time_string = self.__get_time_string_utcnow()
        return await self.__make_request_base(route, self.__dev_id, \
            self.__create_signature(route, time_string), time_string)

    async def test_session(self) -> bytes:
        route = 'testsession'
        time_string = self.__get_time_string_utcnow()
        return await self.__make_request_base(route, self.__dev_id, \
            self.__create_signature(route, time_string), self.__session_id, time_string)

    async def get_data_used(self) -> any:
        return await self._make_request('getdataused')

    async def get_hirez_server_status(self) -> any:
        return await self._make_request('gethirezserverstatus')

    async def get_patch_info(self) -> any:
        return await self._make_request('getpatchinfo')

    async def __make_request_base(self, route: str, *args: tuple) -> any:
        url = f'{self.__base_url}/{route}Json/{"/".join(str(arg) for arg in args)}'
        print(f'Sending request to {url}')
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as res:
                try:
                    return await res.json()
                except (JSONDecodeError, aiohttp.ContentTypeError):
                    print(f'Response content was not in JSON format: {await res.text()}')
                    raise

    async def _make_request(self, route: str, *args: tuple) -> any:
        if self.__session_id is None or  self.__session_id == '':
            await self.__keep_alive()
        req_count = 0
        res = None

        while req_count < self.MAX_RETRIES:
            time_string = self.__get_time_string_utcnow()
            res = await self.__make_request_base(route, self.__dev_id, \
                self.__create_signature(route, time_string), \
                self.__session_id, time_string, *args)
            req_count += 1
            if self.__is_expired(res):
                await self.__keep_alive()
                continue
            break
        if res is None:
            raise ConnectionError('Failed to connect')
        return res

    def __is_expired(self, res: any) -> bool:
        invalid = 'Invalid session id.'
        is_list = isinstance(res, list)
        return (is_list and any(val['ret_msg'] == invalid for val in res)) \
            or (not is_list and res['ret_msg'] == invalid)

    async def __keep_alive(self):
        if self.__should_keep_alive:
            new_session = await self.create_session()
            self.__session_id = new_session['session_id']
            if self.__save_session:
                with open(self.SESSION_FILE, 'w', encoding='utf-8') as file:
                    file.write(self.__session_id)

    def __try_load_session(self):
        try:
            with open(self.SESSION_FILE, 'r', encoding='utf-8') as file:
                self.__session_id = file.read()
        except FileNotFoundError:
            print('Session ID not loaded, will be loaded on demand')

    def __create_signature(self, route: str, time_string: str) -> str:
        sig_input = \
            f'{self.__dev_id}{route}{self.__auth_key}{time_string}'
        return hashlib.md5(sig_input.encode()).hexdigest()

    def __get_time_string_utcnow(self) -> str:
        return str(datetime.utcnow().strftime('%Y%m%d%H%M%S'))

class QueueId(Enum):
    # Main Game Modes
    ARENA = 435
    ASSAULT = 445
    CONQUEST = 426
    JOUST = 448
    MOTD = 434
    SLASH = 10189

    # Ranked (Keyboard)
    RANKED_CONQUEST = 451
    RANKED_DUEL = 440
    RANKED_JOUST = 450

    # Ranked (Controller)
    RANKED_CONQUEST_CONTROLLER = 504
    RANKED_DUEL_CONTROLLER = 502
    RANKED_JOUST_CONTROLLER = 503

    # Under 30 Queues
    UNDER_30_ARENA = 10195
    UNDER_30_CONQUEST = 10193
    UNDER_30_JOUST = 10197

    # Custom
    CUSTOM_ARENA = 438
    CUSTOM_ASSAULT = 446
    CUSTOM_CLASH = 467
    CUSTOM_CONQUEST_CLASSIC = 10206
    CUSTOM_CLASSIC_JOUST = 10177
    CUSTOM_CONQUEST = 429
    CUSTOM_CORRUPTED_ARENA = 10151
    CUSTOM_DOMINATION = 10174
    CUSTOM_DUEL = 10190
    CUSTOM_JOUST = 441
    CUSTOM_SEASON_7_JOUST = 10152
    CUSTOM_SIEGE = 460
    CUSTOM_SLASH = 10191

    # Tutorials
    ARENA_TUTORIAL = 462
    BASIC_TUTORIAL = 436
    CLASH_TUTORIAL = 471
    CONQUEST_TUTORIAL = 463

    # Practice
    ARENA_PRACTICE_EASY = 443
    ARENA_PRACTICE_MEDIUM = 472
    ARENA_PRACTICE_HARD = 10167

    ASSAULT_PRACTICE_EASY = 479
    ASSAULT_PRACTICE_MEDIUM = 480
    ASSAULT_PRACTICE_HARD = 10168

    CONQUEST_PRACTICE_EASY = 458
    CONQUEST_PRACTICE_MEDIUM = 475
    CONQUEST_PRACTICE_HARD = 10170

    JOUST_PRACTICE_EASY = 464
    JOUST_PRACTICE_MEDIUM = 473
    JOUST_PRACTICE_HARD = 10171

    JUNGLE_PRACTICE = 444

    SLASH_PRACTICE_EASY = 10201
    SLASH_PRACTICE_MEDIUM = 10202
    SLASH_PRACTICE_HARD = 10203

    # vs. AI
    ARENA_VS_AI_VERY_EASY = 457
    ARENA_VS_AI_MEDIUM = 468
    ARENA_VS_AI_VERY_HARD = 10158

    ASSAULT_VS_AI_EASY = 481
    ASSAULT_VS_AI_MEDIUM = 454
    ASSAULT_VS_AI_HARD = 10159

    CONQUEST_VS_AI_EASY = 476
    CONQUEST_VS_AI_MEDIUM = 461
    CONQUEST_VS_AI_HARD = 10161

    JOUST_VS_AI_VERY_EASY = 474
    JOUST_VS_AI_MEDIUM = 456
    JOUST_VS_AI_VERY_HARD = 10162

    SLASH_VS_AI_EASY = 10198
    SLASH_VS_AI_MEDIUM = 10199
    SLASH_VS_AI_HARD = 10200

    # Deprecated Queues
    ARENA_TRAINING = 483
    CLASH = 466
    CLASSIC_JOUST = 499
    CONQUEST_5V5 = 423
    CONQUEST_NOVICE = 424
    DOMINATION = 433
    JOUST_OBSOLETE = 431
    RANKED_ARENA = 452
    RANKED_CONQUEST_SOLO = 430
    SIEGE = 459

    # Adventures
    CELESTIAL_DOMINATION = 500
    LEGEND_OF_THE_FOXES = 495
    ADVENTURE_CLASSIC_JOUST = 10153
    SHADOWS_OVER_HERCOPOLIS = 501
    CORRUPTED_ARENA = 508
    CLASSIC_DOMINATION = 10173
    FAFNIRS_WONDERLAND = 484
    FAFNIRS_WONDERLAND_HARD = 485
    HEIMDALLRS_CROSSING = 10155

    # Unknown
    NORMAL_SPECIAL_EVENT = 465
    NORMAL_ADVENTURE_1 = 486
    NORMAL_ADVENTURE_2 = 488
    NORMAL_ADVENTURE_DUNGEON = 489
    NORMAL_ADVENTURE_DUNGEON_HARD = 490
    NORMAL_ADVENTURE_4 = 491
    NORMAL_ADVENTURE_5 = 492
    NORMAL_ADVENTURE_5_HARD = 493
    NORMAL_ADVENTURE_6 = 497
    NORMAL_ADVENTURE_7 = 498

    @staticmethod
    def is_normal(value) -> bool:
        return value in (
            QueueId.ARENA,
            QueueId.ASSAULT,
            QueueId.CONQUEST,
            QueueId.JOUST,
            QueueId.MOTD,
            QueueId.SLASH,
            QueueId.UNDER_30_ARENA,
            QueueId.UNDER_30_CONQUEST,
            QueueId.UNDER_30_JOUST,
        )

    @staticmethod
    def is_ranked(value) -> bool:
        return value in (
            QueueId.RANKED_CONQUEST,
            QueueId.RANKED_CONQUEST_CONTROLLER,
            QueueId.RANKED_DUEL,
            QueueId.RANKED_DUEL_CONTROLLER,
            QueueId.RANKED_JOUST,
            QueueId.RANKED_JOUST_CONTROLLER,
        )

    @staticmethod
    def is_duel(value) -> bool:
        return value in (
            QueueId.RANKED_DUEL,
            QueueId.RANKED_DUEL_CONTROLLER,
        )

    @staticmethod
    def is_custom(value) -> bool:
        return value in (
            QueueId.CUSTOM_ARENA,
            QueueId.CUSTOM_ASSAULT,
            QueueId.CUSTOM_CLASH,
            QueueId.CUSTOM_CONQUEST_CLASSIC,
            QueueId.CUSTOM_CLASSIC_JOUST,
            QueueId.CUSTOM_CONQUEST,
            QueueId.CUSTOM_DUEL,
            QueueId.CUSTOM_JOUST,
            QueueId.CUSTOM_SEASON_7_JOUST,
            QueueId.CUSTOM_SIEGE,
            QueueId.CUSTOM_SLASH,
        )

    @staticmethod
    def is_tutorial(value) -> bool:
        return value in (
            QueueId.ARENA_TUTORIAL,
            QueueId.BASIC_TUTORIAL,
            QueueId.CLASH_TUTORIAL,
            QueueId.CONQUEST_TUTORIAL,
        )

    @staticmethod
    def is_practice(value) -> bool:
        return value in (
            QueueId.ARENA_PRACTICE_EASY,
            QueueId.ARENA_PRACTICE_MEDIUM,
            QueueId.ARENA_PRACTICE_HARD,
            QueueId.ASSAULT_PRACTICE_EASY,
            QueueId.ASSAULT_PRACTICE_MEDIUM,
            QueueId.ASSAULT_PRACTICE_HARD,
            QueueId.CONQUEST_PRACTICE_EASY,
            QueueId.CONQUEST_PRACTICE_MEDIUM,
            QueueId.CONQUEST_PRACTICE_HARD,
            QueueId.JOUST_PRACTICE_EASY,
            QueueId.JOUST_PRACTICE_MEDIUM,
            QueueId.JOUST_PRACTICE_HARD,
            QueueId.JUNGLE_PRACTICE,
            QueueId.SLASH_PRACTICE_EASY,
            QueueId.SLASH_PRACTICE_MEDIUM,
            QueueId.SLASH_PRACTICE_HARD,
        )

    @staticmethod
    def is_vs_ai(value) -> bool:
        return value in (
            QueueId.ARENA_VS_AI_VERY_EASY,
            QueueId.ARENA_VS_AI_MEDIUM,
            QueueId.ARENA_VS_AI_VERY_HARD,
            QueueId.ASSAULT_VS_AI_EASY,
            QueueId.ASSAULT_VS_AI_MEDIUM,
            QueueId.ASSAULT_VS_AI_HARD,
            QueueId.CONQUEST_VS_AI_EASY,
            QueueId.CONQUEST_VS_AI_MEDIUM,
            QueueId.CONQUEST_VS_AI_HARD,
            QueueId.JOUST_VS_AI_VERY_EASY,
            QueueId.JOUST_VS_AI_MEDIUM,
            QueueId.JOUST_VS_AI_VERY_HARD,
            QueueId.SLASH_VS_AI_EASY,
            QueueId.SLASH_VS_AI_MEDIUM,
            QueueId.SLASH_VS_AI_HARD,
        )

    @staticmethod
    def is_deprecated(value) -> bool:
        return value in (
            QueueId.ARENA_TRAINING,
            QueueId.CLASH,
            QueueId.CLASSIC_JOUST,
            QueueId.CONQUEST_5V5,
            QueueId.DOMINATION,
            QueueId.RANKED_ARENA,
            QueueId.RANKED_CONQUEST_SOLO,
            QueueId.SIEGE,
            QueueId.CONQUEST_NOVICE,
            QueueId.JOUST_OBSOLETE,
        )

    @staticmethod
    def is_adventure(value) -> bool:
        return value in (
            QueueId.CELESTIAL_DOMINATION,
            QueueId.LEGEND_OF_THE_FOXES,
            QueueId.ADVENTURE_CLASSIC_JOUST,
            QueueId.SHADOWS_OVER_HERCOPOLIS,
            QueueId.CORRUPTED_ARENA,
            QueueId.CLASSIC_DOMINATION,
            QueueId.FAFNIRS_WONDERLAND,
            QueueId.FAFNIRS_WONDERLAND_HARD,
            QueueId.HEIMDALLRS_CROSSING,
        )

    @property
    def display_name(self) -> str:
        if self == QueueId.MOTD:
            return self.name
        queue = self.name.lower().replace('_', ' ').title()
        if QueueId.is_ranked(self):
            queue = queue.replace('Controller', '(Controller)')
        if QueueId.is_vs_ai(self):
            queue = queue.replace('Vs Ai', 'vs. AI')
            queue_split = queue.split()
            ai_index = queue_split.index('AI')
            queue_split[ai_index + 1] = f'({queue_split[ai_index + 1]}'
            queue = f'{" ".join(queue_split)})'
        if QueueId.is_practice(self):
            if self == QueueId.JUNGLE_PRACTICE:
                return queue
            queue_split = queue.split()
            pidx = queue_split.index('Practice')
            queue_split[pidx + 1] = f'({queue_split[pidx + 1]}'
            queue = f'{" ".join(queue_split)})'
        if QueueId.is_deprecated(self):
            queue = queue.replace('5V5', '5v5')
        if QueueId.is_adventure(self):
            queue = f'Adventure: {queue.replace("Adventure ", "")}'\
                .replace('Of The', 'of the')\
                .replace('Fafnirs', "Fafnir's")\
                .replace('Hard', '(Hard)')\
                .replace('Heimdallrs', "Heimdallr's")
        return queue

class LanguageCode(Enum):
    ENGLISH = 1

class TierId(Enum):
    BRONZE_V = 1
    BRONZE_IV = 2
    BRONZE_III = 3
    BRONZE_II = 4
    BRONZE_I = 5
    SILVER_V = 6
    SILVER_IV = 7
    SILVER_III = 8
    SILVER_II = 9
    SILVER_I = 10
    GOLD_V = 11
    GOLD_IV = 12
    GOLD_III = 13
    GOLD_II = 14
    GOLD_I = 15
    PLATINUM_V = 16
    PLATINUM_IV = 17
    PLATINUM_III = 18
    PLATINUM_II = 19
    PLATINUM_I = 20
    DIAMOND_V = 21
    DIAMOND_IV = 22
    DIAMOND_III = 23
    DIAMOND_II = 24
    DIAMOND_I = 25
    MASTERS = 26
    GRANDMASTER = 27

    @property
    def display_name(self) -> str:
        split_name = self.name.replace('_', ' ')\
            .title().split()
        if len(split_name) > 1:
            split_name[1] = split_name[1].upper()
        return ' '.join(split_name)

class PlayerRole(Enum):
    CARRY = 'carry'
    JUNGLE = 'jungle'
    MID = 'mid'
    SOLO = 'solo'
    SUPPORT = 'support'

class PortalId(Enum):
    HI_REZ = 1
    STEAM = 5
    PS4 = 9
    XBOX = 10
    SWITCH = 22
    DISCORD = 25
    EPIC_GAMES = 28

class Smite(_Base):
    BASE_URL: str = 'https://api.smitegame.com/smiteapi.svc'

    def __init__(self, auth_key: str = None, dev_id: str = None, save_session: bool = True):
        super().__init__(self.BASE_URL, auth_key, dev_id, save_session=save_session)

    # Gods & Items

    async def get_gods(self, language_code: LanguageCode = LanguageCode.ENGLISH):
        return await self._make_request('getgods', language_code.value)

    async def get_god_leaderboard(self, god_id: GodId, queue_id: QueueId):
        return await self._make_request('getgodleaderboard', god_id.value, queue_id.value)

    async def get_god_alt_abilities(self):
        return await self._make_request('getgodaltabilities')

    async def get_god_skins(self, god_id: GodId, language_code: LanguageCode = LanguageCode.ENGLISH):
        return await self._make_request('getgodskins', god_id.value, language_code.value)

    async def get_god_recommended_items(self, god_id: GodId, language_code: LanguageCode = LanguageCode.ENGLISH):
        return await self._make_request('getgodrecommendeditems', god_id.value, language_code.value)

    async def get_items(self, language_code: LanguageCode = LanguageCode.ENGLISH):
        return await self._make_request('getitems', language_code.value)

    # Players

    async def get_player(self, player: int, portal_id: PortalId | None = None):
        if portal_id is None:
            return await self._make_request('getplayer', player)
        return await self._make_request('getplayer', player, portal_id.value)

    async def get_player_id_by_name(self, player_name: str):
        return await self._make_request('getplayeridbyname', player_name)

    async def get_player_id_by_portal_user_id(self, portal_id: PortalId, portal_user_id: int):
        return await self._make_request('getplayeridbyportaluserid', portal_id.value, portal_user_id)

    async def get_player_ids_by_gamer_tag(self, portal_id: PortalId, gamer_tag: str):
        return await self._make_request('getplayeridsbygamertag', portal_id.value, gamer_tag)

    async def get_friends(self, player_id: int):
        return await self._make_request('getfriends', player_id)

    async def get_god_ranks(self, player_id: int):
        return await self._make_request('getgodranks', player_id)

    async def get_player_achievements(self, player_id: int):
        return await self._make_request('getplayerachievements', player_id)

    async def get_player_status(self, player_id: int):
        return await self._make_request('getplayerstatus', player_id)

    async def get_match_history(self, player_id: int):
        return await self._make_request('getmatchhistory', player_id)

    async def get_queue_stats(self, player_id: int, queue_id: QueueId):
        return await self._make_request('getqueuestats', player_id, queue_id.value)

    async def search_players(self, search_query: str):
        return await self._make_request('searchplayers', search_query)

    # Matches

    async def get_demo_details(self, match_id: int):
        return await self._make_request('getdemodetails', match_id)

    async def get_match_details(self, match_id: int):
        return await self._make_request('getmatchdetails', match_id)

    async def get_match_details_batch(self, *match_ids: tuple):
        return await self._make_request('getmatchdetailsbatch', *match_ids)

    async def get_match_ids_by_queue(self, queue_id: QueueId, \
            date: int, hour: int, minute_window: int = 0):
        return await self._make_request('getmatchidsbyqueue', \
            queue_id.value, date, f'{hour},{minute_window}')

    async def get_match_player_details(self, match_id: int):
        return await self._make_request('getmatchplayerdetails', match_id)

    async def get_top_matches(self):
        return await self._make_request('gettopmatches')

    # Other

    async def get_league_leaderboard(self, queue_id: QueueId, tier_id: TierId, _round: int):
        return await self._make_request('getleagueleaderboard', \
            queue_id.value, tier_id.value, _round)

    async def get_league_seasons(self, queue_id: QueueId):
        return await self._make_request('getleagueseasons', queue_id.value)

    async def get_team_details(self, clan_id: int):
        return await self._make_request('getteamdetails', clan_id)

    async def get_team_match_history(self, clan_id: int):
        return await self._make_request('getteammatchhistory', clan_id)

    async def get_team_players(self, clan_id: int):
        return await self._make_request('getteamplayers', clan_id)

    async def search_teams(self, search_query: str):
        return await self._make_request('searchteams', search_query)

    async def get_esports_pro_league_details(self):
        return await self._make_request('getesportsproleaguedetails')

    async def get_motd(self):
        return await self._make_request('getmotd')
