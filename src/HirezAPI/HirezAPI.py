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

import aiohttp

from god import GodId

class _Base:
    """_Base implements base Hirez API functionality.

    This class has     
    """
    SESSION_FILE = 'session'

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

    async def ping(self):
        return await self.__make_request_base('ping')

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
        invalid = 'Invalid session id.'
        session_expired = False
        time_string = self.__get_time_string_utcnow()
        req = lambda: self.__make_request_base(route, self.__dev_id, \
            self.__create_signature(route, time_string), \
            self.__session_id, time_string, *args)
        resp = await req()
        try:
            if isinstance(resp, list):
                if any(val['ret_msg'] == invalid for val in resp):
                    session_expired = True
                    await self.__keep_alive()
            elif resp['ret_msg'] == invalid:
                session_expired = True
                await self.__keep_alive()
        except KeyError:
            print(f'Response message did not contain ret_msg: {resp}')
        return await req() if session_expired else resp

    async def __keep_alive(self):
        if self.__should_keep_alive:
            new_session = await self.create_session()
            self.__session_id = new_session['session_id']
            if self.__save_session:
                with open(self.SESSION_FILE, 'w') as file:
                    file.write(self.__session_id)

    def __try_load_session(self):
        try:
            with open(self.SESSION_FILE, 'r') as file:
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
    ARENA = 435
    ASSAULT = 445
    CLASH = 466
    CONQUEST = 426
    JOUST = 448
    MOTD = 434
    RANKED_CONQUEST = 451
    RANKED_DUEL = 440
    RANKED_JOUST = 450
    SIEGE = 459
    SLASH = 10189

class LanguageCode(Enum):
    ENGLISH = 1

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

    async def get_player(self, player: int, portal_id: int = None):
        if portal_id is None:
            return await self._make_request('getplayer', player)
        return await self._make_request('getplayer', player, portal_id)

    async def get_player_batch(self, *players: tuple):
        return await self._make_request('getplayerbatch', *players)

    async def get_player_id_by_name(self, player_name: str):
        return await self._make_request('getplayeridbyname', player_name)

    async def get_player_id_by_portal_user_id(self, portal_id: int):
        return await self._make_request('getplayeridbyportaluserid', portal_id)

    async def get_player_ids_by_gamer_tag(self, portal_id: int, gamer_tag: str):
        return await self._make_request('getplayeridsbygamertag', portal_id, gamer_tag)

    async def get_player_id_info_for_xbox_and_switch(self, player_name: str):
        return await self._make_request('getplayeridinfoforxboxandswitch', player_name)

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

    async def get_match_ids_by_queue(self, queue_id: QueueId, date: int, hour: int, minute_window: int = 0):
        # TODO: Validation and setup hour window
        return await self._make_request('getmatchidsbyqueue', queue_id.value, date, f'{hour},{minute_window}')

    async def get_match_player_details(self, match_id: int):
        return await self._make_request('getmatchplayerdetails', match_id)

    async def get_top_matches(self):
        return await self._make_request('gettopmatches')

    # Other

    async def get_league_leaderboard(self, queue_id: QueueId, tier: int, _round: int):
        return await self._make_request('getleagueleaderboard', queue_id.value, tier, _round)

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
        return await self._make_request('getsportsproleaguedetails')

    async def get_motd(self):
        return await self._make_request('getmotd')