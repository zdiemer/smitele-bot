import io
import re
from enum import Enum
from typing import List

import aiohttp

from ability import Ability

class _basicAttackProperties:
    DAMAGE_REGEX = r'(?P<base_damage>\d+\.?\d*) \+ (?P<per_level>\d*\.?\d*)/Lvl \(\+(?P<scaling>\d+)%\ of (Magical|Physical) Power\)'
    __damage: str
    progression: str
    damage_scaling: str

    base_damage: float
    per_level: float
    scaling: float

    # Izanami special case
    base_damage_back: float
    per_level_back: float
    scaling_back: float

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

class GodStats:
    attack_speed: float
    attack_speed_per_level: float
    hp5: float
    hp5_per_level: float
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
        if 'AttackSpeed' in obj:
            stats.attack_speed = float(obj['AttackSpeed'])
        if 'AttackSpeedPerLevel' in obj:
            stats.attack_speed_per_level = float(obj['AttackSpeedPerLevel'])
        if 'HealthPerFive' in obj:
            stats.hp5 = float(obj['HealthPerFive'])
        if 'HP5PerLevel' in obj:
            stats.hp5_per_level = float(obj['HP5PerLevel'])
        if 'Health' in obj:
            stats.health = float(obj['Health'])
        if 'HealthPerLevel' in obj:
            stats.health_per_level = float(obj['HealthPerLevel'])
        if 'ManaPerFive' in obj:
            stats.mp5 = float(obj['ManaPerFive'])
        if 'AttackSpeedPerLevel' in obj:
            stats.mp5_per_level = float(obj['AttackSpeedPerLevel'])
        if 'MagicProtection' in obj:
            stats.magic_protection = float(obj['MagicProtection'])
        if 'MagicProtectionPerLevel' in obj:
            stats.magic_protection_per_level = float(obj['MagicProtectionPerLevel'])
        if 'MagicalPower' in obj:
            stats.magical_power = float(obj['MagicalPower'])
        if 'MagicalPowerPerLevel' in obj:
            stats.magical_power_per_level = float(obj['MagicalPowerPerLevel'])
        if 'Mana' in obj:
            stats.mana = float(obj['Mana'])
        if 'ManaPerLevel' in obj:
            stats.mana_per_level = float(obj['ManaPerLevel'])
        if 'PhysicalPower' in obj:
            stats.physical_power = float(obj['PhysicalPower'])
        if 'PhysicalPowerPerLevel' in obj:
            stats.physical_power_per_level = float(obj['PhysicalPowerPerLevel'])
        if 'PhysicalProtection' in obj:
            stats.physical_protection = float(obj['PhysicalProtection'])
        if 'PhysicalProtectionPerLevel' in obj:
            stats.physical_protection_per_level = float(obj['PhysicalProtectionPerLevel'])
        if 'Speed' in obj:
            stats.speed = float(obj['Speed'])

        stats.basic_attack = _basicAttackProperties.from_json(obj['basicAttack'])
        return stats

