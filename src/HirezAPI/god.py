import io
import re
from typing import Dict, List

import aiohttp

from ability import Ability
from god_types import *
from item import ItemAttribute

class _basicAttackProperties:
    DAMAGE_REGEX = r'(?P<base_damage>\d+\.?\d*) \+ (?P<per_level>\d*\.?\d*)/Lvl \(\+(?P<scaling>\d+)%\ of (Magical|Physical) Power\)'
    __damage: str
    progression: str
    damage_scaling: str

    base_damage: float
    per_level: float
    scaling: float

    # Izanami special case
    base_damage_back: float = 0
    per_level_back: float = 0
    scaling_back: float = 0

    def __init__(self, damage: str, progression: str, damage_scaling: str):
        self.__damage = damage
        self.progression = progression
        self.damage_scaling = damage_scaling

        regex = re.compile(self.DAMAGE_REGEX)
        groups = [match.groupdict() for match in regex.finditer(self.__damage)]
        for idx, group in enumerate(groups):
            base_damage = float(group['base_damage'])
            per_level = float(group['per_level'])
            scaling = float(group['scaling']) / 100.0
            if idx == 1: # Izanami special case
                self.base_damage_back = base_damage
                self.per_level_back = per_level
                self.scaling = scaling
            else:
                self.base_damage = base_damage
                self.per_level = per_level
                self.scaling = scaling


    @staticmethod
    def from_json(obj):
        menu_items = obj['itemDescription']['menuitems']
        damage = None
        progression = None
        damage_scaling = None

        for item in menu_items:
            desc = item['description']
            value = item['value']
            if desc in ('Damage:', 'Damage'):
                damage = value
            if 'Progression:' in desc and value != 'None':
                progression = value
            if 'Damage Scaling:' in desc:
                damage_scaling = value

        return _basicAttackProperties(damage, progression, damage_scaling)

class GodStat:
    base: float
    per_level: float

    def __init__(self, base: float, per_level: float = 0):
        self.base = base
        self.per_level = per_level

class GodStats:
    values: Dict[ItemAttribute, GodStat]
    health: float
    health_per_level: float
    mp5: float
    mp5_per_level: float
    magic_protection: float
    magic_protection_per_level: float
    magical_power: float
    magical_power_per_level: float
    mana: float
    mana_per_level: float
    physical_power: float
    physical_power_per_level: float
    physical_protection: float
    physical_protection_per_level: float
    speed: float
    basic_attack: _basicAttackProperties

    def __init__(self):
        pass

    @staticmethod
    def from_json(obj):
        stats = GodStats()
        stats.values = {}
        if 'AttackSpeed' in obj and 'AttackSpeedPerLevel' in obj:
            stats.values[ItemAttribute.ATTACK_SPEED] = GodStat(
                float(obj['AttackSpeed']),
                float(obj['AttackSpeedPerLevel'])
            )
        if 'HealthPerFive' in obj and 'HP5PerLevel' in obj:
            stats.values[ItemAttribute.HP5] = GodStat(
                float(obj['HealthPerFive']),
                float(obj['HP5PerLevel'])
            )
        if 'Health' in obj and 'HealthPerLevel' in obj:
            stats.values[ItemAttribute.HEALTH] = GodStat(
                float(obj['Health']),
                float(obj['HealthPerLevel'])
            )
        if 'ManaPerFive' in obj and 'MP5PerLevel' in obj:
            stats.values[ItemAttribute.MP5] = GodStat(
                float(obj['ManaPerFive']),
                float(obj['MP5PerLevel'])
            )
        if 'MagicProtection' in obj and 'MagicProtectionPerLevel':
            stats.values[ItemAttribute.MAGICAL_PROTECTION] = GodStat(
                float(obj['MagicProtection']),
                float(obj['MagicProtectionPerLevel'])
            )
        if 'MagicalPower' in obj and 'MagicalPowerPerLevel' in obj:
            stats.values[ItemAttribute.MAGICAL_POWER] = GodStat(
                float(obj['MagicalPower']),
                float(obj['MagicalPowerPerLevel'])
            )
        if 'Mana' in obj and 'ManaPerLevel' in obj:
            stats.values[ItemAttribute.MANA] = GodStat(
                float(obj['Mana']),
                float(obj['ManaPerLevel'])
            )
        if 'PhysicalPower' in obj and 'PhysicalPowerPerLevel' in obj:
            stats.values[ItemAttribute.PHYSICAL_POWER] = GodStat(
                float(obj['PhysicalPower']),
                float(obj['PhysicalPowerPerLevel'])
            )
        if 'PhysicalProtection' in obj and 'PhysicalProtectionPerLevel' in obj:
            stats.values[ItemAttribute.PHYSICAL_PROTECTION] = GodStat(
                float(obj['PhysicalProtection']),
                float(obj['PhysicalProtectionPerLevel'])
            )
        if 'Speed' in obj:
            stats.values[ItemAttribute.MOVEMENT_SPEED] = GodStat(
                float(obj['Speed'])
            )

        stats.basic_attack = _basicAttackProperties.from_json(obj['basicAttack'])
        return stats

class God(object):
    abilities: List[Ability]
    stats: GodStats
    name: str
    role: GodRole
    type: GodType
    range: GodRange
    auto_banned: bool
    on_free_rotation: bool
    latest_god: bool
    title: str
    lore: str
    pantheon: str
    pros: List[GodPro]
    card_url: str
    icon_url: str
    id: GodId

    def __init__(self):
        pass

    @staticmethod
    def from_json(obj):
        god = God()

        god.abilities = [Ability.from_json(obj[f'Ability_{idx}']) for idx in range(1, 6)]
        god.stats = GodStats.from_json(obj)
        god.name = obj['Name']
        god.role = GodRole(obj['Roles'].strip().lower())
        god.auto_banned = obj['AutoBanned'] == 'y'
        god.on_free_rotation = obj['OnFreeRotation'] == 'y'
        god.latest_god = obj['latestGod'] == 'y'
        god.title = obj['Title']
        god.lore = obj['Lore']
        god.pantheon = obj['Pantheon']
        god.pros = [GodPro(val.strip().lower()) for val in obj['Pros'].split(',')]
        god.card_url = obj['godCard_URL']
        god.icon_url = obj['godIcon_URL']
        god.id = GodId(obj['id'])

        types = [val.strip() for val in obj['Type'].split(',')]
        for typ in types:
            typ = typ.lower()
            if GodType.has_value(typ):
                god.type = GodType(typ)
            if GodRange.has_value(typ):
                god.range = GodRange(typ)

        return god

    async def get_card_bytes(self) -> io.BytesIO:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.card_url) as res:
                return io.BytesIO(await res.content.read())

    async def get_icon_bytes(self) -> io.BytesIO:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.icon_url) as res:
                return io.BytesIO(await res.content.read())

    def get_stat_at_level(self, stat: ItemAttribute, level: int) -> float:
        try:
            if ItemAttribute == ItemAttribute.BASIC_ATTACK_DAMAGE:
                basic = self.stats.basic_attack.base_damage + \
                    self.stats.basic_attack.per_level * level
                basic_back = self.stats.basic_attack.base_damage_back + \
                    self.stats.basic_attack.per_level_back * level
                return basic + basic_back
            if ItemAttribute == ItemAttribute.MOVEMENT_SPEED:
                level = 8 if level > 8 else level
                speed = self.stats.values[stat].base
                return speed + (speed * 0.3 * level)
            god_stat = self.stats.values[stat]
            return god_stat.base + god_stat.per_level * level
        except KeyError:
            return 0
