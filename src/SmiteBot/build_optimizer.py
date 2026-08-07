import asyncio
import itertools
import random
from enum import Enum
from typing import Dict, FrozenSet, List, Set, Tuple, Union

from god import God
from god_types import GodId, GodPro, GodRole, GodType
from item import Item, ItemAttribute, ItemProperty, ItemType
from passive_parser import PassiveAttribute
import team_context
from stat_calculator import BuildStatCalculator, GodBuild, _Penetration, _Stats
from HirezAPI import QueueId


def compute_item_price(item: Item, all_items: Dict[int, Item]) -> int:
    """What an item costs including everything it was built out of.

    A free function because it needs nothing else an optimizer has. `$item`
    used to reach it by constructing `BuildOptimizer(gods[GodId.AGNI], [],
    items)` — a whole archetype-scoring optimizer, and a hardcoded Smite 1 god,
    to walk a parent chain.

    A stated total wins over walking. Smite 1 has no choice but to walk, and it
    is correct there because a recipe is a single chain. Smite 2's recipes fork
    — Book of Thoth is built from Oracle Staff *and* Mana Tome — so a walk down
    one branch would report 1,650 for an item that costs 2,300, which is the
    kind of wrong that looks plausible on a card.
    """
    if getattr(item, "total_cost", None):
        return item.total_cost

    total_price = item.price
    parent_id = item.parent_item_id
    seen = set()
    while parent_id is not None and parent_id not in seen:
        # Guard the walk. A cycle would otherwise hang the command, and the
        # Smite 2 tree is parsed out of an external page rather than an API.
        seen.add(parent_id)
        parent = all_items.get(parent_id)
        if parent is None:
            break
        total_price += parent.price
        parent_id = parent.parent_item_id
    return total_price


# Which of Smite 1's stats buy damage and which buy survival, for the balance
# tilt. Cooldown reduction, mana and movement are in neither: they are what both
# kinds of build want, and scaling them would make a tank ask for fewer
# cooldowns than the same god asks for as a bruiser.
OFFENSIVE_STATS = frozenset(
    {
        ItemAttribute.PHYSICAL_POWER,
        ItemAttribute.MAGICAL_POWER,
        ItemAttribute.PHYSICAL_PENETRATION,
        ItemAttribute.MAGICAL_PENETRATION,
        ItemAttribute.ATTACK_SPEED,
        ItemAttribute.BASIC_ATTACK_DAMAGE,
        ItemAttribute.CRITICAL_STRIKE_CHANCE,
        ItemAttribute.PHYSICAL_LIFESTEAL,
        ItemAttribute.MAGICAL_LIFESTEAL,
    }
)

DEFENSIVE_STATS = frozenset(
    {
        ItemAttribute.HEALTH,
        ItemAttribute.PHYSICAL_PROTECTION,
        ItemAttribute.MAGICAL_PROTECTION,
        ItemAttribute.HP5,
        ItemAttribute.DAMAGE_REDUCTION,
        ItemAttribute.CROWD_CONTROL_REDUCTION,
    }
)

# What a full tank's defensive share looks like, and the point the stat targets
# were written against. Kept as a number here rather than imported from
# `smite2_optimizer.BuildBalance`, which holds the same value: Smite 1's
# optimizer should not depend on Smite 2's for a constant.
_TANK_BALANCE = 0.85


# What an item's passive is worth, on top of its stat line.
#
# `PassiveParser` has always classified passives into these attributes and
# nothing has ever *valued* them, so an item whose whole point is its passive —
# Divine Ruin's anti-heal, Titan's Bane stripping protections — scored exactly as
# though it had none. This is the Smite 1 half of what `smite2_stats` does by
# reading numbers out of the text: Smite 1's passives are already parsed, so
# what was missing was a number per kind rather than a parser.
#
# The scale is item-score units, where a whole stat line is worth a few. These
# are judgement rather than measurement — unlike Smite 2's lane profiles there
# is no Smite 1 accuracy harness to fit them against yet — so they are
# deliberately modest: enough to separate an item with a real passive from one
# with filler, not enough to outrank a stat line on their own.
PASSIVE_VALUE: Dict[PassiveAttribute, float] = {
    # Effects that change what the rest of the build is worth.
    PassiveAttribute.STRIPS_PROTECTIONS: 1.6,
    PassiveAttribute.ANTIHEAL: 1.4,
    PassiveAttribute.DECREASES_ABILITY_COOLDOWNS: 1.4,
    PassiveAttribute.PERCENT_DAMAGE: 1.3,
    PassiveAttribute.SCALING_BONUS_DAMAGE: 1.2,
    PassiveAttribute.INCREASES_CRITICAL_DAMAGE: 1.2,
    PassiveAttribute.DAMAGE_MITIGATION: 1.2,
    PassiveAttribute.INCREASES_COOLDOWN_CAP: 1.1,
    PassiveAttribute.EVOLVES: 1.0,
    PassiveAttribute.PERCENT_STAT_CONVERTED: 1.0,
    # Real, situational, and common.
    PassiveAttribute.AURA: 0.8,
    PassiveAttribute.ENEMY_STAT_REDUCTION_AURA: 0.9,
    PassiveAttribute.ALLIED_GODS_BUFF_AURA: 0.8,
    PassiveAttribute.SHIELD: 0.8,
    PassiveAttribute.INCREASE_DAMAGE_BELOW_TARGET_THRESHOLD: 0.8,
    PassiveAttribute.FLAT_TRUE_BONUS_DAMAGE: 0.7,
    PassiveAttribute.BASIC_ATTACK_PROC: 0.7,
    PassiveAttribute.ABILITY_PROC: 0.7,
    PassiveAttribute.ULTIMATE_PROC: 0.6,
    PassiveAttribute.TRIGGERS_HEAL: 0.6,
    PassiveAttribute.ABILITY_HEALING: 0.6,
    PassiveAttribute.INCREASES_LIFESTEAL: 0.6,
    PassiveAttribute.CAUSES_CC: 0.6,
    PassiveAttribute.DAMAGE_SCALES_FROM_PROTECTIONS: 0.6,
    PassiveAttribute.DECREASES_CRITICAL_DAMAGE_TAKEN: 0.6,
    PassiveAttribute.IMMUNE_TO_CC: 0.6,
    PassiveAttribute.IMMUNE_TO_SLOWS: 0.5,
    PassiveAttribute.BLOCK_STACKS: 0.5,
    PassiveAttribute.DAMAGING_AURA: 0.5,
    PassiveAttribute.AREA_OF_EFFECT_BASIC_ATTACKS: 0.5,
    PassiveAttribute.INCREASES_HEALING: 0.5,
    PassiveAttribute.BELOW_THRESHOLD_BUFF: 0.4,
    PassiveAttribute.TRIGGERED_BY_CC: 0.4,
    PassiveAttribute.CRITICAL_HIT_EFFECT: 0.4,
    PassiveAttribute.INCREASES_ENEMY_DAMAGE_WHEN_CC: 0.4,
    PassiveAttribute.DECREASES_RELIC_COOLDOWNS: 0.4,
    PassiveAttribute.SELF_BUFF_ON_HEAL: 0.3,
    PassiveAttribute.ALLIED_GODS_BUFF_ON_HEAL: 0.3,
    PassiveAttribute.ALLIED_GODS_CAN_CRITICAL_HIT: 0.3,
    PassiveAttribute.INCREASED_PROJECTILE_SPEED: 0.2,
    PassiveAttribute.ALLIED_MINIONS_BUFF: 0.2,
    PassiveAttribute.ALLIED_STRUCTURES_BUFF: 0.2,
    PassiveAttribute.ENEMY_STRUCTURE_REDUCTION: 0.2,
    # Deliberately zero. These describe *how* a passive arrives rather than
    # what it does — an item does not become better for stacking — and the
    # effect they gate is already counted under its own attribute. The
    # overcapping pair is worth nothing on its own too: it is only ever useful
    # alongside the stat it uncaps, which the stat line already scores.
    PassiveAttribute.STACKS: 0.0,
    PassiveAttribute.EVOLVES_WITH_GOD_KILLS: 0.0,
    PassiveAttribute.EVOLVES_WITH_ASSISTS: 0.0,
    PassiveAttribute.EVOLVES_WITH_MINION_KILLS: 0.0,
    PassiveAttribute.EFFECT_VARIES_BY_CURRENT_STATS: 0.0,
    PassiveAttribute.INCREASES_WITH_MISSING_STAT: 0.0,
    PassiveAttribute.ALLOWS_OVERCAPPING_PENETRATION_WITH_FIRST_ABILITY: 0.0,
    PassiveAttribute.ALLOWS_OVERCAPPING_ATTACK_SPEED: 0.0,
    # Jungle-only effects are worth nothing to the five gods in a lane and
    # everything to the one clearing camps; with no way to tell which is being
    # built for, they stay out of the score.
    PassiveAttribute.IN_JUNGLE_EFFECT: 0.0,
    PassiveAttribute.INCREASES_JUNGLE_MONSTER_DAMAGE: 0.0,
}

# How much of a passive's value to count, mirroring Smite 2's discount and for
# the same reason: nearly all of these are conditional.
PASSIVE_DISCOUNT = 0.6


# Stand-ins for the three attributes that depend on which kind of damage the god
# deals. The weight tables carry both halves of each pair with the irrelevant
# one set to None, so a nudge has to be resolved against the god before it can
# be applied.
_POWER = "power"
_PENETRATION = "penetration"
_LIFESTEAL = "lifesteal"

_BY_GOD_TYPE: Dict[str, Dict[GodType, ItemAttribute]] = {
    _POWER: {
        GodType.PHYSICAL: ItemAttribute.PHYSICAL_POWER,
        GodType.MAGICAL: ItemAttribute.MAGICAL_POWER,
    },
    _PENETRATION: {
        GodType.PHYSICAL: ItemAttribute.PHYSICAL_PENETRATION,
        GodType.MAGICAL: ItemAttribute.MAGICAL_PENETRATION,
    },
    _LIFESTEAL: {
        GodType.PHYSICAL: ItemAttribute.PHYSICAL_LIFESTEAL,
        GodType.MAGICAL: ItemAttribute.MAGICAL_LIFESTEAL,
    },
}

