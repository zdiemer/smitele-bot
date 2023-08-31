import io
import re
from typing import Dict, List

import aiohttp

from ability import Ability
from god_types import *
from item import ItemAttribute

class _basicAttackProgression:
    damage: List[float]
    swing_time: List[float]
    is_aoe: List[bool]
    has_progression: bool

    NUMBER_REGEX = r'([0-9]?\.?[0-9]+)'

    def __init__(self, progression: str):
        self.damage = []
        self.swing_time = []
        self.is_aoe = []
        self.parse_progression(progression)

    def parse_progression(self, progression: str):
        if 'none' in progression or 'special' in progression:
            self.has_progression = False
        progression = progression.lower()
        split_prog = progression.split('/')

        if len(split_prog) > 1:
            self.has_progression = True
            shared_swing_time = 'and swing time' in progression \
                or 'and speed' in progression \
                or progression.endswith('damage')
            is_swing_time = False
            for idx, p in enumerate(split_prog):
                if p.replace('x', '').isdigit():
                    if not is_swing_time:
                        self.damage.append(float(p.replace('x', '')))
                        self.is_aoe.append(False)
                    else:
                        self.swing_time.append(float(p.replace('x', '')))
                    if shared_swing_time:
                        self.swing_time.append(float(p.replace('x', '')))
                else:
                    matches: List[str] = re.findall(self.NUMBER_REGEX, p)
                    if any(matches):
                        self.is_aoe.append(False)
                        if len(matches) > 1 and idx == len(split_prog) - 1:
                            self.damage.append(float(matches[0].replace('x', '')))
                            self.swing_time.append(float(matches[1].replace('x', '')))
                            is_swing_time = True
                        else:
                            self.damage.append(float(matches[0].replace('x', '')))
            if 'aoe on the final blow' in progression:
                self.is_aoe[-1] = True
            elif 'aoe on first two hits' in progression:
                self.is_aoe[0] = True
                self.is_aoe[1] = True
            elif '3rd attack' in progression or '+aoe' in progression:
                self.is_aoe[2] = True
            elif '4th attack' in progression:
                self.is_aoe[3] = True
            if not any(self.swing_time):
                for _ in self.damage:
                    self.swing_time.append(1)
        self.has_progression = False

class _basicAttackProperties:
    DAMAGE_REGEX = r'(?P<base_damage>\d+\.?\d*) \+ (?P<per_level>\d*\.?\d*)/Lvl \(\+(?P<scaling>\d+)%\ of (Magical|Physical) Power\)'
    __damage: str
    __progression: str
    __damage_scaling: str

    base_damage: float
    per_level: float
    scaling: float
    progression: _basicAttackProgression = None

    # Izanami special case
    base_damage_back: float = 0
    per_level_back: float = 0
    scaling_back: float = 0

    def __init__(self, damage: str, progression: str, damage_scaling: str):
        self.__damage = damage
        self.__progression = progression
        self.__damage_scaling = damage_scaling

        regex = re.compile(self.DAMAGE_REGEX)
        groups = [match.groupdict() for match in regex.finditer(self.__damage)]
        for idx, group in enumerate(groups):
            base_damage = float(group['base_damage'])
            per_level = float(group['per_level'])
            scaling = float(group['scaling']) / 100.0
            if idx == 1: # Izanami special case
                self.base_damage_back = base_damage
                self.per_level_back = per_level
                self.scaling_back = scaling
            else:
                self.base_damage = base_damage
                self.per_level = per_level
                self.scaling = scaling
        if self.__progression is not None:
            self.progression = _basicAttackProgression(self.__progression)

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
        if 'MagicProtection' in obj and 'MagicProtectionPerLevel' in obj:
            stats.values[ItemAttribute.MAGICAL_PROTECTION] = GodStat(
                float(obj['MagicProtection']),
                float(obj['MagicProtectionPerLevel'])
            )
        if 'Mana' in obj and 'ManaPerLevel' in obj:
            stats.values[ItemAttribute.MANA] = GodStat(
                float(obj['Mana']),
                float(obj['ManaPerLevel'])
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
            if stat == ItemAttribute.BASIC_ATTACK_DAMAGE:
                basic = self.stats.basic_attack.base_damage + \
                    self.stats.basic_attack.per_level * (level - 1)
                basic_back = self.stats.basic_attack.base_damage_back + \
                    self.stats.basic_attack.per_level_back * (level - 1)
                total_basic = (basic + basic_back)
                return total_basic + \
                    (0.005 * level * total_basic) if self.role == GodRole.HUNTER else 0
            if stat == ItemAttribute.MOVEMENT_SPEED:
                level = 8 if level > 8 else level
                speed = self.stats.values[stat].base
                return speed + (speed * 0.03 * (level - 1))
            if stat == ItemAttribute.COOLDOWN_REDUCTION and self.role == GodRole.WARRIOR:
                return 0.05 + (0.0025 * level)
            if stat == ItemAttribute.PHYSICAL_PENETRATION and self.role == GodRole.ASSASSIN:
                return 5 + (0.25 * level)
            if stat == ItemAttribute.MAGICAL_POWER and self.role == GodRole.MAGE:
                return 20 + level
            if stat == ItemAttribute.CROWD_CONTROL_REDUCTION and self.role == GodRole.GUARDIAN:
                return 0.10 + (0.005 * level)
            if self.id in (GodId.CU_CHULAINN, GodId.YEMOJA):
                if stat == ItemAttribute.MANA:
                    return 0
                if stat == ItemAttribute.MP5:
                    return 0
                if stat == ItemAttribute.HEALTH:
                    god_stat = self.stats.values[stat]
                    mana_stat = self.stats.values[ItemAttribute.MANA]
                    return (god_stat.base + god_stat.per_level * (level - 1)) + \
                        (mana_stat.base + mana_stat.per_level * (level - 1))
                if stat == ItemAttribute.MP5:
                    god_stat = self.stats.values[stat]
                    mana_stat = self.stats.values[ItemAttribute.MP5]
                    return (god_stat.base + god_stat.per_level * (level - 1)) + \
                        (mana_stat.base + mana_stat.per_level * (level - 1))
            god_stat = self.stats.values[stat]
            return god_stat.base + god_stat.per_level * (level - 1)
        except KeyError:
            return 0
