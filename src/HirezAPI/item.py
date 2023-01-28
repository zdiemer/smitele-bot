
from __future__ import annotations
import io
from enum import Enum
from typing import List, Set

import aiohttp

from god_types import GodRole, GodType
from passive_parser import PassiveAttribute, PassiveParser

class ItemAttribute(Enum):
    ATTACK_SPEED = 'attack speed'
    BASIC_ATTACK_DAMAGE = 'basic attack damage'
    COOLDOWN_REDUCTION = 'cooldown reduction'
    CRITICAL_STRIKE_CHANCE = 'critical strike chance'
    CROWD_CONTROL_REDUCTION = 'crowd control reduction'
    HP5 = 'hp5'
    HEALTH = 'health'
    MP5 = 'mp5'
    MAGICAL_LIFESTEAL = 'magical lifesteal'
    MAGICAL_PENETRATION = 'magical penetration'
    MAGICAL_POWER = 'magical power'
    MAGICAL_PROTECTION = 'magical protection'
    MANA = 'mana'
    MOVEMENT_SPEED = 'movement speed'
    PHYSICAL_LIFESTEAL = 'physical lifesteal'
    PHYSICAL_PENETRATION = 'physical penetration'
    PHYSICAL_POWER = 'physical power'
    PHYSICAL_PROTECTION = 'physical protection'

    # These attributes are fairly bespoke, showing up on only a few items
    DAMAGE_REDUCTION = 'damage reduction'
    HP5_AND_MP5 = 'hp5 & mp5'
    MAXIMUM_HEALTH = 'maximum health'
    PENETRATION = 'penetration'
    PHYSICAL_CRITICAL_STRIKE_CHANCE = 'physical critical strike chance'
    PROTECTIONS = 'protections'

    @staticmethod
    def from_string(value: str):
        value = value.lower().strip()

        if value == 'magical protections':
            return ItemAttribute.MAGICAL_PROTECTION
        if value == 'ccr':
            return ItemAttribute.CROWD_CONTROL_REDUCTION

        return ItemAttribute(value)

    @property
    def display_name(self) -> str:
        return str(self.value).title().replace('Hp5', 'HP5').replace('Mp5', 'MP5')

    @property
    def god_type(self) -> GodType:
        if 'protection' in self.value:
            return None
        return GodType.MAGICAL if 'magical' in self.value \
            else GodType.PHYSICAL if 'physical' in self.value else None

class ItemType(Enum):
    CONSUMABLE = 'consumable'
    ITEM = 'item'
    RELIC = 'active'

class ItemProperty:
    attribute: ItemAttribute
    flat_value: float | None
    percent_value: float | None

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
    root_item_id: int
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
    passive_properties: Set[PassiveAttribute]

    def __init__(self):
        pass

    @staticmethod
    def from_json(obj):
        item = Item()
        item.active = obj['ActiveFlag'] == 'y'
        parent_item_id = int(obj['ChildItemId'])
        item.parent_item_id = parent_item_id if parent_item_id != 0 else None
        item.root_item_id = int(obj['RootItemId'])
        item.name = obj['DeviceName']
        item.glyph = obj['Glyph'] == 'y'
        item.icon_id = int(obj['IconId'])
        item.description = obj['ItemDescription']['Description']
        item.id = int(obj['ItemId'])
        item.tier = int(obj['ItemTier'])
        item.price = int(obj['Price'])
        item.is_starter = bool(obj['StartingItem'])
        item.type = ItemType(obj['Type'].lower())
        item.icon_url = obj['itemIcon_URL']
        item.passive_properties = set()

        restricted = obj['RestrictedRoles'].lower()
        item.restricted_roles = []
        if restricted.strip() != 'no restrictions':
            roles = restricted.split(',')
            item.restricted_roles = [GodRole(role.strip()) for role in roles]

        secondary: str = obj['ItemDescription']['SecondaryDescription']
        if secondary is not None and secondary != '':
            secondary = secondary.replace('<n>', '')\
                .replace("<font color='#F44242'>", '')\
                .replace("<font color='#42F46E'>", '')
            if secondary.startswith('AURA'):
                item.aura = secondary.replace('AURA - ', '', 1)
            else:
                item.passive = secondary.replace('PASSIVE - ', '', 1)\
                    .replace('PASSIVE: ', '', 1).replace('GLYPH - ', '', 1)
            item.passive_properties = PassiveParser().parse(secondary.lower())

        if item.type is ItemType.ITEM:
            item.item_properties = [ItemProperty.from_json(val) for \
                val in obj['ItemDescription']['Menuitems']]
        return item

    async def get_icon_bytes(self) -> io.BytesIO:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.icon_url) as res:
                return io.BytesIO(await res.content.read())

class ItemTreeNode:
    item: Item
    children: List[ItemTreeNode]
    width: int
    depth: int

    def __init__(self, item: Item, depth: int = 1):
        self.item = item
        self.children = []
        self.width = 1
        self.depth = depth

    def add_child(self, item: ItemTreeNode):
        self.children.append(item)