# What a god's own description implies about what it should build.
#
# Smite 1 had nothing of the kind, and it showed: the four most-played solo gods
# — Surtr, Chaac, Cu Chulainn, Vamana — all resolve to ABILITY_BASED_WARRIOR,
# and with the archetype being the entire model they were handed the *same six
# items* and scored 0/6 against their metas. Their `pros` differ ("high single
# target damage", "high sustain", "high defense") and nothing had ever read them
# for anything but one passive-denylist check.
#
# This is the same idea as Smite 2's spec nudges, which are the largest single
# effect measured on that model, and it is applied the same way: on top of the
# archetype rather than instead of it, so a god's description reorders its
# priorities without inventing a build its archetype never wanted.
#
# The strength is fitted rather than picked. Against the twenty-five most-played
# gods, halving these scores 2.00 and leaves two of the four solo gods sharing a
# build; these values score 2.08 and separate all four; and raising them by half
# again scores 1.92, because at that point the god's description is outvoting
# the archetype rather than colouring it.
PRO_EMPHASIS: Dict[GodPro, Dict[object, float]] = {
    GodPro.HIGH_SUSTAIN: {_LIFESTEAL: 4, ItemAttribute.HP5: 3},
    GodPro.HIGH_DEFENSE: {
        ItemAttribute.HEALTH: 4,
        ItemAttribute.PHYSICAL_PROTECTION: 3,
        ItemAttribute.MAGICAL_PROTECTION: 3,
    },
    GodPro.HIGH_CROWD_CONTROL: {
        ItemAttribute.COOLDOWN_REDUCTION: 3,
        ItemAttribute.CROWD_CONTROL_REDUCTION: 1,
    },
    GodPro.HIGH_AREA_DAMAGE: {_POWER: 3, ItemAttribute.COOLDOWN_REDUCTION: 2},
    GodPro.HIGH_SINGLE_TARGET_DAMAGE: {_POWER: 3, _PENETRATION: 3},
    GodPro.HIGH_ATTACK_SPEED: {
        ItemAttribute.ATTACK_SPEED: 4,
        ItemAttribute.BASIC_ATTACK_DAMAGE: 2,
    },
    GodPro.HIGH_MOBILITY: {ItemAttribute.MOVEMENT_SPEED: 2},
    GodPro.HIGH_MOVEMENT_SPEED: {ItemAttribute.MOVEMENT_SPEED: 3},
    GodPro.GREAT_JUNGLER: {_PENETRATION: 2, _POWER: 1},
    GodPro.PUSHER: {ItemAttribute.ATTACK_SPEED: 2},
    # The medium tiers say the same thing more quietly.
    GodPro.MEDIUM_AREA_DAMAGE: {_POWER: 1.5, ItemAttribute.COOLDOWN_REDUCTION: 1},
    GodPro.MEDIUM_SINGLE_TARGET_DAMAGE: {_POWER: 1.5, _PENETRATION: 1.5},
    GodPro.MEDIUM_CROWD_CONTROL: {ItemAttribute.COOLDOWN_REDUCTION: 1.5},
}


# How many combinations to check between yields to the event loop. The search
# runs inside the bot's process, so it has to let other commands through; it
# does not have to do so half a million times.
_YIELD_EVERY = 2048

# How many equally-close builds to keep when nothing meets every target. Enough
# to choose between, small enough that a search meeting none of them does not
# accumulate a million builds.
_NEAR_MISS_LIMIT = 200


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
    # 23 was a copy of HEALER_GUARDIAN's value, which does not make two
    # archetypes — an Enum turns the second name into an alias for the first, so
    # this *was* HEALER_GUARDIAN, and `ATTACK_SPEED_STIM_HUNTER.name` returned
    # "HEALER_GUARDIAN". No god maps to it today, so nothing was built as a
    # healer by mistake; the next one to be added would have been.
    ATTACK_SPEED_STIM_HUNTER = 26

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


# The archetypes meant to be bruisers rather than tanks, and the defensive share
# they should aim at.
#
# Warriors optimized to six defensive items. Their stat targets demand
# protections and health, so every viable build is defensive; ranking among
# those by weights that value health and protections as highly as power then
# picks the tankiest of them, and nothing pulls the other way. Achilles came
# back with Tainted Breastplate, Prophetic Cloak, Absolution, Determination,
# Void Doumaru and Mantle of Discord — a build with no damage in it at all.
#
# Guardians are deliberately absent: a support that builds like a tank is
# correct, and their profiles are left exactly as they were.
BRUISER_ARCHETYPE_BALANCE: Dict[BuildArchetype, float] = {
    BuildArchetype.ABILITY_BASED_WARRIOR: 0.5,
    BuildArchetype.AUTO_ATTACK_WARRIOR: 0.45,
    BuildArchetype.JUNGLE_WARRIOR: 0.35,
    BuildArchetype.HEALER_WARRIOR: 0.6,
    BuildArchetype.SOLO_ASSASSIN: 0.45,
    BuildArchetype.SOLO_MAGE: 0.45,
}


