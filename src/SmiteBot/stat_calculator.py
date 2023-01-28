from enum import Enum
from typing import Dict, List

from god import God, GodId, GodType
from item import Item, ItemAttribute

class BaseCalculator:
    @staticmethod
    def basic_attack_damage(base: float, per_level: float, level: int, power: int, god_type: GodType):
        return base + (per_level * level) + (1 if god_type == GodType.PHYSICAL else 0.2 * power)

    @staticmethod
    def protections(prots: float, red_pct: float, red_flat: float, pen_pct: float, pen_flat: int):
        return (prots * (1 - red_pct) - red_flat) * (1 - pen_pct) - pen_flat

    @staticmethod
    def damage_dealt(damage: float, prots: float, red_pct: float, red_flat: float, pen_pct: float, pen_flat: int):
        return (100 * damage) / (BaseCalculator.protections(prots, red_pct, red_flat, pen_pct, pen_flat))

class StatCalculator(BaseCalculator):
    build: List[Item]
    god: God

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
        # Power caps are soft, visual only
        ItemAttribute.MAGICAL_POWER: 900,
        ItemAttribute.PHYSICAL_POWER: 400,
        # Only one item gives Damage Reduction and it's +5
        ItemAttribute.DAMAGE_REDUCTION: 5,
    }

    PERCENT_ITEM_ATTRIBUTE_CAPS: Dict[ItemAttribute, float] = {
        ItemAttribute.MAGICAL_LIFESTEAL: 0.65,
        ItemAttribute.PHYSICAL_LIFESTEAL: 1,
        ItemAttribute.MAGICAL_PENETRATION: 0.40,
        ItemAttribute.PHYSICAL_PENETRATION: 0.40,
        ItemAttribute.CRITICAL_STRIKE_CHANCE: 1,
        ItemAttribute.CROWD_CONTROL_REDUCTION: 0.40,
        ItemAttribute.COOLDOWN_REDUCTION: 0.40,
    }

    def __init__(self):
        pass
