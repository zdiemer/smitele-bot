import io
from enum import Enum
from typing import List

import aiohttp

from god import GodRole

class ItemAttribute(Enum):
    ATTACK_SPEED = 'attack speed'
    BASIC_ATTACK_DAMAGE = 'basic attack damage'
    COOLDOWN_REDUCTION = 'cooldown reduction'
    CRITICAL_STRIKE_CHANCE = 'critical strike chance'
    CROWD_CONTROL_REDUCTION = 'crowd control reduction'
    DAMAGE_REDUCTION = 'damage reduction'
    HP5 = 'hp5'
    HP5_AND_MP5 = 'hp5 & mp5'
    HEALTH = 'health'
    MP5 = 'mp5'
    MAGICAL_LIFESTEAL = 'magical lifesteal'
    MAGICAL_PENETRATION = 'magical penetration'
    MAGICAL_POWER = 'magical power'
    MAGICAL_PROTECTION = 'magical protection'
    MANA = 'mana'
    MAXIMUM_HEALTH = 'maximum health'
    MOVEMENT_SPEED = 'movement speed'
    PENETRATION = 'penetration'
    PHYSICAL_CRITICAL_STRIKE_CHANCE = 'physical critical strike chance'
    PHYSICAL_LIFESTEAL = 'physical lifesteal'
    PHYSICAL_PENETRATION = 'physical penetration'
    PHYSICAL_POWER = 'physical power'
    PHYSICAL_PROTECTION = 'physical protection'
    PROTECTIONS = 'protections'

    @staticmethod
    def from_string(value: str):
        value = value.lower().strip()

        if value == 'magical protections':
            return ItemAttribute.MAGICAL_PROTECTION

        return ItemAttribute(value)

    @property
    def display_name(self) -> str:
        return str(self.value).title().replace('Hp5', 'HP5').replace('Mp5', 'MP5')

class ItemType(Enum):
    CONSUMABLE = 'consumable'
    ITEM = 'item'
    RELIC = 'active'

class ItemProperty:
    attribute: ItemAttribute
    flat_value: float
    percent_value: float

    def __init__(self, attribute: ItemAttribute, \
            flat_value: float = None, percent_value: float = None):
        self.attribute = attribute
        self.flat_value = flat_value
        self.percent_value = percent_value

    @staticmethod
    def from_json(obj):
        attribute = ItemAttribute.from_string(obj['Description'])
        value: str = obj['Value'].replace('+', '').strip()

        if value.endswith('%'):
            return ItemProperty(attribute, percent_value=float(value.replace('%', '')) / 100)
        return ItemProperty(attribute, flat_value=float(value))

class Item:
    active: bool
    parent_item_id: int
    name: str
    glyph: bool
    icon_id: int
    description: str
    item_properties: List[ItemProperty]
    passive: str = None
    aura: str = None
    id: int
    tier: int
    price: int
    restricted_roles: List[GodRole]
    is_starter: bool
    type: ItemType
    icon_url: str

    def __init__(self):
        pass

    @staticmethod
    def from_json(obj):
        item = Item()
        item.active = obj['ActiveFlag'] == 'y'
        parent_item_id = int(obj['ChildItemId'])
        item.parent_item_id = parent_item_id if parent_item_id != 0 else None
        item.name = obj['DeviceName']
        item.glyph = obj['Glyph'] == 'y'
        item.icon_id = int(obj['IconId'])
        item.description = obj['ItemDescription']['Description']
        item.id = int(obj['ItemId'])
        item.tier = int(obj['ItemTier'])
        item.price = int(obj['Price'])
        item.is_starter = bool(obj['StartingItem'])
        item.type = ItemType(obj['Type'].lower())
        # Temporary correction for this mistake in Hirez's response
        item.icon_url = obj['itemIcon_URL']\
            .replace('manticores-spikes', 'manticores-spike')

        restricted = obj['RestrictedRoles'].lower()
        item.restricted_roles = []
        if restricted.strip() != 'no restrictions':
            roles = restricted.split(',')
            item.restricted_roles = [GodRole(role.strip()) for role in roles]

        secondary: str = obj['ItemDescription']['SecondaryDescription']
        if secondary is not None:
            if secondary.startswith('AURA'):
                item.aura = secondary.replace('AURA - ', '', 1)
            else:
                item.passive = secondary.replace('PASSIVE - ', '', 1)\
                    .replace('PASSIVE: ', '', 1).replace('<n>GLPYH - ', '', 1)\
                    .replace('GLYPH - ', '', 1)

        if item.type is ItemType.ITEM:
            item.item_properties = [ItemProperty.from_json(val) for \
                val in obj['ItemDescription']['Menuitems']]
        return item

    async def get_icon_bytes(self) -> io.BytesIO:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.icon_url) as res:
                return io.BytesIO(await res.content.read())