class BuildOptimizer:
    JUNGLE_STARTERS = {
        19500,  # Manikin Scepter
        19502,  # Bumba's Dagger
        19694,  # Eye of the Jungle
    }
    SOLO_STARTERS = {
        19490,  # Bluestone Pendant
        19496,  # Warrior's Axe
        19640,  # Tainted Steel
        19751,  # Warding Sigil
        19492,  # Death's Toll
    }
    SUPPORT_STARTERS = {
        19609,  # Sentinel's Gift
        19634,  # Benevolence
        20698,  # War Flag
    }
    MID_STARTERS = {
        19677,  # Conduit Gem
        19510,  # Vampiric Shroud
        19508,  # Sands of Time
    }
    CARRY_STARTERS = {
        19492,  # Death's Toll
        19494,  # Gilded Arrow
        19672,  # Leather Cowl
    }

    ARCHETYPE_PREFERRED_STARTER: Dict[BuildArchetype, Set[int]] = {
        BuildArchetype.ABILITY_BASED_ASSASSIN: JUNGLE_STARTERS,
        BuildArchetype.AUTO_ATTACK_ASSASSIN: JUNGLE_STARTERS,
        BuildArchetype.AUTO_ATTACK_WITH_CRIT_ASSASSIN: JUNGLE_STARTERS,
        BuildArchetype.SOLO_ASSASSIN: SOLO_STARTERS.copy().difference({19492}),
        BuildArchetype.SUPPORT_GUARDIAN: SUPPORT_STARTERS,
        BuildArchetype.HEALER_GUARDIAN: SUPPORT_STARTERS,
        BuildArchetype.SOLO_GUARDIAN: MID_STARTERS.copy().union({19496}),
        BuildArchetype.CARRY_HUNTER: CARRY_STARTERS,
        BuildArchetype.ABILITY_BASED_HUNTER: {
            19500,  # Manikin Scepter
            19490,  # Bluestone Pendant
        },
        BuildArchetype.MID_MAGE: MID_STARTERS.copy().difference({19510}),
        BuildArchetype.LIFESTEAL_MID_MAGE: {
            19510,  # Vampiric Shroud
        },
        BuildArchetype.JUNGLE_MAGE: JUNGLE_STARTERS,
        BuildArchetype.HEALER_MAGE: MID_STARTERS.copy().difference({19510}),
        BuildArchetype.AUTO_ATTACK_MAGE: MID_STARTERS.copy()
        .difference({19510})
        .union({19500}),
        BuildArchetype.SOLO_MAGE: MID_STARTERS.copy()
        .union(SOLO_STARTERS.union())
        .difference({19490, 19492}),
        BuildArchetype.SUPPORT_MAGE: SUPPORT_STARTERS.copy().union({23048}),
        BuildArchetype.ABILITY_BASED_WARRIOR: SOLO_STARTERS,
        BuildArchetype.HEALER_WARRIOR: SOLO_STARTERS,
        BuildArchetype.AUTO_ATTACK_WARRIOR: {
            19492,  # Death's Toll
        },
        BuildArchetype.JUNGLE_WARRIOR: JUNGLE_STARTERS,
        BuildArchetype.SUPPORT_WARRIOR: SUPPORT_STARTERS,
    }

    # Used for processing input to determine what stats to optimize for
    COLLOQUIAL_MAPPINGS: Dict[str, Set[ItemAttribute]] = {
        "crit": {
            ItemAttribute.CRITICAL_STRIKE_CHANCE,
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE,
        },
        "ccr": {ItemAttribute.CROWD_CONTROL_REDUCTION},
        "cdr": {ItemAttribute.COOLDOWN_REDUCTION},
        "power": {ItemAttribute.MAGICAL_POWER, ItemAttribute.PHYSICAL_POWER},
        "speed": {ItemAttribute.MOVEMENT_SPEED},
        "lifesteal": {
            ItemAttribute.MAGICAL_LIFESTEAL,
            ItemAttribute.PHYSICAL_LIFESTEAL,
        },
        "protection": {
            ItemAttribute.MAGICAL_PROTECTION,
            ItemAttribute.PHYSICAL_PROTECTION,
        },
        "prots": {ItemAttribute.MAGICAL_PROTECTION, ItemAttribute.PHYSICAL_PROTECTION},
        "prot": {ItemAttribute.MAGICAL_PROTECTION, ItemAttribute.PHYSICAL_PROTECTION},
        "pen": {ItemAttribute.MAGICAL_PENETRATION, ItemAttribute.PHYSICAL_PENETRATION},
        "penetration": {
            ItemAttribute.MAGICAL_PENETRATION,
            ItemAttribute.PHYSICAL_PENETRATION,
        },
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
        GodType.MAGICAL: {
            ItemAttribute.MAGICAL_LIFESTEAL,
            ItemAttribute.MAGICAL_PENETRATION,
            ItemAttribute.MAGICAL_POWER,
        },
        GodType.PHYSICAL: {
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE,
            ItemAttribute.PHYSICAL_LIFESTEAL,
            ItemAttribute.PHYSICAL_PENETRATION,
            ItemAttribute.PHYSICAL_POWER,
        },
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

    __archetype_passive_wishlist: Dict[BuildArchetype, Set[PassiveAttribute]]
    __archetype_passive_denylist: Dict[BuildArchetype, Set[PassiveAttribute]]
    __archetype_stat_targets: Dict[BuildArchetype, Dict[ItemAttribute, float]]
    __archetype_weight_mappings: Dict[BuildArchetype, Dict[ItemAttribute, float]]

    god: God
    valid_items: List[Item]
    __all_items: Dict[int, Item]
    __item_scores: Dict[int, float]
    __stat: Union[ItemAttribute, Set[ItemAttribute]] = None
    __level_20_stats: Dict[ItemAttribute, float]
    __current_archetype: BuildArchetype

    def __init__(
        self,
        god: God,
        valid_items: List[Item],
        all_items: Dict[int, Item],
        stat: str = None,
        balance: float = None,
        context: "team_context.TeamContext" = None,
    ):
        self.god = god
        self.valid_items = valid_items
        self.__all_items = all_items
        archetype = None
        if god.id in self.GOD_ID_ARCHETYPE_MAPPINGS:
            archetype = self.GOD_ID_ARCHETYPE_MAPPINGS[god.id]
        self.__current_archetype = (
            BuildArchetype.default_archetype(self.god.role)
            if archetype is None
            else archetype
        )
        self.__init_archetype_passive_denylist()
        self.__init_archetype_passive_wishlist()
        self.__init_archetype_stat_targets()
        self.__init_archetype_weight_mappings()
        # SUPPORT_ASSASSIN, MID_ASSASSIN and MID_GUARDIAN are declared but given
        # neither stat targets nor weights, so a god mapped to one would raise a
        # KeyError deep inside the search rather than at construction. Nothing
        # maps to them today; this keeps that true if something ever does.
        if (
            self.__current_archetype not in self.__archetype_stat_targets
            or self.__current_archetype not in self.__archetype_weight_mappings
        ):
            self.__current_archetype = BuildArchetype.default_archetype(self.god.role)
        # An explicit ask wins; otherwise the archetype's own default, which is
        # None for everything that was already building correctly.
        self.balance = (
            balance
            if balance is not None
            else BRUISER_ARCHETYPE_BALANCE.get(self.__current_archetype)
        )
        self.context = context or team_context.TeamContext()
        # Passives a build must carry, on top of whatever the archetype wants.
        self.__required_passives: Set[PassiveAttribute] = set()
        if balance is None and self.context.allied_tanks and self.balance is not None:
            # A second front line is worth less than the first, so a bruiser
            # behind a support tilts a little further toward damage. Only ever
            # downward, and never when a balance was asked for outright.
            self.balance = max(
                0.0, self.balance - min(self.context.allied_tanks, 2) * 0.10
            )
        self.__balance_stat_targets()
        self.__aim_at_the_lobby()
        self.__init_level_20_stats()
        self.__item_stats_cache: Dict[int, _Stats] = {}
        self.__item_scores = {}
        if stat is not None:
            self.__init_stat(stat.lower())

    def __init_stat(self, stat_name: str):
        try:
            self.__stat = ItemAttribute(stat_name)
        except ValueError:
            if stat_name in self.COLLOQUIAL_MAPPINGS:
                self.__stat = self.COLLOQUIAL_MAPPINGS[stat_name]
            else:
                raise
        if (
            isinstance(self.__stat, ItemAttribute)
            and self.__stat.god_type is not None
            and self.__stat.god_type != self.god.type
        ):
            raise ValueError(
                self.__stat.display_name, " is not a valid stat for ", self.god.name
            )
        if self.__current_archetype in self.__archetype_stat_targets:
            stat_targets = self.__archetype_stat_targets[self.__current_archetype]
            for stat in stat_targets:
                if stat in self.__stat:
                    if stat in (
                        ItemAttribute.ATTACK_SPEED,
                        ItemAttribute.MOVEMENT_SPEED,
                    ):
                        stat_targets[stat] = 1
                        continue
                    if stat in (
                        ItemAttribute.MAGICAL_PENETRATION,
                        ItemAttribute.PHYSICAL_PENETRATION,
                    ):
                        stat_targets[stat] = (
                            self.FLAT_ITEM_ATTRIBUTE_CAPS[stat],
                            self.PERCENT_ITEM_ATTRIBUTE_CAPS[stat],
                        )
                        continue
                    stat_targets[stat] = (
                        self.FLAT_ITEM_ATTRIBUTE_CAPS[stat]
                        if stat in self.FLAT_ITEM_ATTRIBUTE_CAPS
                        else self.PERCENT_ITEM_ATTRIBUTE_CAPS[stat]
                        if stat in self.PERCENT_ITEM_ATTRIBUTE_CAPS
                        else stat_targets[stat]
                    )
                    continue
                stat_targets[stat] = 0

    def __init_archetype_passive_denylist(self):
        jungle = {
            PassiveAttribute.AURA,
            PassiveAttribute.ENEMY_STAT_REDUCTION_AURA,
            PassiveAttribute.ENEMY_STRUCTURE_REDUCTION,
            PassiveAttribute.DECREASES_RELIC_COOLDOWNS,
            PassiveAttribute.DAMAGE_SCALES_FROM_PROTECTIONS,
            PassiveAttribute.BLOCK_STACKS,
            PassiveAttribute.ALLOWS_OVERCAPPING_ATTACK_SPEED,
            PassiveAttribute.INCREASED_PROJECTILE_SPEED,
            PassiveAttribute.EVOLVES_WITH_MINION_KILLS,
            PassiveAttribute.AREA_OF_EFFECT_BASIC_ATTACKS,
        }
        support = {
            PassiveAttribute.BASIC_ATTACK_PROC,
            PassiveAttribute.PERCENT_DAMAGE,
            PassiveAttribute.EVOLVES_WITH_GOD_KILLS,
        }
        solo = {
            PassiveAttribute.EVOLVES_WITH_GOD_KILLS,
            PassiveAttribute.EVOLVES_WITH_ASSISTS,
            PassiveAttribute.BASIC_ATTACK_PROC,
        }
        carry = {
            PassiveAttribute.AURA,
            PassiveAttribute.ENEMY_STAT_REDUCTION_AURA,
            PassiveAttribute.ENEMY_STRUCTURE_REDUCTION,
            PassiveAttribute.INCREASES_HEALING,
            PassiveAttribute.ABILITY_HEALING,
            PassiveAttribute.DECREASES_RELIC_COOLDOWNS,
            PassiveAttribute.INCREASED_PROJECTILE_SPEED,
            PassiveAttribute.ALLIED_GODS_CAN_CRITICAL_HIT,
            PassiveAttribute.PERCENT_DAMAGE,
            PassiveAttribute.ABILITY_PROC,
        }
        mid = {
            PassiveAttribute.AURA,
            PassiveAttribute.ENEMY_STAT_REDUCTION_AURA,
            PassiveAttribute.ENEMY_STRUCTURE_REDUCTION,
            PassiveAttribute.ALLIED_GODS_BUFF_AURA,
        }

        jungle_warrior = auto_assassin = jungle.copy().difference(
            {
                PassiveAttribute.ALLOWS_OVERCAPPING_ATTACK_SPEED,
                PassiveAttribute.AREA_OF_EFFECT_BASIC_ATTACKS,
            }
        )
        jungle_mage = ability_hunter = (
            jungle.copy()
            .difference({PassiveAttribute.EVOLVES_WITH_MINION_KILLS})
            .union({PassiveAttribute.BASIC_ATTACK_PROC})
        )
        defaults = {
            BuildArchetype.ABILITY_BASED_ASSASSIN: jungle,
            BuildArchetype.AUTO_ATTACK_ASSASSIN: auto_assassin,
            BuildArchetype.AUTO_ATTACK_WITH_CRIT_ASSASSIN: auto_assassin,
            BuildArchetype.SUPPORT_ASSASSIN: support,
            BuildArchetype.SOLO_ASSASSIN: solo,
            BuildArchetype.MID_ASSASSIN: jungle.copy().difference(
                {PassiveAttribute.EVOLVES_WITH_MINION_KILLS}
            ),
            BuildArchetype.SUPPORT_GUARDIAN: support,
            BuildArchetype.SOLO_GUARDIAN: solo,
            BuildArchetype.MID_GUARDIAN: mid,
            BuildArchetype.HEALER_GUARDIAN: support,
            BuildArchetype.CARRY_HUNTER: carry,
            BuildArchetype.ABILITY_BASED_HUNTER: ability_hunter,
            BuildArchetype.ATTACK_SPEED_STIM_HUNTER: carry,
            BuildArchetype.MID_MAGE: mid,
            BuildArchetype.JUNGLE_MAGE: jungle_mage,
            BuildArchetype.AUTO_ATTACK_MAGE: carry,
            BuildArchetype.HEALER_MAGE: mid,
            BuildArchetype.SUPPORT_MAGE: support,
            BuildArchetype.SOLO_MAGE: solo,
            BuildArchetype.LIFESTEAL_MID_MAGE: mid,
            BuildArchetype.ABILITY_BASED_WARRIOR: solo,
            BuildArchetype.AUTO_ATTACK_WARRIOR: solo.copy().difference(
                {PassiveAttribute.BASIC_ATTACK_PROC}
            ),
            BuildArchetype.SUPPORT_WARRIOR: support,
            BuildArchetype.JUNGLE_WARRIOR: jungle_warrior,
            BuildArchetype.HEALER_WARRIOR: solo,
        }

        if (
            GodPro.HIGH_SUSTAIN not in self.god.pros
            and self.__current_archetype in defaults
        ):
            defaults[self.__current_archetype].add(
                PassiveAttribute.ALLIED_GODS_BUFF_ON_HEAL
            )
            defaults[self.__current_archetype].add(PassiveAttribute.INCREASES_HEALING)
            defaults[self.__current_archetype].add(PassiveAttribute.SELF_BUFF_ON_HEAL)

        self.__archetype_passive_denylist = defaults

    def __init_archetype_passive_wishlist(self):
        jungle = {
            PassiveAttribute.ABILITY_PROC,
            PassiveAttribute.ALLOWS_OVERCAPPING_PENETRATION_WITH_FIRST_ABILITY,
            PassiveAttribute.ANTIHEAL,
            PassiveAttribute.FLAT_TRUE_BONUS_DAMAGE,
            PassiveAttribute.PERCENT_DAMAGE,
            PassiveAttribute.DECREASES_ABILITY_COOLDOWNS,
            PassiveAttribute.ABILITY_HEALING,
            PassiveAttribute.IMMUNE_TO_CC,
            PassiveAttribute.INCREASES_JUNGLE_MONSTER_DAMAGE,
            PassiveAttribute.IN_JUNGLE_EFFECT,
            PassiveAttribute.SCALING_BONUS_DAMAGE,
            PassiveAttribute.ULTIMATE_PROC,
        }
        support = {
            PassiveAttribute.AURA,
            PassiveAttribute.ENEMY_STAT_REDUCTION_AURA,
            PassiveAttribute.ENEMY_STRUCTURE_REDUCTION,
            PassiveAttribute.ALLIED_GODS_BUFF_AURA,
            PassiveAttribute.ANTIHEAL,
            PassiveAttribute.INCREASES_WITH_MISSING_STAT,
            PassiveAttribute.PERCENT_STAT_CONVERTED,
            PassiveAttribute.SHIELD,
            PassiveAttribute.IMMUNE_TO_SLOWS,
            PassiveAttribute.DECREASES_RELIC_COOLDOWNS,
            PassiveAttribute.INCREASES_ENEMY_DAMAGE_WHEN_CC,
            PassiveAttribute.DAMAGE_MITIGATION,
            PassiveAttribute.DAMAGE_SCALES_FROM_PROTECTIONS,
            PassiveAttribute.BLOCK_STACKS,
            PassiveAttribute.DECREASES_CRITICAL_DAMAGE_TAKEN,
            PassiveAttribute.CAUSES_CC,
            PassiveAttribute.TRIGGERED_BY_CC,
            PassiveAttribute.IMMUNE_TO_CC,
            PassiveAttribute.DAMAGING_AURA,
            PassiveAttribute.EVOLVES_WITH_ASSISTS,
            PassiveAttribute.ALLIED_STRUCTURES_BUFF,
        }
        carry = {
            PassiveAttribute.BASIC_ATTACK_PROC,
            PassiveAttribute.ALLOWS_OVERCAPPING_ATTACK_SPEED,
            PassiveAttribute.INCREASES_CRITICAL_DAMAGE,
            PassiveAttribute.STRIPS_PROTECTIONS,
            PassiveAttribute.CRITICAL_HIT_EFFECT,
            PassiveAttribute.EVOLVES_WITH_MINION_KILLS,
        }
        mid = {
            PassiveAttribute.ABILITY_PROC,
            PassiveAttribute.ALLOWS_OVERCAPPING_PENETRATION_WITH_FIRST_ABILITY,
            PassiveAttribute.TRIGGERS_HEAL,
            PassiveAttribute.INCREASES_WITH_MISSING_STAT,
            PassiveAttribute.PERCENT_DAMAGE,
            PassiveAttribute.PERCENT_STAT_CONVERTED,
            PassiveAttribute.ULTIMATE_PROC,
            PassiveAttribute.SCALING_BONUS_DAMAGE,
            PassiveAttribute.INCREASE_DAMAGE_BELOW_TARGET_THRESHOLD,
        }
        solo = {
            PassiveAttribute.AURA,
            PassiveAttribute.ENEMY_STAT_REDUCTION_AURA,
            PassiveAttribute.ENEMY_STRUCTURE_REDUCTION,
            PassiveAttribute.ABILITY_PROC,
            PassiveAttribute.ALLIED_GODS_BUFF_AURA,
            PassiveAttribute.BELOW_THRESHOLD_BUFF,
            PassiveAttribute.ANTIHEAL,
            PassiveAttribute.INCREASES_WITH_MISSING_STAT,
            PassiveAttribute.PERCENT_DAMAGE,
            PassiveAttribute.PERCENT_STAT_CONVERTED,
            PassiveAttribute.SHIELD,
            PassiveAttribute.IMMUNE_TO_SLOWS,
            PassiveAttribute.ABILITY_HEALING,
            PassiveAttribute.INCREASES_ENEMY_DAMAGE_WHEN_CC,
            PassiveAttribute.EFFECT_VARIES_BY_CURRENT_STATS,
            PassiveAttribute.DAMAGE_SCALES_FROM_PROTECTIONS,
            PassiveAttribute.DAMAGE_MITIGATION,
            PassiveAttribute.BLOCK_STACKS,
            PassiveAttribute.DECREASES_CRITICAL_DAMAGE_TAKEN,
            PassiveAttribute.CAUSES_CC,
            PassiveAttribute.TRIGGERED_BY_CC,
            PassiveAttribute.IMMUNE_TO_CC,
            PassiveAttribute.ALLIED_STRUCTURES_BUFF,
            PassiveAttribute.DAMAGING_AURA,
        }
        auto_assassin = {
            PassiveAttribute.BASIC_ATTACK_PROC,
            PassiveAttribute.ANTIHEAL,
            PassiveAttribute.INCREASES_JUNGLE_MONSTER_DAMAGE,
            PassiveAttribute.ULTIMATE_PROC,
            PassiveAttribute.ALLOWS_OVERCAPPING_ATTACK_SPEED,
            PassiveAttribute.STRIPS_PROTECTIONS,
            PassiveAttribute.IN_JUNGLE_EFFECT,
        }
        auto_assassin_crit = auto_assassin.copy().union(
            {
                PassiveAttribute.INCREASES_CRITICAL_DAMAGE,
                PassiveAttribute.CRITICAL_HIT_EFFECT,
                PassiveAttribute.AREA_OF_EFFECT_BASIC_ATTACKS,
            }
        )
        mid_assassin = jungle.copy().difference(
            {
                PassiveAttribute.INCREASES_JUNGLE_MONSTER_DAMAGE,
                PassiveAttribute.IN_JUNGLE_EFFECT,
                PassiveAttribute.EVOLVES_WITH_MINION_KILLS,
            }
        )
        ability_hunter = {
            PassiveAttribute.ABILITY_PROC,
            PassiveAttribute.EVOLVES_WITH_MINION_KILLS,
            PassiveAttribute.ANTIHEAL,
            PassiveAttribute.ABILITY_HEALING,
            PassiveAttribute.ULTIMATE_PROC,
            PassiveAttribute.SCALING_BONUS_DAMAGE,
        }
        jungle_mage = mid.copy().union(
            {
                PassiveAttribute.INCREASES_JUNGLE_MONSTER_DAMAGE,
                PassiveAttribute.IN_JUNGLE_EFFECT,
                PassiveAttribute.BASIC_ATTACK_PROC,
            }
        )
        auto_mage = {
            PassiveAttribute.STACKS,
            PassiveAttribute.INCREASES_WITH_MISSING_STAT,
            PassiveAttribute.PERCENT_STAT_CONVERTED,
            PassiveAttribute.SCALING_BONUS_DAMAGE,
            PassiveAttribute.INCREASE_DAMAGE_BELOW_TARGET_THRESHOLD,
            PassiveAttribute.STRIPS_PROTECTIONS,
            PassiveAttribute.BASIC_ATTACK_PROC,
        }
        solo_mage = solo.copy().union(
            {
                PassiveAttribute.EVOLVES_WITH_MINION_KILLS,
                PassiveAttribute.TRIGGERS_HEAL,
                PassiveAttribute.INCREASE_DAMAGE_BELOW_TARGET_THRESHOLD,
            }
        )
        defaults = {
            BuildArchetype.ABILITY_BASED_ASSASSIN: jungle,
            BuildArchetype.AUTO_ATTACK_ASSASSIN: auto_assassin,
            BuildArchetype.AUTO_ATTACK_WITH_CRIT_ASSASSIN: auto_assassin_crit,
            BuildArchetype.SUPPORT_ASSASSIN: support,
            BuildArchetype.SOLO_ASSASSIN: mid_assassin.union(solo),
            BuildArchetype.MID_ASSASSIN: mid_assassin,
            BuildArchetype.SUPPORT_GUARDIAN: support,
            BuildArchetype.SOLO_GUARDIAN: solo,
            BuildArchetype.MID_GUARDIAN: mid,
            BuildArchetype.HEALER_GUARDIAN: support,
            BuildArchetype.CARRY_HUNTER: carry,
            BuildArchetype.ABILITY_BASED_HUNTER: ability_hunter,
            BuildArchetype.ATTACK_SPEED_STIM_HUNTER: carry,
            BuildArchetype.MID_MAGE: mid,
            BuildArchetype.JUNGLE_MAGE: jungle_mage,
            BuildArchetype.AUTO_ATTACK_MAGE: auto_mage,
            BuildArchetype.HEALER_MAGE: mid,
            BuildArchetype.SOLO_MAGE: solo_mage,
            BuildArchetype.SUPPORT_MAGE: support,
            BuildArchetype.LIFESTEAL_MID_MAGE: mid.copy().union(
                {PassiveAttribute.INCREASES_LIFESTEAL}
            ),
            BuildArchetype.ABILITY_BASED_WARRIOR: solo,
            BuildArchetype.AUTO_ATTACK_WARRIOR: solo,
            BuildArchetype.SUPPORT_WARRIOR: support,
            BuildArchetype.JUNGLE_WARRIOR: auto_assassin,
            BuildArchetype.HEALER_WARRIOR: solo,
        }

        if (
            GodPro.HIGH_SUSTAIN in self.god.pros
            and self.__current_archetype in defaults
        ):
            defaults[self.__current_archetype].add(
                PassiveAttribute.ALLIED_GODS_BUFF_ON_HEAL
            )
            defaults[self.__current_archetype].add(PassiveAttribute.INCREASES_HEALING)
            defaults[self.__current_archetype].add(PassiveAttribute.SELF_BUFF_ON_HEAL)

        self.__archetype_passive_wishlist = defaults

    def __init_archetype_stat_targets(self):
        self.__archetype_stat_targets = {
            BuildArchetype.ABILITY_BASED_ASSASSIN: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.20,
                ItemAttribute.MANA: 200,
                ItemAttribute.PHYSICAL_PENETRATION: (10, 0.30),
                ItemAttribute.PHYSICAL_POWER: 200,
            },
            BuildArchetype.AUTO_ATTACK_ASSASSIN: {
                ItemAttribute.ATTACK_SPEED: 0.70,
                ItemAttribute.MOVEMENT_SPEED: 0.20,
                ItemAttribute.PHYSICAL_LIFESTEAL: 0.10,
                ItemAttribute.PHYSICAL_PENETRATION: (0, 0.20),
                ItemAttribute.PHYSICAL_POWER: 180,
            },
            BuildArchetype.AUTO_ATTACK_WITH_CRIT_ASSASSIN: {
                ItemAttribute.ATTACK_SPEED: 0.40,
                ItemAttribute.CRITICAL_STRIKE_CHANCE: 0.75,
                ItemAttribute.MOVEMENT_SPEED: 0.07,
                ItemAttribute.PHYSICAL_PENETRATION: (0, 0.20),
                ItemAttribute.PHYSICAL_POWER: 195,
                ItemAttribute.PHYSICAL_PROTECTION: 30,
            },
            BuildArchetype.SOLO_ASSASSIN: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.30,
                ItemAttribute.HEALTH: 150,
                ItemAttribute.MANA: 1000,
                ItemAttribute.PHYSICAL_POWER: 150,
            },
            BuildArchetype.SUPPORT_GUARDIAN: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.30,
                ItemAttribute.HEALTH: 950,
                ItemAttribute.MAGICAL_PROTECTION: 155,
                ItemAttribute.PHYSICAL_PROTECTION: 185,
            },
            BuildArchetype.SOLO_GUARDIAN: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.20,
                ItemAttribute.CROWD_CONTROL_REDUCTION: 0.20,
                ItemAttribute.HEALTH: 800,
                ItemAttribute.MAGICAL_POWER: 100,
                ItemAttribute.MAGICAL_PROTECTION: 130,
                ItemAttribute.MANA: 400,
                ItemAttribute.MP5: 25,
                ItemAttribute.PHYSICAL_PROTECTION: 140,
            },
            BuildArchetype.CARRY_HUNTER: {
                ItemAttribute.ATTACK_SPEED: 0.70,
                ItemAttribute.PHYSICAL_LIFESTEAL: 0.15,
                ItemAttribute.PHYSICAL_PENETRATION: (15, 0),
                ItemAttribute.PHYSICAL_POWER: 180,
                ItemAttribute.CRITICAL_STRIKE_CHANCE: 0.45,
            },
            BuildArchetype.ABILITY_BASED_HUNTER: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.10,
                ItemAttribute.HEALTH: 100,
                ItemAttribute.MANA: 1000,
                ItemAttribute.PHYSICAL_PENETRATION: (10, 0.20),
                ItemAttribute.PHYSICAL_POWER: 285,
            },
            BuildArchetype.MID_MAGE: {
                ItemAttribute.COOLDOWN_REDUCTION: 0.30,
                ItemAttribute.MAGICAL_PENETRATION: (25, 0.30),
                ItemAttribute.MAGICAL_POWER: 420,
                ItemAttribute.MANA: 300,
                ItemAttribute.MP5: 40,
            },
            BuildArchetype.LIFESTEAL_MID_MAGE: {
                ItemAttribute.MAGICAL_LIFESTEAL: 0.45,
                ItemAttribute.MAGICAL_PENETRATION: (10, 0.30),
                ItemAttribute.MAGICAL_POWER: 575,
                ItemAttribute.MANA: 1550,
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
                ItemAttribute.MAGICAL_PENETRATION: (25, 0.30),
                ItemAttribute.MAGICAL_POWER: 560,
                ItemAttribute.MANA: 1300,
                ItemAttribute.MP5: 40,
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
                ItemAttribute.HEALTH: 500,
                ItemAttribute.MAGICAL_PROTECTION: 120,
                ItemAttribute.MANA: 150,
                ItemAttribute.PHYSICAL_PROTECTION: 150,
            },
            BuildArchetype.AUTO_ATTACK_WARRIOR: {
                ItemAttribute.ATTACK_SPEED: 0.30,
                ItemAttribute.COOLDOWN_REDUCTION: 0.10,
                ItemAttribute.HEALTH: 500,
                ItemAttribute.HP5: 10,
                ItemAttribute.MAGICAL_PROTECTION: 100,
                ItemAttribute.MP5: 20,
                ItemAttribute.PHYSICAL_POWER: 50,
                ItemAttribute.PHYSICAL_PROTECTION: 120,
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

        self.__archetype_stat_targets[
            BuildArchetype.HEALER_WARRIOR
        ] = self.__archetype_stat_targets[BuildArchetype.ABILITY_BASED_WARRIOR].copy()
        self.__archetype_stat_targets[
            BuildArchetype.HEALER_GUARDIAN
        ] = self.__archetype_stat_targets[BuildArchetype.SUPPORT_GUARDIAN].copy()

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
                ItemAttribute.PHYSICAL_LIFESTEAL: -0.5,
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
                ItemAttribute.CROWD_CONTROL_REDUCTION: 0.5,
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
        defaults[BuildArchetype.HEALER_GUARDIAN] = defaults[
            BuildArchetype.SUPPORT_GUARDIAN
        ].copy()

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
        lifesteal_mage[ItemAttribute.MAGICAL_LIFESTEAL] = 10
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
        defaults[BuildArchetype.SUPPORT_MAGE] = defaults[
            BuildArchetype.SUPPORT_GUARDIAN
        ].copy()

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
        defaults[BuildArchetype.HEALER_WARRIOR] = defaults[
            BuildArchetype.ABILITY_BASED_WARRIOR
        ].copy()
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

    def __init_level_20_stats(self):
        self.__level_20_stats = {}
        for attr in list(ItemAttribute):
            self.__level_20_stats[attr] = self.god.get_stat_at_level(attr, 20)

    def __compute_item_score(
        self, item: Item, weights: Dict[ItemAttribute, float]
    ) -> float:
        build_stats = self.compute_build_stats([item])
        score = self.__compute_properties_score(build_stats, weights)
        return score + self.passive_score(item)

    def passive_score(self, item: Item) -> float:
        """What this item's passive adds, on top of its stat line.

        An evolved item often carries no passive of its own and inherits its
        parent's, which is why the parent is consulted — scoring Evolved Book of
        Thoth as though it had no passive is exactly the mistake this exists to
        fix.
        """
        passives = set(item.passive_properties or set())
        if item.tier == 4 and item.parent_item_id in self.__all_items:
            parent = self.__all_items[item.parent_item_id]
            passives |= set(parent.passive_properties or set())
        if not passives:
            return 0.0
        return PASSIVE_DISCOUNT * sum(
            PASSIVE_VALUE.get(passive, 0.0) for passive in passives
        )

    def __compute_properties_score(
        self,
        build_stats: Dict[ItemAttribute, ItemProperty],
        weights: Dict[ItemAttribute, float],
    ) -> float:
        score = 0
        for attr, prop in build_stats.items():
            flat_weight = pct_weight = weights[attr]
            if attr in (
                ItemAttribute.PHYSICAL_PENETRATION,
                ItemAttribute.MAGICAL_PENETRATION,
            ):
                pct_weight = weights[attr][0]
                flat_weight = weights[attr][1]
            if prop.flat_value > 0:
                level_20_stat = self.__level_20_stats[attr]
                score += (
                    prop.flat_value
                    / (self.FLAT_ITEM_ATTRIBUTE_CAPS[attr] - level_20_stat)
                ) * flat_weight
            if prop.percent_value > 0:
                if attr in (ItemAttribute.ATTACK_SPEED, ItemAttribute.MOVEMENT_SPEED):
                    level_20_stat = self.__level_20_stats[attr]
                    score += (
                        (level_20_stat + (level_20_stat * prop.percent_value))
                        / self.FLAT_ITEM_ATTRIBUTE_CAPS[attr]
                    ) * pct_weight
                    continue
                score += (
                    prop.percent_value / self.PERCENT_ITEM_ATTRIBUTE_CAPS[attr]
                ) * pct_weight
        return score

    def __item_stats(self, item: Item) -> _Stats:
        """One item's contribution, computed once and kept.

        `optimize` asks for this hundreds of thousands of times across a search
        — the same few dozen items, over and over — and rebuilding it from the
        item's properties each time was the single largest cost in the walk.
        """
        cached = self.__item_stats_cache.get(item.id)
        if cached is None:
            cached = BuildStatCalculator(
                GodBuild(self.god, [item], 20)
            ).calculate_item_stats(item)
            self.__item_stats_cache[item.id] = cached
        return cached

    def __build_stats(self, build) -> _Stats:
        """The cached per-item stats of a build, summed into a fresh total.

        Fresh on purpose. `_Stats.merge` adopts the other side's `_Penetration`
        object rather than copying it when it holds none of its own, so merging
        straight out of the cache would leave two builds sharing one mutable
        object and the second would read the first's penetration.
        """
        total = _Stats()
        for item in build:
            source = self.__item_stats(item)
            for stat, value in source.stats.items():
                if isinstance(value, _Penetration):
                    value = _Penetration(value.flat, value.percent)
                total.add_or_set_stat(stat, value)
        return total

    def __target_vectors(
        self, pool: List[Item], stat_targets: Dict[ItemAttribute, float] = None
    ):
        """The stat targets as arithmetic: one row per item, one goal vector.

        A pre-filter, not a replacement. Summing six short rows and comparing
        against a vector settles the stat targets for a combination in about a
        microsecond, and only the fraction that clears every one of them — half
        a percent, typically — needs the real check, which also has to weigh
        overcapping, the god's own level-20 stats and the passive wishlist and
        is far too intricate to be worth duplicating in two forms that could
        drift apart.

        Penetration occupies two columns, flat and percent, because its target
        is a pair and both halves have to be met.
        """
        stat_targets = (
            stat_targets or self.__archetype_stat_targets[self.__current_archetype]
        )
        columns: List[Tuple[ItemAttribute, str]] = []
        goals: List[float] = []
        for stat, target in stat_targets.items():
            if isinstance(target, tuple):
                columns.append((stat, "flat"))
                goals.append(float(target[0]))
                columns.append((stat, "percent"))
                goals.append(float(target[1]))
            else:
                columns.append((stat, "flat"))
                goals.append(float(target))

        rows: Dict[int, List[float]] = {}
        for item in pool:
            stats = self.__item_stats(item)
            row = []
            for stat, kind in columns:
                if stat not in stats.stats:
                    row.append(0.0)
                    continue
                value = stats.get_stat(stat)
                if isinstance(value, _Penetration):
                    row.append(float(value.flat if kind == "flat" else value.percent))
                else:
                    row.append(float(value) if kind == "flat" else 0.0)
            rows[item.id] = row
        return rows, goals

    def __check_build_on_target(
        self, build: List[Item], stat_targets: Dict[ItemAttribute, float] = None
    ) -> bool:
        stats = self.__build_stats(build)
        stat_targets = (
            stat_targets or self.__archetype_stat_targets[self.__current_archetype]
        )
        for stat in stat_targets:
            if stat in stats.stats:
                value = flat_value = pct_value = stats.get_stat(stat)
                flat_target = pct_target = stat_targets[stat]
                if stat in (
                    ItemAttribute.MAGICAL_PENETRATION,
                    ItemAttribute.PHYSICAL_PENETRATION,
                ):
                    flat_target = flat_target[0]
                    pct_target = pct_target[1]
                    flat_value = 0.1 if value.flat == 0 else value.flat
                    pct_value = 0.01 if value.percent == 0 else value.percent
                if 0 <= float(f"{flat_value:.2f}") < float(f"{flat_target:.2f}"):
                    return False
                if 0 <= float(f"{pct_value:.2f}") < float(f"{pct_target:.2f}"):
                    return False
            else:
                return False
        all_passives = {
            passive for item in build for passive in item.passive_properties
        }

        # Evolutions (sometimes) don't have a passive so add passive properties
        # from their parents
        evos = list(filter(lambda item: item.tier == 4, build))
        if any(evos):
            for evo in evos:
                all_passives.union(
                    {
                        passive
                        for passive in self.__all_items[
                            evo.parent_item_id
                        ].passive_properties
                    }
                )
        if self.__check_overcapped(stats.stats, all_passives):
            return False
        if self.__required_passives - all_passives:
            return False
        if self.__current_archetype in self.__archetype_passive_wishlist:
            if (
                len(
                    all_passives
                    & self.__archetype_passive_wishlist[self.__current_archetype]
                )
                < 1
            ):
                return False
        return True

    def __check_overcapped(
        self,
        stats: Dict[ItemAttribute, float | _Penetration],
        passives: Set[PassiveAttribute],
    ) -> bool:
        for attr, prop in stats.items():
            if attr in self.FLAT_ITEM_ATTRIBUTE_CAPS:
                # Not `isinstance(prop, float)`: an integer stat value is
                # neither a float nor a _Penetration, so that spelling took
                # the else branch and asked an int for `.flat`. It only
                # surfaced when a build carrying one reached this check.
                flat_value = prop.flat if isinstance(prop, _Penetration) else prop
                if attr == ItemAttribute.ATTACK_SPEED:
                    attack_speed = self.god.get_stat_at_level(attr, 20)
                    flat_value = attack_speed + attack_speed * prop
                elif attr not in (
                    ItemAttribute.MAGICAL_PENETRATION,
                    ItemAttribute.PHYSICAL_PENETRATION,
                ):
                    flat_value += self.god.get_stat_at_level(attr, 20)
                flat_cap = self.FLAT_ITEM_ATTRIBUTE_CAPS[attr]
                if float(f"{flat_value:.2f}") > float(f"{flat_cap:.2f}"):
                    if attr in (
                        ItemAttribute.MAGICAL_POWER,
                        ItemAttribute.PHYSICAL_POWER,
                    ):
                        # Soft cap for these two
                        return False
                    if attr in (
                        ItemAttribute.MAGICAL_PENETRATION,
                        ItemAttribute.PHYSICAL_PENETRATION,
                    ):
                        if (
                            PassiveAttribute.ALLOWS_OVERCAPPING_PENETRATION_WITH_FIRST_ABILITY
                            in passives
                        ):
                            continue
                    return True
            if attr in self.PERCENT_ITEM_ATTRIBUTE_CAPS:
                pct_value = (
                    prop.percent if isinstance(prop, _Penetration) else prop
                )
                pct_cap = self.PERCENT_ITEM_ATTRIBUTE_CAPS[attr]
                if attr == ItemAttribute.COOLDOWN_REDUCTION:
                    if self.god.role == GodRole.WARRIOR:
                        pct_value += self.god.get_stat_at_level(attr, 20)
                    if PassiveAttribute.INCREASES_COOLDOWN_CAP in passives:
                        pct_cap = 0.50
                if (
                    attr == ItemAttribute.CROWD_CONTROL_REDUCTION
                    and self.god.role == GodRole.GUARDIAN
                ):
                    pct_value += self.god.get_stat_at_level(attr, 20)
                if float(f"{pct_value:.2f}") > float(f"{pct_cap:.2f}"):
                    if attr == ItemAttribute.ATTACK_SPEED:
                        if PassiveAttribute.ALLOWS_OVERCAPPING_ATTACK_SPEED in passives:
                            continue
                    return True
        return False

    def __get_weights(self) -> Dict[ItemAttribute, float]:
        inverted_type = (
            GodType.PHYSICAL if self.god.type != GodType.PHYSICAL else GodType.MAGICAL
        )
        self_stat: Set[ItemAttribute] = (
            set()
            if self.__stat is None
            else self.__stat
            if isinstance(self.__stat, set)
            else set([self.__stat])
        )
        stats_to_optimize: Set[ItemAttribute] = self_stat.difference(
            self.GOD_TYPE_MAPPINGS[inverted_type]
        )
        god_weights = self.__archetype_weight_mappings[self.__current_archetype]
        if any(stats_to_optimize):
            for stat in stats_to_optimize:
                if stat in (
                    ItemAttribute.PHYSICAL_PENETRATION,
                    ItemAttribute.MAGICAL_PENETRATION,
                ):
                    god_weights[stat] = (15, 15)
                    continue
                god_weights[stat] = 15
        return self.__balanced(self.__by_pros(god_weights))

    def __by_pros(
        self, weights: Dict[ItemAttribute, float]
    ) -> Dict[ItemAttribute, float]:
        """The archetype's weights, nudged by what this god is described as.

        Added to rather than multiplied, so a pro can raise a stat the archetype
        rated at zero — a sustain warrior wants lifesteal whether or not
        ABILITY_BASED_WARRIOR asked for any.

        A weight of None means the attribute is the wrong half of a
        physical/magical pair for this god and is left alone; that is what None
        has always meant in these tables, and overwriting it would hand a mage
        physical power.
        """
        pros = getattr(self.god, "pros", None) or []
        if not pros:
            return weights

        out = dict(weights)
        for pro in pros:
            for key, bonus in PRO_EMPHASIS.get(pro, {}).items():
                attribute = key
                if isinstance(key, str):
                    attribute = _BY_GOD_TYPE[key].get(self.god.type)
                    if attribute is None:
                        continue
                current = out.get(attribute)
                if current is None and attribute in out:
                    continue
                if isinstance(current, tuple):
                    out[attribute] = tuple(part + bonus for part in current)
                else:
                    out[attribute] = (current or 0.0) + bonus
        return out

    def __balance_stat_targets(self) -> None:
        """Relax the defensive floor a bruiser is held to.

        Weighting alone could not produce a bruiser. The stat targets are a
        hard filter — a warrior had to reach 500 health, 150 physical and 120
        magical protection before a build counted as viable at all — so four of
        its six slots were spent before ranking ever ran, and re-weighting only
        chose which two were left. Asking a bruiser for two thirds of a tank's
        protections is what actually frees the slots.

        Only the current archetype is touched, and only when a balance was
        asked for or defaulted, so every archetype that was already building
        correctly keeps the exact targets it had.
        """
        if self.balance is None:
            return
        targets = self.__archetype_stat_targets.get(self.__current_archetype)
        if not targets:
            return

        defensive = sum(
            1 for stat in targets if stat in DEFENSIVE_STATS
        )
        if not defensive:
            return

        # A tank archetype tilted to 0.5 halves its defensive floor; the scale
        # is relative to the tank end of the range rather than to the
        # archetype's own implied ratio, because a *target* is a floor rather
        # than a share of anything.
        scale = min(max(self.balance, 0.0), 1.0) / _TANK_BALANCE
        for stat in list(targets):
            if stat not in DEFENSIVE_STATS:
                continue
            value = targets[stat]
            targets[stat] = (
                tuple(part * scale for part in value)
                if isinstance(value, tuple)
                else value * scale
            )

    def __with_required_passives(self, pool: List[Item]) -> List[Item]:
        """Make sure the pool can actually satisfy what the lobby demands.

        The search runs over the 28 highest-scoring items, and anti-heal is
        rarely among them: it is a passive, and the scoring only reads stat
        lines, so Contagion and Pestilence lose to items with bigger numbers.
        Requiring a passive no item in the pool has makes every build fail the
        check, and the near-miss fallback then quietly returns builds without
        it — the requirement would look applied and do nothing.

        So the best two carriers of each required passive are added to the pool
        if none is already there. Two rather than one because a single carrier
        may be excluded later as a glyph parent.
        """
        if not self.__required_passives:
            return pool

        out = list(pool)
        for required in self.__required_passives:
            if any(required in (item.passive_properties or set()) for item in out):
                continue
            carriers = [
                item
                for item in self.valid_items
                if required in (item.passive_properties or set())
                and item.tier >= 3
                and item.active
            ]
            carriers.sort(key=lambda item: -self.__item_scores.get(item.id, 0.0))
            out.extend(carriers[:2])
        return out

    def __aim_at_the_lobby(self) -> None:
        """Point the defensive budget at the damage that is actually coming.

        The same three moves as Smite 2, against Smite 1's vocabulary: the
        protection split follows the enemy's damage types, crowd-control
        reduction rises with how much crowd control they bring, and an
        anti-heal item is *required* against a healer rather than merely
        preferred — Smite 1 carries anti-heal as an item passive rather than a
        stat, so the passive wishlist is the only place to ask for it.

        The protection scales average 1.0, so this aims the same budget rather
        than quietly making every build tankier.
        """
        context = self.context
        if not context.known:
            return

        targets = self.__archetype_stat_targets.get(self.__current_archetype)
        if targets:
            physical_scale, magical_scale = team_context.protection_scales(context)
            for stat, scale in (
                (ItemAttribute.PHYSICAL_PROTECTION, physical_scale),
                (ItemAttribute.MAGICAL_PROTECTION, magical_scale),
            ):
                if stat in targets:
                    targets[stat] = targets[stat] * scale

            share = context.crowd_control_share
            if share:
                cap = self.PERCENT_ITEM_ATTRIBUTE_CAPS[
                    ItemAttribute.CROWD_CONTROL_REDUCTION
                ]
                targets[ItemAttribute.CROWD_CONTROL_REDUCTION] = max(
                    targets.get(ItemAttribute.CROWD_CONTROL_REDUCTION, 0.0),
                    cap * share * 0.5,
                )

        if context.wants_anti_heal:
            # Kept apart from the archetype's wishlist rather than added to it.
            # A wishlist is satisfied by *any one* of its passives, so putting
            # anti-heal in it would make it an alternative to what the archetype
            # already wanted instead of a requirement alongside it.
            self.__required_passives.add(PassiveAttribute.ANTIHEAL)

    def __balanced(
        self, weights: Dict[ItemAttribute, float]
    ) -> Dict[ItemAttribute, float]:
        """The archetype's weights, tilted toward damage or toward survival.

        Scaling each side as a whole leaves the relative sizes within it alone,
        so a bruiser asks for the same *kind* of damage its archetype already
        wanted, just more of it against less protection. Stats in neither set —
        cooldowns, mana, movement — are untouched, because a tank and a bruiser
        want those equally.

        Penetration weights are a (flat, percent) pair rather than a number,
        which is why the scaling has to look at what it is holding.
        """
        balance = self.balance
        if balance is None:
            return weights

        def side_total(stats: FrozenSet[ItemAttribute]) -> float:
            total = 0.0
            for stat in stats:
                weight = weights.get(stat)
                if weight is None:
                    continue
                total += sum(weight) if isinstance(weight, tuple) else weight
            return total

        defensive = side_total(DEFENSIVE_STATS)
        offensive = side_total(OFFENSIVE_STATS)
        total = defensive + offensive
        if total <= 0:
            return weights

        current = defensive / total
        wanted = min(max(balance, 0.0), 1.0)
        defensive_scale = (wanted / current) if current > 0.01 else wanted / 0.01
        offensive_scale = (
            ((1 - wanted) / (1 - current)) if current < 0.99 else (1 - wanted) / 0.01
        )

        out: Dict[ItemAttribute, float] = {}
        for stat, weight in weights.items():
            if weight is None:
                out[stat] = weight
                continue
            if stat in DEFENSIVE_STATS:
                scale = defensive_scale
            elif stat in OFFENSIVE_STATS:
                scale = offensive_scale
            else:
                out[stat] = weight
                continue
            out[stat] = (
                tuple(part * scale for part in weight)
                if isinstance(weight, tuple)
                else weight * scale
            )
        return out

    def __compute_scores(
        self, weights: Dict[ItemAttribute, float]
    ) -> Dict[ItemAttribute, float]:
        self.__item_scores = {}
        for item in self.valid_items:
            self.__item_scores[item.id] = self.__compute_item_score(item, weights)

    async def optimize(
        self, stat_targets: Dict[ItemAttribute, float] = None
    ) -> Tuple[List[List[Item]], int]:
        god_weights = self.__get_weights()
        self.__compute_scores(god_weights)
        starters = self.__filter_passive_denylist(self.get_preferred_starters())

        sorted_ids = list(
            [
                id
                for id, _ in sorted(
                    self.__item_scores.items(), key=lambda item: item[1], reverse=True
                )
            ]
        )
        rated_items = [self.__all_items[id] for id in sorted_ids]
        rated_items = self.__filter_passive_denylist(
            self.filter_evolution_parents(self.filter_tiers_with_glyphs(rated_items))
        )
        rated_items = rated_items[: int(min(len(rated_items) + 1, 28))]
        rated_items = self.__with_required_passives(rated_items)

        glyphs = self.get_glyphs(rated_items)
        items = self.filter_tiers(rated_items)
        all_non_glyph_items = self.filter_glyph_parent(items)

        viable_builds: List[List[Item]] = []
        # Best-effort runners-up: how many targets each build did meet, so a
        # search that satisfies nothing still has something to answer with.
        near_misses: List[Tuple[int, List[Item]]] = []
        best_met = -1

        iterations = 0

        pool = list({item.id: item for item in rated_items + starters}.values())
        rows, goals = self.__target_vectors(pool, stat_targets)
        width = len(goals)

        async def check_combinations(
            existing_build: FrozenSet[Item], combo_items: List[Item], size: int
        ) -> int:
            nonlocal best_met
            build_n = 0
            base = [0.0] * width
            for item in existing_build:
                row = rows[item.id]
                base = [a + b for a, b in zip(base, row)]

            for combo in itertools.combinations(combo_items, size):
                build_n += 1
                # Yielding once per combination cost more than the work between
                # yields — an await on a ready coroutine still round-trips the
                # event loop, and there are hundreds of thousands of them. A
                # batch is often enough to keep the bot answering.
                if build_n % _YIELD_EVERY == 0:
                    await asyncio.sleep(0)

                totals = list(base)
                for item in combo:
                    row = rows[item.id]
                    for index in range(width):
                        totals[index] += row[index]

                met = 0
                for index in range(width):
                    if totals[index] >= goals[index]:
                        met += 1

                if met == width:
                    item_build = existing_build.union(frozenset(combo))
                    if self.__check_build_on_target(item_build, stat_targets):
                        viable_builds.append(list(item_build))
                        continue
                elif viable_builds:
                    continue

                if not viable_builds and met >= best_met:
                    if met > best_met:
                        best_met = met
                        near_misses.clear()
                    if len(near_misses) < _NEAR_MISS_LIMIT:
                        near_misses.append(
                            (met, list(existing_build.union(frozenset(combo))))
                        )
            return build_n

        # Five items and a starter.
        for starter in starters:
            starter_build: FrozenSet[Item] = frozenset([starter])
            for glyph in glyphs:
                iterations += await check_combinations(
                    starter_build.union(frozenset([glyph])),
                    self.filter_glyph_parent(items, glyph),
                    4,
                )
            iterations += await check_combinations(
                starter_build, all_non_glyph_items, 5
            )

        # Six items and no starter. The glyph-free case used to be enumerated
        # as "every item, plus five of the others", which produces each
        # six-item set once per member of it — six times over, six times the
        # work, and six copies of every viable build in the result.
        for glyph in glyphs:
            iterations += await check_combinations(
                frozenset([glyph]), self.filter_glyph_parent(items, glyph), 5
            )
        iterations += await check_combinations(frozenset(), all_non_glyph_items, 6)

        if not viable_builds and near_misses and self.__required_passives:
            # A near miss may still be a build without the anti-heal the lobby
            # called for. Prefer the ones that carry what was required, and only
            # fall back to the rest if nothing does.
            carrying = [
                (met, build)
                for met, build in near_misses
                if not self.__required_passives
                - {
                    passive
                    for item in build
                    for passive in (item.passive_properties or set())
                }
            ]
            if carrying:
                near_misses = carrying

        if not viable_builds and near_misses:
            # Nothing met every target. That is the normal outcome for several
            # archetypes rather than a rare one — the targets were written per
            # archetype and never checked against a build that could hold them
            # all at once — so the closest builds are the answer instead of
            # failing outright after two minutes of searching.
            viable_builds = [build for _, build in near_misses]

        print(f"Iterated {iterations} times...")

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
                key=lambda i: self.compute_item_price(i)
                if not i.glyph
                else self.compute_item_price(self.__all_items[i.parent_item_id]),
            )

            for item in build:
                if item.tier == 4 and not item.glyph:
                    if (
                        PassiveAttribute.EVOLVES_WITH_GOD_KILLS
                        not in self.__all_items[item.parent_item_id].passive_properties
                    ):
                        evos.append(item)
            # Place evolved items second
            for evo in evos:
                build.remove(evo)
            for evo in reversed(evos):
                build.insert(1, evo)
        return (viable_builds, iterations)

    # Relics are actives, and what they are worth is a question about the match
    # rather than about the build: Beads is worth everything against a stun and
    # nothing against a team with none, and no stat line says so. They are
    # therefore picked by convention rather than computed, and the build says as
    # much rather than implying a number stood behind them.
    #
    # Matched on name because relic ids churn between patches while these two
    # have been the default pair for years — a cleanse and a shield.
    CONVENTIONAL_RELICS = ("beads", "barrier", "aegis")

    def conventional_relics(self, count: int = 2) -> List[Item]:
        """The relics a player takes by default, best-known first."""
        available = [
            item
            for item in self.__all_items.values()
            if item.type is ItemType.RELIC
            and item.active
            and item.tier == 4
            and item.price == 500
            # Two relics are flagged active by the API but cannot be bought.
            and item.id not in (21478, 21492)
        ]

        chosen: List[Item] = []
        for wanted in self.CONVENTIONAL_RELICS:
            for item in available:
                if wanted in item.name.lower() and item not in chosen:
                    chosen.append(item)
                    break
            if len(chosen) == count:
                return chosen

        for item in sorted(available, key=lambda relic: relic.name):
            if item not in chosen:
                chosen.append(item)
            if len(chosen) == count:
                break
        return chosen[:count]

    def score_build(self, build: List[Item]) -> float:
        """How well a build serves this god's archetype.

        The same weighted stat scoring that ranks individual items, summed. It
        is what separates two builds that both clear the archetype's targets:
        for a support that means the one with more protections and health, for
        a mid mage the one with more power and penetration, because that is
        what those archetypes weight.

        Cheap, deterministic, and defined for every role — which matters
        because time-to-kill is not. That simulation swings basic attacks until
        the defender dies, so it says something real about a hunter and very
        little about a guardian who is not trying to auto-attack anyone.
        """
        weights = self.__get_weights()
        total = 0.0
        for item in build:
            score = self.__item_scores.get(item.id)
            if score is None:
                # Not every item in a build was in `valid_items` — a starter
                # often is not — so an unscored one is scored now rather than
                # silently counting for nothing.
                score = self.__compute_item_score(item, weights)
                self.__item_scores[item.id] = score
            total += score
        return total

    def rank_builds(
        self, builds: List[List[Item]], balance: float = None
    ) -> List[List[Item]]:
        """Viable builds, best for this archetype first.

        `balance` re-ranks the same builds as though the god wanted a different
        tank:damage split, without searching again. That is what makes the
        power curve's fork affordable: one search produces every viable build,
        and asking "which of these is best if I am ahead" is then a re-sort
        rather than another two minutes of combinations.

        It re-ranks rather than re-searches on purpose, so the fork can only
        ever choose among builds that were already viable — a branch is a
        different emphasis, not a different set of rules.
        """
        if balance is None:
            return sorted(builds, key=self.score_build, reverse=True)

        held = self.balance
        try:
            self.balance = balance
            self.__item_scores = {}
            return sorted(builds, key=self.score_build, reverse=True)
        finally:
            self.balance = held
            self.__item_scores = {}

    def compute_build_stats(
        self, items: List[Item]
    ) -> Dict[ItemAttribute, ItemProperty]:
        attributes: Dict[ItemAttribute, ItemProperty] = {}
        protections: float = None
        maximum_health: float = None

        def add_attribute(iattr: ItemAttribute, iprop: ItemProperty):
            if iattr in attributes:
                pval = attributes[iattr]
                pval.flat_value += (
                    iprop.flat_value if iprop.flat_value is not None else 0
                )
                pval.percent_value += (
                    iprop.percent_value if iprop.percent_value is not None else 0
                )
            else:
                attributes[iattr] = ItemProperty(
                    iattr,
                    iprop.flat_value if iprop.flat_value is not None else 0,
                    iprop.percent_value if iprop.percent_value is not None else 0,
                )

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
                attributes[ItemAttribute.MAGICAL_PROTECTION].flat_value *= protections
            if ItemAttribute.PHYSICAL_PROTECTION in attributes:
                attributes[ItemAttribute.PHYSICAL_PROTECTION].flat_value *= protections
        if maximum_health is not None:
            if ItemAttribute.HEALTH in attributes:
                attributes[ItemAttribute.HEALTH].flat_value *= maximum_health
        return attributes

    def compute_item_price(self, item: Item) -> int:
        return compute_item_price(item, self.__all_items)

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
        return list(filter(lambda item: item.tier >= 3, items))

    @staticmethod
    def filter_tiers_with_glyphs(items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.tier >= 3, items))

    def __filter_passive_denylist(self, items: List[Item]) -> List[Item]:
        if self.__current_archetype not in self.__archetype_passive_denylist:
            return items
        denylist = self.__archetype_passive_denylist[self.__current_archetype]
        return list(
            filter(
                lambda item: not any(item.passive_properties & denylist)
                if item.tier < 4
                else not any(
                    self.__all_items[item.parent_item_id].passive_properties & denylist
                ),
                items,
            )
        )

    def filter_glyph_parent(self, items: List[Item], glyph: Item = None) -> List[Item]:
        if glyph is not None:
            return list(filter(lambda item: item.id != glyph.parent_item_id, items))
        return list(
            filter(
                lambda item: item.id
                not in [g.parent_item_id for g in self.get_glyphs(self.valid_items)],
                items,
            )
        )

    @staticmethod
    def get_evolutions(items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.tier == 4 and not item.glyph, items))

    def filter_evolution_parents(self, items: List[Item]) -> List[Item]:
        return list(
            filter(
                lambda item: item.id
                not in [
                    evo.parent_item_id
                    for evo in self.get_evolutions(self.__all_items.values())
                ],
                items,
            )
        )

    def filter_recipes(self, items: List[Item]) -> List[Item]:
        return list(filter(lambda item: not item.recipe, items))

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
            return list(
                filter(
                    lambda item: all(
                        [p.attribute in allowed for p in item.item_properties]
                    ),
                    items,
                )
            )

        if prioritize == "power":
            return filter_items(power_allowed)
        elif prioritize == "defense":
            return filter_items(defense_allowed)
        raise ValueError

    def filter_queue_items(self, items: List[Item], queue_id: QueueId) -> List[Item]:
        if queue_id in (QueueId.RANKED_DUEL, QueueId.RANKED_DUEL_CONTROLLER):
            return list(filter(lambda i: not i.is_starter, items))
        return items

    @staticmethod
    def get_glyphs(items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.glyph, items))

    @staticmethod
    def get_tier_3_recipes(items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.recipe and item.tier == 3, items))

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
        return list(
            filter(lambda item: item.root_item_id == self.MAGIC_ACORN_ID, items)
        )

    def get_preferred_starters(self) -> List[Item]:
        return self.get_starters(
            list(
                filter(
                    lambda item: item.root_item_id
                    in self.ARCHETYPE_PREFERRED_STARTER[self.__current_archetype],
                    self.__all_items.values(),
                )
            )
        )

    def get_starters(self, items: List[Item]) -> List[Item]:
        return list(
            filter(
                lambda item: item.tier == 2
                and item.parent_item_id in self.__all_items
                and self.__all_items[item.parent_item_id].is_starter
                and item.root_item_id != self.MAGIC_ACORN_ID,
                items,
            )
        )

    def filter_acorns(self, items: List[Item]) -> List[Item]:
        return list(
            filter(lambda item: item.root_item_id != self.MAGIC_ACORN_ID, items)
        )

    @staticmethod
    def filter_by_stat(items: List[Item], stat: ItemAttribute) -> List[Item]:
        return list(
            filter(lambda i: stat in (p.attribute for p in i.item_properties), items)
        )

    def get_build_stats_string(self, build: List[Item], level: int = 20) -> str:
        build_stats = self.compute_build_stats(build)
        total_price = self.compute_price(build)
        desc = f"**Stats** _(Total Price - {total_price:,})_:\n\n"
        stats = build_stats.values()

        def get_level_stats(attr: ItemAttribute, value: float) -> str:
            stat = self.god.get_stat_at_level(attr, level)
            if stat > 0:
                if attr in (ItemAttribute.ATTACK_SPEED, ItemAttribute.MOVEMENT_SPEED):
                    return f"_({(stat + stat * value):.1f} @ Level {level})_"
                elif attr in (
                    ItemAttribute.COOLDOWN_REDUCTION,
                    ItemAttribute.CROWD_CONTROL_REDUCTION,
                ):
                    return f"_({round((stat + value) * 100)}% @ Level {level})_"
                return f"_({int(stat + value)} @ Level {level})_"
            return ""

        for stat in sorted(stats, key=lambda s: s.attribute.value):
            percent_prefix = stat.attribute in (
                ItemAttribute.PENETRATION,
                ItemAttribute.MAGICAL_PENETRATION,
                ItemAttribute.PHYSICAL_PENETRATION,
            )
            if stat.flat_value > 0:
                desc += (
                    f'**{"Flat " if percent_prefix else ""}'
                    f"{stat.attribute.display_name}**: {int(stat.flat_value)} "
                    f"{get_level_stats(stat.attribute, stat.flat_value)}\n"
                )
            if stat.percent_value > 0:
                desc += (
                    f'**{"Percent " if percent_prefix else ""}'
                    f"{stat.attribute.display_name}**: {round(stat.percent_value * 100)}% "
                    f"{get_level_stats(stat.attribute, stat.percent_value)}\n"
                )
        return desc
