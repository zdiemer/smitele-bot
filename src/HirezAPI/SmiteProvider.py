import io
import json
import os
from json.decoder import JSONDecodeError
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import pandas as pd

from god import God
from god_types import GodId
from item import Item
from HirezAPI import Smite


class SmiteProvider(Smite):
    CONFIG_FILE: str = "config.json"
    GODS_FILE: str = "gods.json"
    ITEMS_FILE: str = "items.json"
    SMITE_PATCH_VERSION_FILE: str = "version"

    gods: Dict[GodId, God]
    items: Dict[int, Item]
    player_matches: pd.DataFrame

    # Cached config values
    __config: dict = None

    def __init__(self):
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

        super().__init__(self.__config["hirezAuthKey"], self.__config["hirezDevId"])

    def __init_dataframe(self):
        print("Loading match details from file.")
        match_details = {}

        for root, _, files in os.walk(".\\matchDetails"):
            for file in files:
                with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                    match_details.update(json.loads(f.read()))

        player_details = []

        print("Flattening match details to player details.")

        for _, players in match_details.items():
            if len(players) < 10:
                continue
            player_details.extend(players)

        print("Converting details to DataFrame.")

        self.player_matches = pd.DataFrame.from_dict(player_details)

        self.player_matches["GodType"] = self.player_matches.apply(
            lambda x: self.gods[GodId(int(x["GodId"]))].type.value, axis=1
        )

        cached_results = {}

        def get_match_god_ids(row: Any, allies: bool = False) -> str:
            if (int(row["Match"]), allies) in cached_results:
                return cached_results[(int(row["Match"]), allies)]

            players = match_details[str(row["Match"])]
            god_ids = []

            for player in players:
                if player["Win_Status"] == row["Win_Status"]:
                    if not allies or player["GodId"] == row["GodId"]:
                        continue
                    god_ids.append(str(player["GodId"]))
                else:
                    if allies:
                        continue
                    god_ids.append(str(player["GodId"]))

            output = ",".join(sorted(god_ids))
            cached_results[(int(row["Match"]), allies)] = output

            return output

        def god_ids_to_types(id_str: str) -> str:
            ids = [GodId(int(gid)) for gid in id_str.split(",")]

            return ",".join(sorted(self.gods[g].type.value[0] for g in ids))

        def god_ids_to_roles(id_str: str) -> str:
            ids = [GodId(int(gid)) for gid in id_str.split(",")]

            return ",".join(sorted(self.gods[g].role.value[0] for g in ids))

        def get_match_god_types(row: Any, allies: bool = False) -> str:
            if (int(row["Match"]), allies) in cached_results:
                return god_ids_to_types(cached_results[(int(row["Match"]), allies)])

            return god_ids_to_types(get_match_god_ids(row, allies))

        def get_match_god_roles(row: Any, allies: bool = False) -> str:
            if (int(row["Match"]), allies) in cached_results:
                return god_ids_to_roles(cached_results[(int(row["Match"]), allies)])

            return god_ids_to_roles(get_match_god_ids(row, allies))

        print("Applying enemy god IDs per row.")

        self.player_matches["EnemyGodIds"] = self.player_matches.apply(
            get_match_god_ids, axis=1
        )

        self.player_matches["EnemyGodRoles"] = self.player_matches.apply(
            get_match_god_roles, axis=1
        )

        self.player_matches["EnemyGodTypes"] = self.player_matches.apply(
            get_match_god_types, axis=1
        )

    async def create(self):
        should_refresh, current_patch = await self.__should_refresh()

        gods = await self.__load_cache(
            self.GODS_FILE, self.get_gods if should_refresh else None
        )

        gods_list = [God.from_json(god) for god in gods]
        self.gods = {god.id: god for god in gods_list}

        items = await self.__load_cache(
            self.ITEMS_FILE, self.get_items if should_refresh else None
        )

        item_list = [Item.from_json(item) for item in items]
        self.items = {item.id: item for item in item_list}

        if should_refresh:
            with open(self.SMITE_PATCH_VERSION_FILE, "w", encoding="utf-8") as file:
                file.write(str(current_patch))

        self.__init_dataframe()

    async def __load_cache(
        self, file_name: str, refresher: Callable[[], Awaitable[Any]] = None
    ) -> List[Any]:
        tmp: List[Any] = []
        if refresher is not None:
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
                    print(
                        f"Current local cache ({cached_version}) "
                        f"is out of date ({current_patch}), refreshing"
                    )
                    return (True, current_patch)
        except (FileNotFoundError, ValueError):
            print("Failed to open version cache, refreshing")
            return (True, current_patch)

        return (False, None)

    async def __write_to_file_from_api(
        self, file: io.TextIOWrapper, refresher: Callable[[], Awaitable[Any]]
    ) -> Any:
        tmp = await refresher()
        json.dump(tmp, file)
        return tmp
