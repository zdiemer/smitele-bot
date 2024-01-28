import asyncio
import io
import json
import os
from datetime import datetime, timedelta
from json.decoder import JSONDecodeError
from typing import Any, Awaitable, Callable, Dict, List, Set, Tuple

import pandas as pd
from aiohttp import ClientConnectionError, ContentTypeError

from god import God
from god_types import GodId
from item import Item
from HirezAPI import Smite, QueueId


class SmiteProvider(Smite):
    CONFIG_FILE: str = "config.json"
    GODS_FILE: str = "gods.json"
    ITEMS_FILE: str = "items.json"
    SMITE_PATCH_VERSION_FILE: str = "version"

    gods: Dict[GodId, God]
    items: Dict[int, Item]
    player_matches: pd.DataFrame = None

    # Cached config values
    __config: dict = None

    __fetched_match_detail_file_names: Set[str]

    __OUTPUT_FILE_PREFIX: str = "match_details_"
    __OUTPUT_FILE_DATE_FORMAT: str = "%Y-%m-%d"

    __COLUMNS_TO_EXCLUDE: List[str] = [
        "Account_Level",
        "Ban1",
        "Ban1Id",
        "Ban2",
        "Ban2Id",
        "Ban3",
        "Ban3Id",
        "Ban4",
        "Ban4Id",
        "Ban5",
        "Ban5Id",
        "Ban6",
        "Ban6Id",
        "Ban7",
        "Ban7Id",
        "Ban8",
        "Ban8Id",
        "Ban9",
        "Ban9Id",
        "Ban10",
        "Ban10Id",
        "Ban11",
        "Ban11Id",
        "Ban12",
        "Ban12Id",
        "First_Ban_Side",
        "Item_Active_1",
        "Item_Active_2",
        "Item_Active_3",
        "Item_Active_4",
        "Item_Purch_1",
        "Item_Purch_2",
        "Item_Purch_3",
        "Item_Purch_4",
        "Item_Purch_5",
        "Item_Purch_6",
        "MergedPlayers",
        "hasReplay",
        "hz_gamer_tag",
        "hz_player_name",
        "playerId",
        "playerName",
        "playerPortalId",
        "playerPortalUserId",
        "ret_msg",
        "Skin",
        "SkinId",
        "Reference_Name",
        "ActivePlayerId",
        "ActiveId3",
        "ActiveId4",
        "name",
        "TaskForce",
        "Team1Score",
        "Team2Score",
        "TeamId",
        "Team_Name",
        "Winning_TaskForce",
        "Conquest_Losses",
        "Conquest_Points",
        "Conquest_Wins",
        "Duel_Losses",
        "Duel_Points",
        "Duel_Wins",
        "Joust_Losses",
        "Joust_Points",
        "Joust_Wins",
        "Kills_Siege_Juggernaut",
        "Kills_Wild_Juggernaut",
        "Map_Game",
        "Mastery_Level",
        # Below columns may be useful later
        "Damage_Bot",
        "Damage_Done_In_Hand",
        "Damage_Done_Magical",
        "Damage_Done_Physical",
        "Damage_Mitigated",
        "Damage_Taken",
        "Damage_Taken_Magical",
        "Damage_Taken_Physical",
        "Distance_Traveled",
        "Final_Match_Level",
        "Gold_Earned",
        "Gold_Per_Minute",
        "Healing",
        "Healing_Bot",
        "Healing_Player_Self",
        "Killing_Spree",
        "Kills_Bot",
        "Kills_Double",
        "Kills_Fire_Giant",
        "Kills_First_Blood",
        "Kills_Gold_Fury",
        "Kills_Penta",
        "Kills_Phoenix",
        "Kills_Quadra",
        "Kills_Single",
        "Kills_Triple",
        "Match_Duration",
        "Minutes",
        "Multi_kill_Max",
        "Objective_Assists",
        "PartyId",
        "Region",
        "Structure_Damage",
        "Time_Dead_Seconds",
        "Time_In_Match_Seconds",
        "Towers_Destroyed",
        "Wards_Placed",
        "Camps_Cleared",
        "Surrendered",
    ]

    def __init__(self, silent: bool = False):
        if self.__config is None:
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as file:
                    self.__config = json.load(file)

                    if not "hirezDevId" in self.__config:
                        raise RuntimeError(
                            f"{self.CONFIG_FILE} " 'was missing value for "hirezDevId."'
                        )
                    if not "hirezAuthKey" in self.__config:
                        raise RuntimeError(
                            f"{self.CONFIG_FILE} "
                            'was missing value for "hirezAuthKey."'
                        )
            except (FileNotFoundError, JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Failed to load {self.CONFIG_FILE}. Does this file exist?"
                ) from exc

        self.__fetched_match_detail_file_names = set()

        super().__init__(
            self.__config["hirezAuthKey"], self.__config["hirezDevId"], silent=silent
        )

    def __match_details_file_to_dataframe(
        self, file_name: str, df: pd.DataFrame
    ) -> pd.DataFrame:
        if file_name in self.__fetched_match_detail_file_names:
            return df

        with open(file_name, "r", encoding="utf-8") as f:
            file_details = json.loads(f.read())

            file_details = list(
                filter(
                    lambda m: m is not None and m["ret_msg"] is None,
                    file_details,
                )
            )

            match_details = pd.DataFrame.from_records(
                file_details,
                exclude=self.__COLUMNS_TO_EXCLUDE,
            )

            if not self._silent:
                print(
                    f"Converted {match_details.shape[0]:,} details from {file_name} to DataFrame"
                )

            if df is None:
                df = match_details
            else:
                df = (
                    pd.concat([df, match_details])
                    .drop_duplicates()
                    .reset_index(drop=True)
                )

            self.__fetched_match_detail_file_names.add(file_name)

        return df

    def __refresh_dataframe(self):
        new_match_details: pd.DataFrame = None

        for root, _, files in os.walk(".\\src\\match_data_collector\\output"):
            for file in files:
                new_match_details = self.__match_details_file_to_dataframe(
                    os.path.join(root, file), new_match_details
                )

        self.__update_player_matches(new_match_details)

    def __update_player_matches(self, new_match_details: pd.DataFrame):
        if new_match_details is None or new_match_details.shape[0] == 0:
            if not self._silent:
                print("Nothing to refresh.")
            return

        new_match_details.set_index("Match", inplace=True)

        new_match_details["Entry_Datetime"] = pd.to_datetime(
            new_match_details["Entry_Datetime"], format="%m/%d/%Y %I:%M:%S %p"
        )

        new_match_details = new_match_details[
            new_match_details["Entry_Datetime"]
            # Start of season 11 is around this time
            > pd.to_datetime("1/23/2024 03:00:00 PM", format="%m/%d/%Y %I:%M:%S %p")
        ]

        new_match_details = new_match_details[new_match_details["GodId"] != 0]

        details_length, _ = new_match_details.shape

        new_match_details["Win_Status"] = new_match_details.apply(
            lambda x: x["Win_Status"] == "Winner", axis=1
        )

        cached_results = {}

        def get_match_team_data(row: pd.Series) -> Tuple[str, str, str, str, str, str]:
            ally_ids, enemy_ids = get_match_god_ids(row)
            ally_types, enemy_types = get_match_god_types(row)
            ally_roles, enemy_roles = get_match_god_roles(row)
            return (
                ally_ids,
                enemy_ids,
                ally_types,
                enemy_types,
                ally_roles,
                enemy_roles,
            )

        def get_match_god_ids(row: pd.Series) -> Tuple[str, str]:
            match_id = row.name

            if match_id in cached_results:
                return cached_results[match_id]

            players = new_match_details.loc[match_id]

            if isinstance(players, pd.Series):
                # This happens only in Ranked Duel when someone has disconnected prior to losing the game
                return (str(players["GodId"]), "")

            allies = []
            enemies = []

            for _, player in players.iterrows():
                if player["Win_Status"] == row["Win_Status"]:
                    allies.append(str(player["GodId"]))
                else:
                    enemies.append(str(player["GodId"]))

            ally_str = ",".join(sorted(allies))
            enemy_str = ",".join(sorted(enemies))
            cached_results[match_id] = (ally_str, enemy_str)

            return (ally_str, enemy_str)

        def god_ids_to_types(id_str: str) -> str | None:
            if not any(id_str):
                return None
            ids = [GodId(int(gid)) for gid in id_str.split(",")]

            return ",".join(sorted(self.gods[g].type.value[0] for g in ids))

        def god_ids_to_roles(id_str: str) -> str | None:
            if not any(id_str):
                return None
            ids = [GodId(int(gid)) for gid in id_str.split(",")]

            return ",".join(sorted(self.gods[g].role.value[0] for g in ids))

        def get_match_god_types(row: pd.Series) -> Tuple[str, str]:
            if row.name in cached_results:
                allies, enemies = cached_results[row.name]
                return (god_ids_to_types(allies), god_ids_to_types(enemies))

            allies, enemies = get_match_god_ids(row)
            return (god_ids_to_types(allies), god_ids_to_types(enemies))

        def get_match_god_roles(row: pd.Series) -> Tuple[str, str]:
            if row.name in cached_results:
                allies, enemies = cached_results[row.name]
                return (god_ids_to_roles(allies), god_ids_to_roles(enemies))

            allies, enemies = get_match_god_ids(row)
            return (god_ids_to_roles(allies), god_ids_to_roles(enemies))

        start = datetime.utcnow()

        if not self._silent:
            print(f"Generating Builds for {details_length:,} player rows.")

        new_match_details["Build"] = new_match_details.apply(
            lambda x: ",".join(
                [
                    str(x["ItemId1"]),
                    str(x["ItemId2"]),
                    str(x["ItemId3"]),
                    str(x["ItemId4"]),
                    str(x["ItemId5"]),
                    str(x["ItemId6"]),
                ]
            ),
            axis=1,
        )

        new_match_details["Relics"] = new_match_details.apply(
            lambda x: ",".join([str(x["ActiveId1"]), str(x["ActiveId2"])]), axis=1
        )

        # Drop all the columns we no longer need
        new_match_details.drop(
            columns=[
                "Entry_Datetime",
                "ItemId1",
                "ItemId2",
                "ItemId3",
                "ItemId4",
                "ItemId5",
                "ItemId6",
                "ActiveId1",
                "ActiveId2",
            ],
            inplace=True,
        )

        stop = datetime.utcnow()

        if not self._silent:
            print(f"Generating Builds took {(stop - start).total_seconds():.2f}s")
            print("Applying team data per row.")

        start = datetime.utcnow()

        new_match_details[
            [
                "AllyGodIds",
                "EnemyGodIds",
                "AllyGodTypes",
                "EnemyGodTypes",
                "AllyGodRoles",
                "EnemyGodRoles",
            ]
        ] = new_match_details.apply(get_match_team_data, axis=1, result_type="expand")

        stop = datetime.utcnow()

        if not self._silent:
            print(f"Applying team data took {(stop - start).total_seconds():.2f}s")

        if self.player_matches is None:
            self.player_matches = new_match_details
        else:
            self.player_matches = (
                pd.concat([self.player_matches, new_match_details])
                .drop_duplicates()
                .reset_index(drop=True)
            )

    async def create(self):
        should_refresh, current_patch = await self.__should_refresh()

        gods = await self.__load_cache(self.GODS_FILE, should_refresh, self.get_gods)

        gods_list = [God.from_json(god) for god in gods]
        self.gods = {god.id: god for god in gods_list}

        items = await self.__load_cache(self.ITEMS_FILE, should_refresh, self.get_items)

        item_list = [Item.from_json(item) for item in items]
        self.items = {item.id: item for item in item_list}

        if should_refresh:
            with open(self.SMITE_PATCH_VERSION_FILE, "w", encoding="utf-8") as file:
                file.write(str(current_patch))

    async def load_dataframe(self):
        pd.options.mode.copy_on_write = True
        await asyncio.to_thread(self.__refresh_dataframe)
        asyncio.get_running_loop().create_task(self.__refresh_dataframe_loop())

    async def __load_cache(
        self,
        file_name: str,
        should_refresh=False,
        refresher: Callable[[], Awaitable[Any]] = None,
    ) -> List[Any]:
        tmp: List[Any] = []
        if should_refresh:
            with open(file_name, "w", encoding="utf-8") as file:
                tmp = await self.__write_to_file_from_api(file, refresher)
        else:
            try:
                with open(file_name, "r+", encoding="utf-8") as file:
                    tmp = json.load(file)
            except (FileNotFoundError, JSONDecodeError):
                with open(file_name, "w", encoding="utf-8") as file:
                    tmp = await self.__write_to_file_from_api(file, refresher)
        return tmp

    async def __should_refresh(self) -> Tuple[bool, float]:
        current_patch = float((await self.get_patch_info())["version_string"])
        try:
            with open(self.SMITE_PATCH_VERSION_FILE, "r+", encoding="utf-8") as file:
                cached_version = float(file.read())
                if current_patch > cached_version:
                    if not self._silent:
                        print(
                            f"Current local cache ({cached_version}) "
                            f"is out of date ({current_patch}), refreshing"
                        )
                    return (True, current_patch)
        except (FileNotFoundError, ValueError):
            if not self._silent:
                print("Failed to open version cache, refreshing")
            return (True, current_patch)

        return (False, None)

    async def __write_to_file_from_api(
        self, file: io.TextIOWrapper, refresher: Callable[[], Awaitable[Any]]
    ) -> Any:
        tmp = await refresher()
        json.dump(tmp, file)
        return tmp

    async def __fetch_new_match_data(self, fetch_date: datetime):
        new_match_details: pd.DataFrame = None

        for root, _, files in os.walk(".\\src\\match_data_collector\\output"):
            for file in files:
                new_match_details = self.__match_details_file_to_dataframe(
                    os.path.join(root, file), new_match_details
                )

        match_ids = set()

        for queue in list(
            filter(
                lambda q: QueueId.is_normal(q) or QueueId.is_ranked(q),
                list(QueueId),
            )
        ):
            req_count = 0
            while req_count < self.MAX_RETRIES:
                try:
                    matches = await self.get_match_ids_by_queue(
                        queue,
                        fetch_date.strftime("%Y%m%d"),
                        fetch_date.hour,
                        fetch_date.minute,
                    )
                    match_ids = match_ids.union(
                        [
                            match["Match"]
                            for match in list(
                                filter(lambda m: m["Active_Flag"] == "n", matches)
                            )
                        ]
                    )
                    break
                except ClientConnectionError:
                    req_count += 1
        match_ids = list(match_ids)

        if not self._silent:
            print(f"Found {len(match_ids)} matches to fetch")

        for i in range(0, len(match_ids), 10):
            req_count = 0
            while req_count < self.MAX_RETRIES:
                try:
                    match_res = await self.get_match_details_batch(
                        match_ids[i : i + 10]
                    )

                    match_res = list(
                        filter(
                            lambda m: m is not None and m["ret_msg"] is None,
                            match_res,
                        )
                    )

                    match_details = pd.DataFrame.from_records(
                        match_res,
                        exclude=self.__COLUMNS_TO_EXCLUDE,
                    )

                    if not self._silent:
                        print(
                            f"Converted {match_details.shape[0]:,} details to DataFrame"
                        )

                    if new_match_details is None:
                        new_match_details = match_details
                    else:
                        new_match_details = (
                            pd.concat([new_match_details, match_details])
                            .drop_duplicates()
                            .reset_index(drop=True)
                        )
                    break
                except (
                    json.JSONDecodeError,
                    ClientConnectionError,
                    ContentTypeError,
                    TypeError,
                ) as e:
                    if not self._silent:
                        print(
                            f"Failed to fetch match details due to {e}{', retrying' if req_count + 1 < self.MAX_RETRIES else ''}"
                        )
                    req_count += 1

        self.__update_player_matches(new_match_details)

    async def __refresh_dataframe_loop(self):
        while True:
            now = datetime.utcnow()
            wait_seconds = (10 - (now.minute % 10)) * 60

            if now.second > 0:
                wait_seconds -= now.second

            if not self._silent:
                print(f"Sleeping {wait_seconds}s for next DataFrame refresh")

            await asyncio.sleep(wait_seconds)

            request_date = datetime.utcnow()
            request_date = request_date.replace(minute=(request_date.minute // 10) * 10)
            request_date = request_date - timedelta(minutes=30)

            try:
                if not self._silent:
                    print(
                        f"Requesting matches for {request_date.strftime('%Y%m%d %H:%M')}"
                    )
                await self.__fetch_new_match_data(request_date)
            except Exception as e:
                print(f"Uncaught exception in DataFrame refreshing loop: {e}")
