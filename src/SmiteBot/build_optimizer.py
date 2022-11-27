import asyncio
import itertools
import random
from enum import Enum
from typing import Dict, FrozenSet, List, Set, Tuple, Union

from god import God
from god_types import GodId, GodRole, GodType
from item import Item, ItemAttribute, ItemProperty
from passive_parser import PassiveAttribute

class BuildArchetype(Enum):
    # Assassin Archetypes
    ABILITY_BASED_ASSASSIN = 1
    AUTO_ATTACK_ASSASSIN = 2
    AUTO_ATTACK_WITH_CRIT_ASSASSIN = 25
    SUPPORT_ASSASSIN = 4
    SOLO_ASSASSIN = 5
    MID_ASSASSIN = 6

    # Guardian Archetypes
    SUPPORT_GUARDIAN = 7
    SOLO_GUARDIAN = 8
    MID_GUARDIAN = 9
    HEALER_GUARDIAN = 23

    # Hunter Archetypes
    CARRY_HUNTER = 10
    ABILITY_BASED_HUNTER = 11
    ATTACK_SPEED_STIM_HUNTER = 23

    # Mage Archetypes
    MID_MAGE = 12
    JUNGLE_MAGE = 13
    AUTO_ATTACK_MAGE = 14
    HEALER_MAGE = 15
    SOLO_MAGE = 16
    SUPPORT_MAGE = 17
    LIFESTEAL_MID_MAGE = 24

    # Warrior Archetypes
    ABILITY_BASED_WARRIOR = 18
    AUTO_ATTACK_WARRIOR = 19
    SUPPORT_WARRIOR = 20
    JUNGLE_WARRIOR = 21
    HEALER_WARRIOR = 22

    @staticmethod
    def default_archetype(role: GodRole):
        if role == GodRole.ASSASSIN:
            return BuildArchetype.ABILITY_BASED_ASSASSIN
        if role == GodRole.GUARDIAN:
            return BuildArchetype.SUPPORT_GUARDIAN
        if role == GodRole.HUNTER:
            return BuildArchetype.CARRY_HUNTER
        if role == GodRole.MAGE:
            return BuildArchetype.MID_MAGE
        if role == GodRole.WARRIOR:
            return BuildArchetype.ABILITY_BASED_WARRIOR