class GodId(Enum):
    ACHILLES = 3492
    AGNI = 1737
    AH_MUZEN_CAB = 1956
    AH_PUCH = 2056
    AMATERASU = 2110
    ANHUR = 1773
    ANUBIS = 1668
    AO_KUANG = 2034
    APHRODITE = 1898
    APOLLO = 1899
    ARACHNE = 1699
    ARES = 1782
    ARTEMIS = 1748
    ARTIO = 3336
    ATHENA = 1919
    ATLAS = 4034
    AWILIX = 2037
    BABA_YAGA = 3925
    BACCHUS = 1809
    BAKASURA = 1755
    BARON_SAMEDI = 3518
    BASTET = 1678
    BELLONA = 2047
    CABRAKAN = 2008
    CAMAZOTZ = 2189
    CERBERUS = 3419
    CERNUNNOS = 2268
    CHAAC = 1966
    CHANGE = 1921
    CHARYBDIS = 4010
    CHERNOBOG = 3509
    CHIRON = 2075
    CHRONOS = 1920
    CLIODHNA = 4017
    CTHULHU = 3945
    CU_CHULAINN = 2319
    CUPID = 1778
    DA_JI = 2270
    DANZABUROU = 3984
    DISCORDIA = 3377
    ERLANG_SHEN = 2138
    ESET = 1918
    FAFNIR = 2136
    FENRIR = 1843
    FREYA = 1784
    GANESHA = 2269
    GEB = 1978
    GILGAMESH = 3997
    GUAN_YU = 1763
    HACHIMAN = 3344
    HADES = 1676
    HE_BO = 1674
    HEIMDALLR = 3812
    HEL = 1718
    HERA = 3558
    HERCULES = 1848
    HORUS = 3611
    HOU_YI = 2040
    HUN_BATZ = 1673
    ISHTAR = 4137
    IZANAMI = 2179
    JANUS = 1999
    JING_WEI = 2122
    JORMUNGANDR = 3585
    KALI = 1649
    KHEPRI = 2066
    KING_ARTHUR = 3565
    KUKULKAN = 1677
    KUMBHAKARNA = 1993
    KUZENBO = 2260
    LANCELOT = 4075
    LOKI = 1797
    MAUI = 4183
    MEDUSA = 2051
    MERCURY = 1941
    MERLIN = 3566
    MORGAN_LE_FAY = 4006
    MULAN = 3881
    NE_ZHA = 1915
    NEITH = 1872
    NEMESIS = 1980
    NIKE = 2214
    NOX = 2036
    NU_WA = 1958
    ODIN = 1669
    OLORUN = 3664
    OSIRIS = 2000
    PELE = 3543
    PERSEPHONE = 3705
    POSEIDON = 1881
    RA = 1698
    RAIJIN = 2113
    RAMA = 2002
    RATATOSKR = 2063
    RAVANA = 2065
    SCYLLA = 1988
    SERQET = 2005
    SET = 3612
    SHIVA = 4039
    SKADI = 2107
    SOBEK = 1747
    SOL = 2074
    SUN_WUKONG = 1944
    SUSANO = 2123
    SYLVANUS = 2030
    TERRA = 2147
    THANATOS = 1943
    THE_MORRIGAN = 2226
    THOR = 1779
    THOTH = 2203
    TIAMAT = 3990
    TSUKUYOMI = 3954
    TYR = 1924
    ULLR = 1991
    VAMANA = 1723
    VULCAN = 1869
    XBALANQUE = 1864
    XING_TIAN = 2072
    YEMOJA = 3811
    YMIR = 1670
    YU_HUANG = 4060
    ZEUS = 1672
    ZHONG_KUI = 1926

class GodRole(Enum):
    ASSASSIN = 'assassin'
    GUARDIAN = 'guardian'
    HUNTER = 'hunter'
    MAGE = 'mage'
    WARRIOR = 'warrior'

class GodType(Enum):
    MAGICAL = 'magical'
    PHYSICAL = 'physical'

    @classmethod
    def has_value(self, value):
        # pylint: disable=no-member
        return value in self._value2member_map_

class GodRange(Enum):
    MELEE = 'melee'
    RANGED = 'ranged'

    @classmethod
    def has_value(self, value):
        # pylint: disable=no-member
        return value in self._value2member_map_

class GodPro(Enum):
    GREAT_JUNGLER = 'great jungler'
    HIGH_AREA_DAMAGE = 'high area damage'
    HIGH_ATTACK_SPEED = 'high attack speed'
    HIGH_CROWD_CONTROL = 'high crowd control'
    HIGH_DEFENSE = 'high defense'
    HIGH_MOBILITY = 'high mobility'
    HIGH_MOVEMENT_SPEED = 'high movement speed'
    HIGH_SINGLE_TARGET_DAMAGE = 'high single target damage'
    HIGH_SUSTAIN = 'high sustain'
    MEDIUM_AREA_DAMAGE = 'medium area damage'
    MEDIUM_CROWD_CONTROL = 'medium crowd control'
    PUSHER = 'pusher'

class God(object):
    abilities: List[Ability]
    stats: GodStats
    name: str
    roles: List[GodRole]
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
        god.roles = [GodRole(val.strip().lower()) for val in obj['Roles'].split(',')]
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
