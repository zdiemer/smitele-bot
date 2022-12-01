import io
import json
from json.decoder import JSONDecodeError
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from god import God
from god_types import GodId
from item import Item
from HirezAPI import Smite

class SmiteProvider(Smite):
    CONFIG_FILE: str = 'config.json'
    GODS_FILE: str = 'gods.json'
    ITEMS_FILE: str = 'items.json'
    SMITE_PATCH_VERSION_FILE: str = 'version'

    gods: Dict[GodId, God]
    items: Dict[int, Item]

    # Cached config values
    __config: dict = None

    def __init__(self):
        if self.__config is None:
            try:
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as file:
                    self.__config = json.load(file)

                    if not 'hirezDevId' in self.__config:
                        raise RuntimeError(f'{self.CONFIG_FILE} '\
                            'was missing value for "hirezDevId."')
                    if not 'hirezAuthKey' in self.__config:
                        raise RuntimeError(f'{self.CONFIG_FILE} '\
                            'was missing value for "hirezAuthKey."')
            except (FileNotFoundError, JSONDecodeError) as exc:
                raise RuntimeError(f'Failed to load {self.CONFIG_FILE}. Does this file exist?') \
                    from exc

        super().__init__(self.__config['hirezAuthKey'], self.__config['hirezDevId'])

    async def create(self):
        should_refresh, current_patch = await self.__should_refresh()

        gods = await self.__load_cache(self.GODS_FILE, \
            self.get_gods if should_refresh else None)

        gods_list = [God.from_json(god) for god in gods]
        self.gods = {god.id:god for god in gods_list}

        items = await self.__load_cache(self.ITEMS_FILE, \
            self.get_items if should_refresh else None)

        item_list = [Item.from_json(item) for item in items] 
        self.items = {item.id:item for item in item_list}

        if should_refresh:
            with open(self.SMITE_PATCH_VERSION_FILE, 'w', encoding='utf-8') as file:
                file.write(str(current_patch))

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
        current_patch = float((await self.get_patch_info())['version_string'])
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

    async def __write_to_file_from_api(self, file: io.TextIOWrapper, \
            refresher: Callable[[], Awaitable[Any]]) -> Any:
        tmp = await refresher()
        json.dump(tmp, file)
        return tmp