class BuildOptimizer:
    ARCHETYPE_PREFERRED_STARTER: Dict[BuildArchetype, Set[int]] = {
        BuildArchetype.ABILITY_BASED_ASSASSIN: {
            19500, # Manikin Scepter
            19502, # Bumba's Dagger
            19694, # Eye of the Jungle
        },
        BuildArchetype.AUTO_ATTACK_ASSASSIN: {
            19500, # Manikin Scepter
            19502, # Bumba's Dagger
            19694, # Eye of the Jungle
        },
        BuildArchetype.AUTO_ATTACK_WITH_CRIT_ASSASSIN: {
            19500, # Manikin Scepter
            19502, # Bumba's Dagger
            19694, # Eye of the Jungle
        },
        BuildArchetype.SOLO_ASSASSIN: {
            19490, # Bluestone Pendant
            19496, # Warrior's Axe
            19640, # Tainted Steel
            19751, # Warding Sigil
        },
        BuildArchetype.SUPPORT_GUARDIAN: {
            19609, # Sentinel's Gift
            19634, # Benevolence
            20698, # War Flag
        },
        BuildArchetype.HEALER_GUARDIAN: {
            19609, # Sentinel's Gift
            19634, # Benevolence
            20698, # War Flag
        },
        BuildArchetype.SOLO_GUARDIAN: {
            19677, # Conduit Gem
            19496, # Warrior's Axe
            19510, # Vampiric Shroud
            19508, # Sands of Time
        },
        BuildArchetype.CARRY_HUNTER: {
            19492, # Death's Toll
            19494, # Gilded Arrow
            19672, # Leather Cowl
        },
        BuildArchetype.ABILITY_BASED_HUNTER: {
            19500, # Manikin Scepter
            19490, # Bluestone Pendant
        },
        BuildArchetype.MID_MAGE: {
            19508, # Sands of Time
            19677, # Conduit Gem
        },
        BuildArchetype.LIFESTEAL_MID_MAGE: {
            19510, # Vampiric Shroud
        },
        BuildArchetype.JUNGLE_MAGE: {
            19500, # Manikin Scepter
            19502, # Bumba's Dagger
            19694, # Eye of the Jungle
        },
        BuildArchetype.HEALER_MAGE: {
            19508, # Sands of Time
            19677, # Conduit Gem
        },
        BuildArchetype.AUTO_ATTACK_MAGE: {
            19508, # Sands of Time
            19677, # Conduit Gem
            19500, # Manikin Scepter
        },
        BuildArchetype.SOLO_MAGE: {
            19508, # Sands of Time
            19677, # Conduit Gem
            19510, # Vampiric Shroud
            19496, # Warrior's Axe
            19640, # Tainted Steel
        },
        BuildArchetype.SUPPORT_MAGE: {
            19609, # Sentinel's Gift
            19634, # Benevolence
            20698, # War Flag
            23048, # Protector's Mask
        },
        BuildArchetype.ABILITY_BASED_WARRIOR: {
            19490, # Bluestone Pendant
            19492, # Death's Toll
            19496, # Warrior's Axe
            19640, # Tainted Steel
            19751, # Warding Sigil
        },
        BuildArchetype.HEALER_WARRIOR: {
            19490, # Bluestone Pendant
            19492, # Death's Toll
            19496, # Warrior's Axe
            19640, # Tainted Steel
            19751, # Warding Sigil
        },
        BuildArchetype.AUTO_ATTACK_WARRIOR: {
            19492, # Death's Toll
        },
        BuildArchetype.JUNGLE_WARRIOR: {
            19500, # Manikin Scepter
            19502, # Bumba's Dagger
            19694, # Eye of the Jungle
        },
        BuildArchetype.SUPPORT_WARRIOR: {
            19609, # Sentinel's Gift
            19634, # Benevolence
            20698, # War Flag
        },
    }

    # Used for processing input to determine what stats to optimize for
    COLLOQUIAL_MAPPINGS: Dict[str, Set[ItemAttribute]] = {
        'crit': set(
            [ItemAttribute.CRITICAL_STRIKE_CHANCE,
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE]),
        'ccr': set([ItemAttribute.CROWD_CONTROL_REDUCTION]),
        'cdr': set([ItemAttribute.COOLDOWN_REDUCTION]),
        'power': set([ItemAttribute.MAGICAL_POWER, ItemAttribute.PHYSICAL_POWER]),
        'speed': set([ItemAttribute.MOVEMENT_SPEED]),
        'lifesteal': set([ItemAttribute.MAGICAL_LIFESTEAL, ItemAttribute.PHYSICAL_LIFESTEAL]),
        'protection': set([
            ItemAttribute.MAGICAL_PROTECTION,
            ItemAttribute.PHYSICAL_PROTECTION,
            ItemAttribute.PROTECTIONS]),
        'prots': set([
            ItemAttribute.MAGICAL_PROTECTION,
            ItemAttribute.PHYSICAL_PROTECTION,
            ItemAttribute.PROTECTIONS]),
        'prot': set([
            ItemAttribute.MAGICAL_PROTECTION,
            ItemAttribute.PHYSICAL_PROTECTION,
            ItemAttribute.PROTECTIONS]),
        'pen': set([
            ItemAttribute.MAGICAL_PENETRATION,
            ItemAttribute.PENETRATION,
            ItemAttribute.PHYSICAL_PENETRATION]),
        'penetration': set([
            ItemAttribute.MAGICAL_PENETRATION,
            ItemAttribute.PENETRATION,
            ItemAttribute.PHYSICAL_PENETRATION]),
    }

    FLAT_ITEM_ATTRIBUTE_CAPS: Dict[ItemAttribute, float] = {
        ItemAttribute.ATTACK_SPEED: 2.5,
        ItemAttribute.BASIC_ATTACK_DAMAGE: 10000,
        ItemAttribute.MAGICAL_PENETRATION: 50,
        ItemAttribute.PHYSICAL_PENETRATION: 50,
        ItemAttribute.MAGICAL_PROTECTION: 325,
        ItemAttribute.PHYSICAL_PROTECTION: 325,
        ItemAttribute.HEALTH: 5500,
        ItemAttribute.HP5: 100,
        ItemAttribute.MP5: 100,
        ItemAttribute.MOVEMENT_SPEED: 1000,
        ItemAttribute.MANA: 4000,
        ItemAttribute.MAGICAL_POWER: 900,
        ItemAttribute.PHYSICAL_POWER: 400,
        # Only one item gives Damage Reduction and it's +5
        ItemAttribute.DAMAGE_REDUCTION: 5,
    }

    GOD_ID_ARCHETYPE_MAPPINGS: Dict[GodId, BuildArchetype] = {
        GodId.ANUBIS: BuildArchetype.LIFESTEAL_MID_MAGE,
        GodId.AO_KUANG: BuildArchetype.JUNGLE_MAGE,
        GodId.APHRODITE: BuildArchetype.HEALER_MAGE,
        GodId.ARACHNE: BuildArchetype.AUTO_ATTACK_ASSASSIN,
        GodId.ARTIO: BuildArchetype.SOLO_GUARDIAN,
        GodId.BAKASURA: BuildArchetype.AUTO_ATTACK_ASSASSIN,
        GodId.BARON_SAMEDI: BuildArchetype.HEALER_MAGE,
        GodId.BELLONA: BuildArchetype.AUTO_ATTACK_WARRIOR,
        GodId.CAMAZOTZ: BuildArchetype.SOLO_ASSASSIN,
        GodId.CERBERUS: BuildArchetype.SOLO_GUARDIAN,
        GodId.CHANGE: BuildArchetype.HEALER_MAGE,
        GodId.CHIRON: BuildArchetype.ABILITY_BASED_HUNTER,
        GodId.CHRONOS: BuildArchetype.AUTO_ATTACK_MAGE,
        GodId.CTHULHU: BuildArchetype.SOLO_GUARDIAN,
        GodId.ERLANG_SHEN: BuildArchetype.JUNGLE_WARRIOR,
        GodId.FREYA: BuildArchetype.AUTO_ATTACK_MAGE,
        GodId.GILGAMESH: BuildArchetype.AUTO_ATTACK_WARRIOR,
        GodId.GUAN_YU: BuildArchetype.HEALER_WARRIOR,
        GodId.HADES: BuildArchetype.SOLO_MAGE,
        GodId.HEL: BuildArchetype.HEALER_MAGE,
        GodId.HORUS: BuildArchetype.SUPPORT_WARRIOR,
        GodId.JORMUNGANDR: BuildArchetype.SOLO_GUARDIAN,
        GodId.KALI: BuildArchetype.AUTO_ATTACK_ASSASSIN,
        GodId.KUZENBO: BuildArchetype.SOLO_GUARDIAN,
        GodId.MERCURY: BuildArchetype.AUTO_ATTACK_WITH_CRIT_ASSASSIN,
        GodId.NEITH: BuildArchetype.ABILITY_BASED_HUNTER,
        GodId.NOX: BuildArchetype.SUPPORT_MAGE,
        GodId.OLORUN: BuildArchetype.AUTO_ATTACK_MAGE,
        GodId.OSIRIS: BuildArchetype.AUTO_ATTACK_WARRIOR,
        GodId.RA: BuildArchetype.HEALER_MAGE,
        GodId.SKADI: BuildArchetype.ABILITY_BASED_HUNTER,
        GodId.SOL: BuildArchetype.AUTO_ATTACK_MAGE,
        GodId.SYLVANUS: BuildArchetype.HEALER_GUARDIAN,
        GodId.TERRA: BuildArchetype.HEALER_GUARDIAN,
        GodId.ULLR: BuildArchetype.ABILITY_BASED_HUNTER,
        GodId.XING_TIAN: BuildArchetype.SOLO_GUARDIAN,
        GodId.YEMOJA: BuildArchetype.HEALER_GUARDIAN,
        GodId.ZHONG_KUI: BuildArchetype.SOLO_MAGE,
    }

    GOD_TYPE_MAPPINGS: Dict[GodType, Set[ItemAttribute]] = {
        GodType.MAGICAL: set([
            ItemAttribute.MAGICAL_LIFESTEAL,
            ItemAttribute.MAGICAL_PENETRATION,
            ItemAttribute.MAGICAL_POWER,
        ]),
        GodType.PHYSICAL: set([
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE,
            ItemAttribute.PHYSICAL_LIFESTEAL,
            ItemAttribute.PHYSICAL_PENETRATION,
            ItemAttribute.PHYSICAL_POWER,
        ])
    }

    MAGIC_ACORN_ID = 18703

    PERCENT_ITEM_ATTRIBUTE_CAPS: Dict[ItemAttribute, float] = {
        ItemAttribute.MAGICAL_LIFESTEAL: 0.65,
        ItemAttribute.PHYSICAL_LIFESTEAL: 1,
        ItemAttribute.MAGICAL_PENETRATION: 0.40,
        ItemAttribute.PHYSICAL_PENETRATION: 0.40,
        ItemAttribute.CRITICAL_STRIKE_CHANCE: 1,
        ItemAttribute.CROWD_CONTROL_REDUCTION: 0.40,
        ItemAttribute.COOLDOWN_REDUCTION: 0.40,
    }

    __archetype_stat_targets: Dict[BuildArchetype, Dict[ItemAttribute, float]]
    __archetype_weight_mappings: Dict[BuildArchetype, Dict[ItemAttribute, float]]

    god: God
    valid_items: List[Item]
    __all_items: Dict[int, Item]
    __item_scores: Dict[int, float]
    __stat: Union[ItemAttribute, Set[ItemAttribute]] = None
    __level_20_stats: Dict[ItemAttribute, float]
    __current_archetype: BuildArchetype

    def __init__(self, god: God, valid_items: List[Item],
            all_items: Dict[int, Item], stat: str = None):
        self.god = god
        self.valid_items = valid_items
        self.__all_items = all_items
        if stat is not None:
            self.__init_stat(stat.lower())
        self.__init_archetype_weight_mappings()
        self.__init_level_20_stats()
        self.__init_archetype_stat_targets()
        archetype = None
        if god.id in self.GOD_ID_ARCHETYPE_MAPPINGS:
            archetype = self.GOD_ID_ARCHETYPE_MAPPINGS[god.id]
        self.__current_archetype = \
            BuildArchetype.default_archetype(self.god.role) \
                if archetype is None else archetype

    def __init_stat(self, stat_name: str):
        try:
            self.__stat = ItemAttribute(stat_name)
        except ValueError:
            if stat_name in self.COLLOQUIAL_MAPPINGS:
                self.__stat = self.COLLOQUIAL_MAPPINGS[stat_name]
            else:
                raise
        if isinstance(self.__stat, ItemAttribute) and \
                self.__stat.god_type is not None and self.__stat.god_type != self.god.type:
            raise ValueError(self.__stat.display_name, ' is not a valid stat for ', self.god.name)

    def __init_archetype_weight_mappings(self):
        defaults = {
            BuildArchetype.ABILITY_BASED_ASSASSIN: {
                ItemAttribute.ATTACK_SPEED: 0.5,
                ItemAttribute.BASIC_ATTACK_DAMAGE: 1,
                ItemAttribute.COOLDOWN_REDUCTION: 5,
                ItemAttribute.CRITICAL_STRIKE_CHANCE: -1,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 1,
                ItemAttribute.DAMAGE_REDUCTION: -1,
                ItemAttribute.HP5: 1,
                ItemAttribute.HEALTH: 1,
                ItemAttribute.MP5: 1,
                ItemAttribute.MAGICAL_LIFESTEAL: None,
                ItemAttribute.MAGICAL_PENETRATION: None,
                ItemAttribute.MAGICAL_POWER: None,
                ItemAttribute.MAGICAL_PROTECTION: 1,
                ItemAttribute.MANA: 1,
                ItemAttribute.MOVEMENT_SPEED: 1,
                ItemAttribute.PHYSICAL_LIFESTEAL: -1,
                ItemAttribute.PHYSICAL_PENETRATION: (5, 5),
                ItemAttribute.PHYSICAL_POWER: 5,
                ItemAttribute.PHYSICAL_PROTECTION: 1,
            },
            BuildArchetype.SUPPORT_GUARDIAN: {
                ItemAttribute.ATTACK_SPEED: -1,
                ItemAttribute.BASIC_ATTACK_DAMAGE: -1,
                ItemAttribute.COOLDOWN_REDUCTION: 5,
                ItemAttribute.CRITICAL_STRIKE_CHANCE: None,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 5,
                ItemAttribute.DAMAGE_REDUCTION: 1,
                ItemAttribute.HP5: 1,
                ItemAttribute.HEALTH: 5,
                ItemAttribute.MP5: 1,
                ItemAttribute.MAGICAL_LIFESTEAL: -1,
                ItemAttribute.MAGICAL_PENETRATION: (-1, -1),
                ItemAttribute.MAGICAL_POWER: -1,
                ItemAttribute.MAGICAL_PROTECTION: 5,
                ItemAttribute.MANA: 1,
                ItemAttribute.MOVEMENT_SPEED: 1,
                ItemAttribute.PHYSICAL_LIFESTEAL: None,
                ItemAttribute.PHYSICAL_PENETRATION: None,
                ItemAttribute.PHYSICAL_POWER: None,
                ItemAttribute.PHYSICAL_PROTECTION: 5,
            },
            BuildArchetype.CARRY_HUNTER: {
                ItemAttribute.ATTACK_SPEED: 5,
                ItemAttribute.BASIC_ATTACK_DAMAGE: 1,
                ItemAttribute.COOLDOWN_REDUCTION: 1,
                ItemAttribute.CRITICAL_STRIKE_CHANCE: 5,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 0.1,
                ItemAttribute.DAMAGE_REDUCTION: 0.1,
                ItemAttribute.HP5: 0.5,
                ItemAttribute.HEALTH: 0.5,
                ItemAttribute.MP5: 0.5,
                ItemAttribute.MAGICAL_LIFESTEAL: None,
                ItemAttribute.MAGICAL_PENETRATION: None,
                ItemAttribute.MAGICAL_POWER: None,
                ItemAttribute.MAGICAL_PROTECTION: 0.1,
                ItemAttribute.MANA: 0.5,
                ItemAttribute.MOVEMENT_SPEED: 0.5,
                ItemAttribute.PHYSICAL_LIFESTEAL: 5,
                ItemAttribute.PHYSICAL_PENETRATION: (5, 5),
                ItemAttribute.PHYSICAL_POWER: 5,
                ItemAttribute.PHYSICAL_PROTECTION: 0.1,
            },
            BuildArchetype.MID_MAGE: {
                ItemAttribute.ATTACK_SPEED: 0.1,
                ItemAttribute.BASIC_ATTACK_DAMAGE: 0.1,
                ItemAttribute.COOLDOWN_REDUCTION: 5,
                ItemAttribute.CRITICAL_STRIKE_CHANCE: None,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 0.1,
                ItemAttribute.DAMAGE_REDUCTION: 0.1,
                ItemAttribute.HP5: 1,
                ItemAttribute.HEALTH: 1,
                ItemAttribute.MP5: 1,
                ItemAttribute.MAGICAL_LIFESTEAL: 1,
                ItemAttribute.MAGICAL_PENETRATION: (5, 5),
                ItemAttribute.MAGICAL_POWER: 5,
                ItemAttribute.MAGICAL_PROTECTION: 0.1,
                ItemAttribute.MANA: 1,
                ItemAttribute.MOVEMENT_SPEED: 0.5,
                ItemAttribute.PHYSICAL_LIFESTEAL: None,
                ItemAttribute.PHYSICAL_PENETRATION: None,
                ItemAttribute.PHYSICAL_POWER: None,
                ItemAttribute.PHYSICAL_PROTECTION: 0.1,
            },
            BuildArchetype.ABILITY_BASED_WARRIOR: {
                ItemAttribute.ATTACK_SPEED: 0.5,
                ItemAttribute.BASIC_ATTACK_DAMAGE: 0.1,
                ItemAttribute.COOLDOWN_REDUCTION: 3,
                ItemAttribute.CRITICAL_STRIKE_CHANCE: 0.1,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 2,
                ItemAttribute.DAMAGE_REDUCTION: 1,
                ItemAttribute.HP5: 1,
                ItemAttribute.HEALTH: 5,
                ItemAttribute.MP5: 1,
                ItemAttribute.MAGICAL_LIFESTEAL: None,
                ItemAttribute.MAGICAL_PENETRATION: None,
                ItemAttribute.MAGICAL_POWER: None,
                ItemAttribute.MAGICAL_PROTECTION: 5,
                ItemAttribute.MANA: 1,
                ItemAttribute.MOVEMENT_SPEED: 1,
                ItemAttribute.PHYSICAL_LIFESTEAL: 1,
                ItemAttribute.PHYSICAL_PENETRATION: (1, 1),
                ItemAttribute.PHYSICAL_POWER: 5,
                ItemAttribute.PHYSICAL_PROTECTION: 5,
            },
        }

        # Additional Settings for Assassin Archetypes
        solo_assassin = defaults[BuildArchetype.ABILITY_BASED_ASSASSIN].copy()
        auto_assassin = solo_assassin.copy()
        auto_crit_assassin = auto_assassin.copy()
        auto_assassin[ItemAttribute.ATTACK_SPEED] = 5
        defaults[BuildArchetype.AUTO_ATTACK_ASSASSIN] = auto_assassin
        auto_crit_assassin[ItemAttribute.CRITICAL_STRIKE_CHANCE] = 8
        auto_crit_assassin[ItemAttribute.MOVEMENT_SPEED] = 5
        auto_crit_assassin[ItemAttribute.PHYSICAL_LIFESTEAL] = 1
        defaults[BuildArchetype.AUTO_ATTACK_WITH_CRIT_ASSASSIN] = auto_crit_assassin
        solo_assassin[ItemAttribute.PHYSICAL_PROTECTION] = 3
        solo_assassin[ItemAttribute.MAGICAL_PROTECTION] = 3
        solo_assassin[ItemAttribute.HEALTH] = 3
        solo_assassin[ItemAttribute.MANA] = 3
        solo_assassin[ItemAttribute.CROWD_CONTROL_REDUCTION] = 2
        solo_assassin[ItemAttribute.PHYSICAL_POWER] = 2
        solo_assassin[ItemAttribute.PHYSICAL_PENETRATION] = (1, 1)
        defaults[BuildArchetype.SOLO_ASSASSIN] = solo_assassin

        # Additional Settings for Guardian Archetypes
        solo_guardian = defaults[BuildArchetype.SUPPORT_GUARDIAN].copy()
        solo_guardian[ItemAttribute.MAGICAL_POWER] = 5
        defaults[BuildArchetype.SOLO_GUARDIAN] = solo_guardian
        defaults[BuildArchetype.HEALER_GUARDIAN] = \
            defaults[BuildArchetype.SUPPORT_GUARDIAN].copy()

        # Additional Settings for Hunter Archetypes
        ability_hunter = defaults[BuildArchetype.CARRY_HUNTER].copy()
        ability_hunter[ItemAttribute.ATTACK_SPEED] = -0.5
        ability_hunter[ItemAttribute.CRITICAL_STRIKE_CHANCE] = -1
        ability_hunter[ItemAttribute.PHYSICAL_LIFESTEAL] = 1
        ability_hunter[ItemAttribute.MANA] = 2
        ability_hunter[ItemAttribute.COOLDOWN_REDUCTION] = 1
        defaults[BuildArchetype.ABILITY_BASED_HUNTER] = ability_hunter

        # Additional Settings for Mage Archetypes
        lifesteal_mage = defaults[BuildArchetype.MID_MAGE].copy()
        healer_mage = lifesteal_mage.copy()
        solo_mage = lifesteal_mage.copy()
        defaults[BuildArchetype.HEALER_MAGE] = healer_mage
        lifesteal_mage[ItemAttribute.MAGICAL_LIFESTEAL] = 5
        defaults[BuildArchetype.LIFESTEAL_MID_MAGE] = lifesteal_mage
        defaults[BuildArchetype.JUNGLE_MAGE] = lifesteal_mage
        auto_mage = lifesteal_mage.copy()
        auto_mage[ItemAttribute.ATTACK_SPEED] = 5
        defaults[BuildArchetype.AUTO_ATTACK_MAGE] = auto_mage
        solo_mage[ItemAttribute.HEALTH] = 5
        solo_mage[ItemAttribute.PHYSICAL_PROTECTION] = 5
        solo_mage[ItemAttribute.MAGICAL_PROTECTION] = 5
        solo_mage[ItemAttribute.MP5] = 1
        defaults[BuildArchetype.SOLO_MAGE] = solo_mage
        defaults[BuildArchetype.SUPPORT_MAGE] = \
            defaults[BuildArchetype.SUPPORT_GUARDIAN].copy()

        # Additional Settings for Warrior Archetypes
        auto_warrior = defaults[BuildArchetype.ABILITY_BASED_WARRIOR].copy()
        auto_warrior[ItemAttribute.ATTACK_SPEED] = 5
        defaults[BuildArchetype.AUTO_ATTACK_WARRIOR] = auto_warrior
        jungle_warrior = defaults[BuildArchetype.ABILITY_BASED_ASSASSIN].copy()
        jungle_warrior[ItemAttribute.ATTACK_SPEED] = 2
        jungle_warrior[ItemAttribute.PHYSICAL_PROTECTION] = 1
        jungle_warrior[ItemAttribute.MAGICAL_PROTECTION] = 1
        jungle_warrior[ItemAttribute.HEALTH] = 1
        defaults[BuildArchetype.JUNGLE_WARRIOR] = jungle_warrior
        defaults[BuildArchetype.HEALER_WARRIOR] = \
            defaults[BuildArchetype.ABILITY_BASED_WARRIOR].copy()
        support_warrior = defaults[BuildArchetype.SUPPORT_GUARDIAN].copy()
        support_warrior[ItemAttribute.PHYSICAL_LIFESTEAL] = -1
        support_warrior[ItemAttribute.PHYSICAL_PENETRATION] = (0.1, 0.1)
        support_warrior[ItemAttribute.PHYSICAL_POWER] = 0.1
        support_warrior[ItemAttribute.CRITICAL_STRIKE_CHANCE] = -1
        support_warrior[ItemAttribute.MAGICAL_LIFESTEAL] = None
        support_warrior[ItemAttribute.MAGICAL_PENETRATION] = None
        support_warrior[ItemAttribute.MAGICAL_POWER] = None
        defaults[BuildArchetype.SUPPORT_WARRIOR] = support_warrior

        self.__archetype_weight_mappings = defaults

    def __init_archetype_stat_targets(self):
        self.__archetype_stat_targets = {
            BuildArchetype.ABILITY_BASED_ASSASSIN: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.30,
                ItemAttribute.HEALTH: 200,
                ItemAttribute.MANA: 200,
                ItemAttribute.MOVEMENT_SPEED: 0.07,
                ItemAttribute.PHYSICAL_LIFESTEAL: 0.05,
                ItemAttribute.PHYSICAL_PENETRATION: (10, 0.40),
                ItemAttribute.PHYSICAL_POWER: 300,
            },
            BuildArchetype.AUTO_ATTACK_ASSASSIN: {
                ItemAttribute.ATTACK_SPEED: 0.70,
                ItemAttribute.MOVEMENT_SPEED: 0.20,
                ItemAttribute.PHYSICAL_LIFESTEAL: 0.10,
                ItemAttribute.PHYSICAL_PENETRATION: (0, 0.20),
                ItemAttribute.PHYSICAL_POWER: 225,
            },
            BuildArchetype.AUTO_ATTACK_WITH_CRIT_ASSASSIN: {
                ItemAttribute.ATTACK_SPEED: 0.40,
                ItemAttribute.CRITICAL_STRIKE_CHANCE: 0.75,
                ItemAttribute.MOVEMENT_SPEED: 0.21,
                ItemAttribute.PHYSICAL_PENETRATION: (0, 0.20),
                ItemAttribute.PHYSICAL_POWER: 195,
                ItemAttribute.PHYSICAL_PROTECTION: 30,
            },
            BuildArchetype.SOLO_ASSASSIN: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.30,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 0.20,
                ItemAttribute.HEALTH: 300,
                ItemAttribute.MAGICAL_PROTECTION: 100,
                ItemAttribute.MANA: 1000,
                ItemAttribute.MP5: 10,
                ItemAttribute.PHYSICAL_PENETRATION: (15, 0),
                ItemAttribute.PHYSICAL_POWER: 150,
                ItemAttribute.PHYSICAL_PROTECTION: 130,
            },
            BuildArchetype.SUPPORT_GUARDIAN: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.20,
                ItemAttribute.HEALTH: 1350,
                ItemAttribute.HP5: 60,
                ItemAttribute.MAGICAL_PROTECTION: 195,
                ItemAttribute.MP5: 15,
                ItemAttribute.PHYSICAL_PROTECTION: 180,
            },
            BuildArchetype.SOLO_GUARDIAN: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.20,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 0.20,
                ItemAttribute.HEALTH: 800,
                ItemAttribute.MAGICAL_POWER: 200,
                ItemAttribute.MAGICAL_PROTECTION: 130,
                ItemAttribute.MANA: 400,
                ItemAttribute.MP5: 60,
                ItemAttribute.PHYSICAL_PROTECTION: 140,
            },
            BuildArchetype.CARRY_HUNTER: {
                ItemAttribute.ATTACK_SPEED: 0.90,
                ItemAttribute.BASIC_ATTACK_DAMAGE: 40,
                ItemAttribute.CRITICAL_STRIKE_CHANCE: 0.45,
                ItemAttribute.PHYSICAL_LIFESTEAL: 0.30,
                ItemAttribute.PHYSICAL_PENETRATION: (10, 0.20),
                ItemAttribute.PHYSICAL_POWER: 185,
            },
            BuildArchetype.ABILITY_BASED_HUNTER: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.10,
                ItemAttribute.HEALTH: 100,
                ItemAttribute.MANA: 1050,
                ItemAttribute.MP5: 10,
                ItemAttribute.PHYSICAL_PENETRATION: (15, 0.20),
                ItemAttribute.PHYSICAL_POWER: 285,
            },
            BuildArchetype.MID_MAGE: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.30,
                ItemAttribute.MAGICAL_PENETRATION: (25, 0.30),
                ItemAttribute.MAGICAL_POWER: 560,
                ItemAttribute.MANA: 1300,
                ItemAttribute.MP5: 40,
            },
            BuildArchetype.LIFESTEAL_MID_MAGE: {
                ItemAttribute.MAGICAL_LIFESTEAL: 0.40,
                ItemAttribute.MAGICAL_PENETRATION: (30, 0.30),
                ItemAttribute.MAGICAL_POWER: 600,
                ItemAttribute.MANA: 150,
                ItemAttribute.MP5: 45,
            },
            BuildArchetype.JUNGLE_MAGE: {
                ItemAttribute.ATTACK_SPEED: 0.20,
                ItemAttribute.MAGICAL_LIFESTEAL: 0.25,
                ItemAttribute.MAGICAL_PENETRATION: (10, 0.20),
                ItemAttribute.MAGICAL_POWER: 500,
                ItemAttribute.MANA: 1200,
                ItemAttribute.MP5: 50,
            },
            BuildArchetype.HEALER_MAGE: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.30,
                ItemAttribute.HEALTH: 350,
                ItemAttribute.MAGICAL_LIFESTEAL: 0.10,
                ItemAttribute.MAGICAL_PENETRATION: (10, 0.10),
                ItemAttribute.MAGICAL_POWER: 600,
                ItemAttribute.MANA: 1600,
                ItemAttribute.MP5: 65,
            },
            BuildArchetype.AUTO_ATTACK_MAGE: {
                ItemAttribute.ATTACK_SPEED: 0.80,
                ItemAttribute.MAGICAL_LIFESTEAL: 0.30,
                ItemAttribute.MAGICAL_PENETRATION: (0, 0.10),
                ItemAttribute.MAGICAL_POWER: 400,
                ItemAttribute.MANA: 200,
            },
            BuildArchetype.SOLO_MAGE: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.20,
                ItemAttribute.HEALTH: 600,
                ItemAttribute.MAGICAL_LIFESTEAL: 0.20,
                ItemAttribute.MAGICAL_PENETRATION: (0, 0.10),
                ItemAttribute.MAGICAL_POWER: 400,
                ItemAttribute.MANA: 1000,
                ItemAttribute.MP5: 25,
                ItemAttribute.PHYSICAL_PROTECTION: 100,
                ItemAttribute.MAGICAL_PROTECTION: 30,
            },
            BuildArchetype.SUPPORT_MAGE: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.20,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 0.20,
                ItemAttribute.HEALTH: 1200,
                ItemAttribute.HP5: 15,
                ItemAttribute.MAGICAL_PROTECTION: 155,
                ItemAttribute.MP5: 15,
                ItemAttribute.PHYSICAL_PROTECTION: 155,
            },
            BuildArchetype.ABILITY_BASED_WARRIOR: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.20,
                ItemAttribute.HEALTH: 600,
                ItemAttribute.HP5: 30,
                ItemAttribute.MAGICAL_PROTECTION: 125,
                ItemAttribute.MANA: 200,
                ItemAttribute.MP5: 60,
                ItemAttribute.PHYSICAL_LIFESTEAL: 0.15,
                ItemAttribute.PHYSICAL_PENETRATION: (0, 0.10),
                ItemAttribute.PHYSICAL_POWER: 100,
                ItemAttribute.PHYSICAL_PROTECTION: 155,
            },
            BuildArchetype.AUTO_ATTACK_WARRIOR: {
                ItemAttribute.ATTACK_SPEED: 0.50,
                ItemAttribute.COOLDOWN_REDUCTION: 0.10,
                ItemAttribute.HEALTH: 500,
                ItemAttribute.HP5: 15,
                ItemAttribute.MAGICAL_PROTECTION: 110,
                ItemAttribute.MOVEMENT_SPEED: 0.07,
                ItemAttribute.MP5: 20,
                ItemAttribute.PHYSICAL_PENETRATION: (15, 0),
                ItemAttribute.PHYSICAL_POWER: 80,
                ItemAttribute.PHYSICAL_PROTECTION: 110,
            },
            BuildArchetype.JUNGLE_WARRIOR: {
                ItemAttribute.ATTACK_SPEED: 0.60,
                ItemAttribute.COOLDOWN_REDUCTION: 0.10,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 0.10,
                ItemAttribute.HEALTH: 200,
                ItemAttribute.HP5: 15,
                ItemAttribute.MAGICAL_PROTECTION: 40,
                ItemAttribute.MOVEMENT_SPEED: 0.07,
                ItemAttribute.PHYSICAL_PENETRATION: (20, 0.10),
                ItemAttribute.PHYSICAL_POWER: 100,
                ItemAttribute.PHYSICAL_PROTECTION: 80,
            },
            BuildArchetype.SUPPORT_WARRIOR: {
                ItemAttribute.HEALTH: 1400,
                ItemAttribute.HP5: 15,
                ItemAttribute.MAGICAL_PROTECTION: 175,
                ItemAttribute.MANA: 300,
                ItemAttribute.MP5: 50,
                ItemAttribute.PHYSICAL_PROTECTION: 195,
            },
        }

        self.__archetype_stat_targets[BuildArchetype.HEALER_WARRIOR] = \
            self.__archetype_stat_targets[BuildArchetype.ABILITY_BASED_WARRIOR].copy()
        self.__archetype_stat_targets[BuildArchetype.HEALER_GUARDIAN] = \
            self.__archetype_stat_targets[BuildArchetype.SUPPORT_GUARDIAN].copy()

    def __init_level_20_stats(self):
        self.__level_20_stats = {}
        for attr in list(ItemAttribute):
            self.__level_20_stats[attr] = self.god.get_stat_at_level(attr, 20)

    def __compute_item_score(
            self,
            item: Item,
            weights: Dict[ItemAttribute, float]) -> float:
        build_stats = self.compute_build_stats([item])
        return self.__compute_properties_score(build_stats, weights)

    def __compute_properties_score(
            self,
            build_stats: Dict[ItemAttribute, ItemProperty],
            weights: Dict[ItemAttribute, float]) -> float:
        score = 0
        for attr, prop in build_stats.items():
            flat_weight = pct_weight = weights[attr]
            if attr in (ItemAttribute.PHYSICAL_PENETRATION, ItemAttribute.MAGICAL_PENETRATION):
                pct_weight = weights[attr][0]
                flat_weight = weights[attr][1]
            if prop.flat_value > 0:
                level_20_stat = self.__level_20_stats[attr]
                score += (prop.flat_value / \
                    (self.FLAT_ITEM_ATTRIBUTE_CAPS[attr] - level_20_stat)) * flat_weight
            if prop.percent_value > 0:
                if attr in (ItemAttribute.ATTACK_SPEED, ItemAttribute.MOVEMENT_SPEED):
                    level_20_stat = self.__level_20_stats[attr]
                    score += ((level_20_stat + (level_20_stat * prop.percent_value)) / \
                        self.FLAT_ITEM_ATTRIBUTE_CAPS[attr]) * pct_weight
                    continue
                score += (prop.percent_value / \
                    self.PERCENT_ITEM_ATTRIBUTE_CAPS[attr]) * pct_weight
        return score

    def __check_build_on_target(self, build: List[Item]) -> bool:
        stats = self.compute_build_stats(build)
        stat_targets = self.__archetype_stat_targets[self.__current_archetype]
        for stat in stat_targets:
            if stat in stats:
                flat_value = stats[stat].flat_value
                pct_value = stats[stat].percent_value
                flat_target = pct_target = stat_targets[stat]
                if stat in (ItemAttribute.MAGICAL_PENETRATION, \
                        ItemAttribute.PHYSICAL_PENETRATION):
                    flat_target = flat_target[0]
                    pct_target = pct_target[1]
                    flat_value = 0.1 if flat_value == 0 else flat_value
                    pct_value = 0.01 if pct_value == 0 else pct_value
                if 0 < flat_value < flat_target:
                    return False
                if 0 < pct_value < pct_target:
                    return False
            else:
                return False
        if self.__check_overcapped(
                stats,
                {passive for item in build for passive in item.passive_properties}):
            return False
        return True

    def __check_overcapped(
            self,
            stats: Dict[ItemAttribute, ItemProperty],
            passives: Set[PassiveAttribute]) -> bool:
        for attr, prop in stats.items():
            if attr in self.FLAT_ITEM_ATTRIBUTE_CAPS:
                flat_value = prop.flat_value
                if attr == ItemAttribute.ATTACK_SPEED:
                    attack_speed = self.god.get_stat_at_level(attr, 20)
                    flat_value = attack_speed + attack_speed * prop.percent_value
                elif attr not in (\
                        ItemAttribute.MAGICAL_PENETRATION, \
                        ItemAttribute.PHYSICAL_PENETRATION):
                    flat_value += self.god.get_stat_at_level(attr, 20)
                flat_cap = self.FLAT_ITEM_ATTRIBUTE_CAPS[attr]
                if flat_value > flat_cap:
                    if attr in (ItemAttribute.MAGICAL_POWER, \
                            ItemAttribute.PHYSICAL_POWER):
                        # Soft cap for these two
                        return False
                    if attr in (ItemAttribute.MAGICAL_PENETRATION, \
                            ItemAttribute.PHYSICAL_PENETRATION):
                        if PassiveAttribute.ALLOWS_OVERCAPPING_PENETRATION_WITH_FIRST_ABILITY in \
                            passives:
                            continue
                    return True
            if attr in self.PERCENT_ITEM_ATTRIBUTE_CAPS:
                pct_value = prop.percent_value
                pct_cap = self.PERCENT_ITEM_ATTRIBUTE_CAPS[attr]
                if pct_value > pct_cap:
                    if attr == ItemAttribute.ATTACK_SPEED:
                        if PassiveAttribute.ALLOWS_OVERCAPPING_ATTACK_SPEED in \
                            passives:
                            continue
                    return True
        return False

    def __get_weights(self) -> Dict[ItemAttribute, float]:
        inverted_type = GodType.PHYSICAL if self.god.type != GodType.PHYSICAL else GodType.MAGICAL
        self_stat: Set[ItemAttribute] = set() if self.__stat is None else self.__stat if \
            isinstance(self.__stat, set) else set([self.__stat])
        stats_to_optimize: Set[ItemAttribute] = self_stat\
            .difference(self.GOD_TYPE_MAPPINGS[inverted_type])
        god_weights = self.__archetype_weight_mappings[self.__current_archetype]
        if any(stats_to_optimize):
            for stat in stats_to_optimize:
                god_weights[stat] = 15
        return god_weights

    def __compute_scores(self, weights: Dict[ItemAttribute, float]) -> Dict[ItemAttribute, float]:
        self.__item_scores = {}
        for item in self.valid_items:
            self.__item_scores[item.id] = self.__compute_item_score(item, weights)

    async def optimize(self) -> Tuple[List[List[Item]], int]:
        god_weights = self.__get_weights()
        self.__compute_scores(god_weights)
        starters = self.get_preferred_starters()

        sorted_ids = list([id for id, _ in sorted(
            self.__item_scores.items(), key=lambda item: item[1], reverse=True)])
        rated_items = [self.__all_items[id] for id in sorted_ids]
        rated_items = self.filter_tiers_with_glyphs(rated_items)
        rated_items = rated_items[:int(len(rated_items) / 3)] # Top 50th percentile of item rankings

        glyphs = self.get_glyphs(rated_items)
        items = self.filter_evolution_parents(
            self.filter_tiers(rated_items))

        viable_builds: List[List[Item]] = []
        async def check_combinations(
                existing_build: FrozenSet[Item],
                combo_items: List[Item], size: int) -> int:
            build_n = 1
            combinations = itertools.combinations(combo_items, size)
            for combo in combinations:
                await asyncio.sleep(0)
                next_items = frozenset(combo)
                item_build = existing_build.union(next_items)
                build_n += 1
                if self.__check_build_on_target(item_build):
                    viable_builds.append(list(item_build))
            return build_n

        build_size = 5
        iterations = 0
        for starter in starters:
            starter_build: FrozenSet[Item] = frozenset([starter])
            for glyph in glyphs:
                glyph_build = starter_build.union(frozenset([glyph]))
                non_glyph_items = self.filter_glyph_parent(items, glyph)
                build_n = await check_combinations(
                    glyph_build, non_glyph_items, build_size - 1)
                iterations += build_n
                print(f'Iterated {iterations} times...')
            build_n = await check_combinations(
                starter_build, items, build_size)
            iterations += build_n
            print(f'Iterated {iterations} times...')

        for build in viable_builds:
            starter_idx = 0
            evos = []

            # Place starter first
            for idx, item in enumerate(build):
                if item.is_starter:
                    starter_idx = idx
            if starter_idx != 0:
                build[starter_idx], build[0] = build[0], build[starter_idx]

            build[1:] = sorted(
                build[1:],
                key=lambda i: self.compute_item_price(i) if not i.glyph \
                    else self.compute_item_price(
                        self.__all_items[i.parent_item_id]))

            for item in build:
                if item.tier == 4 and not item.glyph:
                    evos.append(item)
            # Place evolved items second
            for evo in evos:
                build.remove(evo)
            for evo in reversed(evos):
                build.insert(1, evo)
        return (viable_builds, iterations)

    def compute_build_stats(self, items: List[Item]) -> Dict[ItemAttribute, ItemProperty]:
        attributes: Dict[ItemAttribute, ItemProperty] = {}
        protections: float = None
        maximum_health: float = None
        def add_attribute(iattr: ItemAttribute, iprop: ItemProperty):
            if iattr in attributes:
                pval = attributes[iattr]
                pval.flat_value += iprop.flat_value if iprop.flat_value is not None else 0
                pval.percent_value += iprop.percent_value if iprop.percent_value is not None else 0
            else:
                attributes[iattr] = ItemProperty(
                    iattr,
                    iprop.flat_value if iprop.flat_value is not None else 0,
                    iprop.percent_value if iprop.percent_value is not None else 0)
        for item in items:
            for prop in item.item_properties:
                attr = prop.attribute
                if attr.god_type is not None and attr.god_type != self.god.type:
                    continue
                if attr == ItemAttribute.PROTECTIONS:
                    protections = prop.percent_value
                    continue
                if attr == ItemAttribute.MAXIMUM_HEALTH:
                    maximum_health = prop.percent_value
                    continue
                if attr == ItemAttribute.HP5_AND_MP5:
                    add_attribute(ItemAttribute.HP5, prop)
                    add_attribute(ItemAttribute.MP5, prop)
                    continue
                if attr == ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE:
                    attr = ItemAttribute.CRITICAL_STRIKE_CHANCE
                if attr == ItemAttribute.PENETRATION:
                    if self.god.type == GodType.MAGICAL:
                        attr = ItemAttribute.MAGICAL_PENETRATION
                    else:
                        attr = ItemAttribute.PHYSICAL_PENETRATION
                add_attribute(attr, prop)
        if protections is not None:
            if ItemAttribute.MAGICAL_PROTECTION in attributes:
                attributes[ItemAttribute.MAGICAL_PROTECTION] *= protections
            if ItemAttribute.PHYSICAL_PROTECTION in attributes:
                attributes[ItemAttribute.PHYSICAL_PROTECTION] *= protections
        if maximum_health is not None:
            if ItemAttribute.HEALTH in attributes:
                attributes[ItemAttribute.HEALTH] *= maximum_health
        return attributes

    def compute_item_price(self, item: Item) -> int:
        total_price = item.price
        parent_id = item.parent_item_id
        while parent_id is not None:
            parent = self.__all_items[parent_id]
            total_price += parent.price
            parent_id = parent.parent_item_id
        return total_price

    def compute_price(self, items: List[Item]) -> int:
        total_price = 0
        for item in items:
            total_price += self.compute_item_price(item)
        return total_price

    def set_stat(self, stat_name: str):
        self.__init_stat(stat_name)

    def filter_unwanted_items(self, items: List[Item]) -> List[Item]:
        def all_item_properties_unwanted(item: Item) -> bool:
            stat_targets = self.__archetype_stat_targets[self.__current_archetype]
            for prop in item.item_properties:
                if prop.attribute in stat_targets:
                    return True
            return False
        return list(filter(all_item_properties_unwanted, items))

    @staticmethod
    def filter_tiers(items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.tier >= 3 and not item.glyph, items))

    @staticmethod
    def filter_tiers_with_glyphs(items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.tier >= 3, items))

    @staticmethod
    def filter_glyph_parent(items: List[Item], glyph: Item) -> List[Item]:
        return list(filter(lambda item: item.id != glyph.parent_item_id, items))

    @staticmethod
    def get_evolutions(items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.tier == 4 and not item.glyph, items))

    def filter_evolution_parents(self, items: List[Item]) -> List[Item]:
        return list(filter(
            lambda item: item.id not in \
                [evo.parent_item_id for evo in \
                    self.get_evolutions(self.__all_items.values())], items))

    @staticmethod
    def filter_prioritize(items: List[Item], prioritize: str) -> List[Item]:
        power_allowed = (
            ItemAttribute.ATTACK_SPEED,
            ItemAttribute.BASIC_ATTACK_DAMAGE,
            ItemAttribute.COOLDOWN_REDUCTION,
            ItemAttribute.CRITICAL_STRIKE_CHANCE,
            ItemAttribute.HP5,
            ItemAttribute.HP5_AND_MP5,
            ItemAttribute.MP5,
            ItemAttribute.MAGICAL_LIFESTEAL,
            ItemAttribute.MAGICAL_PENETRATION,
            ItemAttribute.MAGICAL_POWER,
            ItemAttribute.MANA,
            ItemAttribute.MOVEMENT_SPEED,
            ItemAttribute.PENETRATION,
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE,
            ItemAttribute.PHYSICAL_LIFESTEAL,
            ItemAttribute.PHYSICAL_PENETRATION,
            ItemAttribute.PHYSICAL_POWER,
        )
        defense_allowed = (
            ItemAttribute.ATTACK_SPEED,
            ItemAttribute.COOLDOWN_REDUCTION,
            ItemAttribute.CROWD_CONTROL_REDUCTION,
            ItemAttribute.DAMAGE_REDUCTION,
            ItemAttribute.HP5,
            ItemAttribute.HP5_AND_MP5,
            ItemAttribute.HEALTH,
            ItemAttribute.MP5,
            ItemAttribute.MAGICAL_PROTECTION,
            ItemAttribute.MANA,
            ItemAttribute.MAXIMUM_HEALTH,
            ItemAttribute.MOVEMENT_SPEED,
            ItemAttribute.PHYSICAL_PROTECTION,
            ItemAttribute.PROTECTIONS,
        )
        def filter_items(allowed: Set[ItemAttribute]) -> List[Item]:
            return list(filter(
                lambda item: all([p.attribute in allowed for p in item.item_properties]),
                items))
        if prioritize == 'power':
            return filter_items(power_allowed)
        elif prioritize == 'defense':
            return filter_items(defense_allowed)
        raise ValueError

    @staticmethod
    def get_glyphs(items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.glyph, items))

    def get_glyph_parent_if_no_glyphs(self, items: List[Item]) -> Tuple[int, Item]:
        glyphs = self.get_glyphs(self.valid_items)
        potential_glyphs = []
        for idx, item in enumerate(items):
            for glyph in glyphs:
                if glyph.parent_item_id == item.id:
                    potential_glyphs.append((idx, glyph))
        if any(potential_glyphs):
            return random.choice(potential_glyphs)
        return (None, None)

    def get_ratatoskr_acorn(self, items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.root_item_id == self.MAGIC_ACORN_ID, items))

    def get_preferred_starters(self) -> List[Item]:
        return self.get_starters(list(filter(
            lambda item: item.root_item_id \
                in self.ARCHETYPE_PREFERRED_STARTER[self.__current_archetype],
            self.__all_items.values())))

    def get_starters(self, items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.tier == 2 and
            item.parent_item_id in self.__all_items and
            self.__all_items[item.parent_item_id].is_starter and
            item.root_item_id != self.MAGIC_ACORN_ID, items))

    def filter_acorns(self, items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.root_item_id != self.MAGIC_ACORN_ID, items))

    def get_build_stats_string(self, build: List[Item]) -> str:
        build_stats = self.compute_build_stats(build)
        total_price = self.compute_price(build)
        desc = f'**Stats** _(Total Price - {total_price:,})_:\n\n'
        stats = build_stats.values()
        def get_level_20(attr: ItemAttribute, value: float) -> str:
            stat = self.__level_20_stats[attr]
            if stat > 0:
                if attr in (ItemAttribute.ATTACK_SPEED, ItemAttribute.MOVEMENT_SPEED):
                    return f'_({(stat + stat * value):.1f} @ Level 20)_'
                return f'_({int(stat + value)} @ Level 20)_'
            return ''
        for stat in sorted(stats, key=lambda s: s.attribute.value):
            percent_prefix = stat.attribute in (
                ItemAttribute.PENETRATION,
                ItemAttribute.MAGICAL_PENETRATION,
                ItemAttribute.PHYSICAL_PENETRATION,
            )
            if stat.flat_value > 0:
                desc += f'**{"Flat " if percent_prefix else ""}'\
                        f'{stat.attribute.display_name}**: {int(stat.flat_value)} '\
                        f'{get_level_20(stat.attribute, stat.flat_value)}\n'
            if stat.percent_value > 0:
                desc += f'**{"Percent " if percent_prefix else ""}'\
                        f'{stat.attribute.display_name}**: {round(stat.percent_value * 100)}% '\
                        f'{get_level_20(stat.attribute, stat.percent_value)}\n'
        return desc
