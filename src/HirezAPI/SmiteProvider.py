import asyncio
import io
import os
from datetime import datetime, timedelta
from json.decoder import JSONDecodeError
from typing import Any, Awaitable, Callable, Dict, List, Set, Tuple

import pandas as pd
import ujson as json
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
        "Entry_Datetime",
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
        self, file_name: str, df: pd.DataFrame = None
    ) -> pd.DataFrame:
        if file_name in self.__fetched_match_detail_file_names:
            return df

        with open(file_name, "r", encoding="utf-8") as f:
            start = datetime.utcnow()

            file_details = json.loads(f.read())

            file_details = list(
                filter(
                    lambda m: m is not None,
                    file_details,
                )
            )

            if not self._silent:
                print(
                    f"Reading from JSON file took {(datetime.utcnow() - start).total_seconds():.2f}s"
                )

            start = datetime.utcnow()

            match_details = pd.DataFrame.from_records(
                file_details,
                exclude=self.__COLUMNS_TO_EXCLUDE,
            )

            if not self._silent:
                print(
                    f"Converting {match_details.shape[0]:,} details from {file_name} "
                    f"to DataFrame took {(datetime.utcnow() - start).total_seconds():.2f}s"
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
        for root, _, files in os.walk(".\\src\\match_data_collector\\output"):
            for file in files:
                self.__update_player_matches(
                    self.__match_details_file_to_dataframe(os.path.join(root, file))
                )

    def __update_player_matches(self, new_match_details: pd.DataFrame):
        if new_match_details is None or new_match_details.shape[0] == 0:
            if not self._silent:
                print("Nothing to refresh.")
            return

        start = datetime.utcnow()

        new_match_details = new_match_details[new_match_details["GodId"] != 0]

        details_length, _ = new_match_details.shape

        new_match_details["Win_Status"] = new_match_details.apply(
            lambda x: x["Win_Status"] == "Winner", axis=1
        )

        new_match_details.loc[
            new_match_details["match_queue_id"] == QueueId.UNDER_30_ARENA.value,
            "match_queue_id",
        ] = QueueId.ARENA.value

        new_match_details.loc[
            new_match_details["match_queue_id"] == QueueId.UNDER_30_CONQUEST.value,
            "match_queue_id",
        ] = QueueId.CONQUEST.value

        new_match_details.loc[
            new_match_details["match_queue_id"] == QueueId.UNDER_30_JOUST.value,
            "match_queue_id",
        ] = QueueId.JOUST.value

        match_to_god_ids: Dict[str, Dict[bool, List[int]]] = {}

        for _, player in new_match_details.iterrows():
            match_dict = match_to_god_ids.get(player["Match"]) or {}
            god_ids = match_dict.get(player["Win_Status"]) or []

            god_ids.append(str(player["GodId"]))
            match_dict[player["Win_Status"]] = god_ids
            match_to_god_ids[player["Match"]] = match_dict

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
            match_id = row["Match"]

            if match_id in cached_results:
                return cached_results[match_id]

            ally_str = ",".join(sorted(match_to_god_ids[match_id][row["Win_Status"]]))
            enemy_str = (
                ",".join(sorted(match_to_god_ids[match_id][not row["Win_Status"]]))
                if (not row["Win_Status"]) in match_to_god_ids[match_id]
                else ""
            )
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
                allies, enemies = cached_results[row["Match"]]
                return (god_ids_to_types(allies), god_ids_to_types(enemies))

            allies, enemies = get_match_god_ids(row)
            return (god_ids_to_types(allies), god_ids_to_types(enemies))

        def get_match_god_roles(row: pd.Series) -> Tuple[str, str]:
            if row.name in cached_results:
                allies, enemies = cached_results[row["Match"]]
                return (god_ids_to_roles(allies), god_ids_to_roles(enemies))

            allies, enemies = get_match_god_ids(row)
            return (god_ids_to_roles(allies), god_ids_to_roles(enemies))

        def generate_build(row: pd.Series) -> Tuple[str, str, bool, bool]:
            items = [int(row[f"ItemId{i}"]) for i in range(1, 7)]
            relics = [int(row[f"ActiveId{i}"]) for i in range(1, 3)]

            is_full_build = True

            for item_id in items:
                if item_id not in self.items:
                    return (None, None, False, False)

                item = self.items[item_id]

                is_full_build = (
                    is_full_build
                    and item_id != 0
                    and (item.tier >= 3 or item.is_starter)
                )

            is_full_relics = True

            for relic_id in relics:
                if relic_id not in self.items:
                    return (None, None, False, False)

                # No Relic & No Shard Relic
                is_full_relics = is_full_relics and relic_id not in (0, 12333, 23795)

            return (
                self.hash_list(items) if is_full_build else None,
                ",".join(str(r) for r in relics) if is_full_relics else None,
                is_full_build,
                is_full_relics,
            )

        stop = datetime.utcnow()

        if not self._silent:
            print(f"Preprocessing DataFrame took {(stop - start).total_seconds():.2f}s")

        start = datetime.utcnow()

        if not self._silent:
            print(f"Generating Builds for {details_length:,} player rows.")

        new_match_details[["BuildHash", "Relics", "IsFullBuild", "IsFullRelics"]] = (
            new_match_details.apply(generate_build, axis=1, result_type="expand")
        )

        # Drop all the columns we no longer need
        new_match_details.drop(
            columns=[
                "ActiveId1",
                "ActiveId2",
            ],
            inplace=True,
        )

        stop = datetime.utcnow()

        if not self._silent:
            print(f"Generating Builds took {(stop - start).total_seconds():.2f}s")
            # print(f"Applying team data for {new_match_details.shape[0]:,} player rows.")

        # start = datetime.utcnow()

        # new_match_details[
        #     [
        #         "AllyGodIds",
        #         "EnemyGodIds",
        #         "AllyGodTypes",
        #         "EnemyGodTypes",
        #         "AllyGodRoles",
        #         "EnemyGodRoles",
        #     ]
        # ] = new_match_details.apply(get_match_team_data, axis=1, result_type="expand")

        # stop = datetime.utcnow()

        # if not self._silent:
        #     print(f"Applying team data took {(stop - start).total_seconds():.2f}s")

        if self.player_matches is None:
            self.player_matches = new_match_details
        else:
            new_match_details = pd.concat([self.player_matches, new_match_details])
            new_match_details.drop_duplicates(inplace=True)
            new_match_details.reset_index(drop=True, inplace=True)

            self.player_matches = new_match_details

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

        await asyncio.to_thread(lambda: self.__update_player_matches(new_match_details))

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

    # From: https://stackoverflow.com/questions/28326965/good-hash-function-for-list-of-integers-where-order-doesnt-change-value
    @staticmethod
    def triple32(x: int) -> int:
        x ^= x >> 17
        x *= 0xED5AD4BB
        x ^= x >> 11
        x *= 0xAC4C1B51
        x ^= x >> 15
        x *= 0x31848BAB
        x ^= x >> 14
        return x

    @staticmethod
    def hash_list(ints: List[int]) -> int:
        return sum(SmiteProvider.triple32(i) for i in ints)
