import io
from typing import List, NamedTuple

import aiohttp

class _item:
    description: str
    value: str

    def __init__(self, description: str, value: str):
        self.description = description
        self.value = value

    @staticmethod
    def from_json(obj):
        return _item(obj['description'], obj['value'])

class _itemDescription:
    cooldown: str
    cost: str
    description: str
    menu_items: List[_item]
    rank_items: List[_item]

    def __init__(self, cooldown: str, cost: str, description: str, \
            menu_items: List[_item], rank_items: List[_item]):
        self.cooldown = cooldown
        self.cost = cost
        self.description = description

        self.menu_items = menu_items
        self.rank_items = rank_items

    @staticmethod
    def from_json(obj):
        cooldown = obj['cooldown']
        cost = obj['cost']
        description = obj['description']

        menu_items = [_item.from_json(item) for item in obj['menuitems']]
        rank_items = [_item.from_json(item) for item in obj['rankitems']]
        return _itemDescription(cooldown, cost, description, \
            menu_items, rank_items)

class AbilityProperty(NamedTuple):
    name: str
    value: str

    def __str__(self):
        return f'{self.name}: {self.value}\n'

class RankProperty(NamedTuple):
    name: str
    rank_values: str

class Ability(object):
    __item_description: _itemDescription
    id: int
    name: str
    icon_url: str

    def __init__(self, item_description: _itemDescription, id: int, name: str, icon_url: str):
        self.__item_description = item_description
        self.id = id
        self.name = name
        self.icon_url = icon_url

    @staticmethod
    def from_json(obj):
        item_description = _itemDescription.from_json(obj['Description']['itemDescription'])
        id = int(obj['Id'])
        name = obj['Summary']
        icon_url = obj['URL']
        return Ability(item_description, id, name, icon_url)

    @property
    def cooldown_by_rank(self) -> List[float]:
        cd_str = self.__item_description.cooldown
        if cd_str == '' or cd_str is None:
            return []
        try:
            return [float(cool.strip()) for cool in cd_str.replace('s', '').split('/')]
        except ValueError:
            print(f'Error while extracting cooldowns from {cd_str}')
            return []

    __cost_modifiers = ['+ 1 arrow per shot', 'per shot', 'Omi', 'Rage', 'every 0.5s.']

    @property
    def cost_by_rank(self) -> List[int]:
        cost_str = self.__item_description.cost
        # Variable is a special case for Heimdallr's Bifrost
        if cost_str in ('', 'None', 'Variable') or cost_str is None:
            return []
        # Special case for King Arthur's ultimate
        if cost_str == '35 (80) Energy & 40 Mana':
            return 40
        try:
            for modifier in self.__cost_modifiers:
                cost_str = cost_str.replace(modifier, '')
            return [int(cost.strip()) for cost in cost_str.split('/')]
        except (ValueError, KeyError):
            print(f'Error while extracting costs from {cost_str}')
            return []

    @property
    def cost_modifier(self) -> str:
        # Special case for ONLY King Arthur's ultimate, which has a static
        # mana cost and varying energy costs
        if '35 (80) Energy & 40 Mana' in self.__item_description.cost:
            return '& 35 (80) Energy'
        for modifier in self.__cost_modifiers:
            if modifier in self.__item_description.cost:
                return modifier
        return None

    @property
    def description(self) -> str:
        return self.__item_description.description

    async def get_icon_bytes(self) -> io.BytesIO:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.icon_url) as res:
                return io.BytesIO(await res.content.read())

    @property
    def ability_properties(self) -> List[AbilityProperty]:
        return [AbilityProperty(item.description.replace(':', '').strip(), item.value) \
            for item in self.__item_description.menu_items]

    @property
    def rank_properties(self) -> List[RankProperty]:
        return [RankProperty(item.description.replace(':', '').strip(), item.value) \
            for item in self.__item_description.rank_items]
