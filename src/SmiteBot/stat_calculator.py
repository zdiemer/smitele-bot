from enum import Enum
from typing import Dict, List, NamedTuple, Tuple, Union

from god import God, GodId, GodRole, GodType
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

class _Penetration(NamedTuple):
    flat: int
    percent: float

class _Stats:
    _stats: Dict[ItemAttribute, float | _Penetration]

    def __init__(self):
        self._stats = {}
 
    def set_stat(self, stat: ItemAttribute, value: float | _Penetration):
        self._stats[stat] = value

    def get_stat(self, stat: ItemAttribute) -> float | _Penetration:
        return self._stats[stat]

    def has_stat(self, stat: ItemAttribute) -> bool:
        return stat in self._stats

    def merge(self, other):
        for stat in filter(lambda s: other.has_stat(s), list(ItemAttribute)):
            if self.has_stat(stat):
                if stat in (ItemAttribute.MAGICAL_PENETRATION, ItemAttribute.PHYSICAL_PENETRATION):
                    first = self.get_stat(stat)
                    second = other.get_stat(stat)
                    updated_tuple = _Penetration(first.flat + second.flat, first.percent + second.percent)
                    self.set_stat(stat, updated_tuple)
                    continue
                self.set_stat(stat, self.get_stat(stat) + other.get_stat(stat))
            else:
                self.set_stat(stat, other.get_stat(stat))

class StatCalculator:
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

    def __init__(self, god: God, build: List[Item]):
        self.build = build
        self.god = god

    def _calculate_god_stats(self, level: int) -> _Stats:
        god_stats = _Stats()
        for stat in list(ItemAttribute):
            god_stats.set_stat(stat, self.god.get_stat_at_level(stat, level))
        return god_stats

    def _calculate_build_stats(self) -> _Stats:
        pass

    def _calculate_item_stats(self, item: Item) -> _Stats:
        item_stats = _Stats()

        for prop in item.item_properties:
            if prop.attribute.god_type != self.god.type:
                continue
            if prop.attribute in (ItemAttribute.MAGICAL_PENETRATION, ItemAttribute.PHYSICAL_PENETRATION):
                item_stats.set_stat(prop.attribute, _Penetration(prop.flat_value or 0, prop.percent_value or 0))
                continue
            item_stats.set_stat(prop.attribute, prop.flat_value or prop.percent_value)
        return item_stats

    def _calculate_build_stats(self) -> _Stats:
        build_stats = _Stats()
        for item in self.build:
            build_stats.merge(self._calculate_item_stats(item))
        return build_stats

    def _fix_overcapped(self, stats: _Stats) -> _Stats:
        for stat in filter(lambda s: stats.has_stat(s), list(ItemAttribute)):
            if stat in self.FLAT_ITEM_ATTRIBUTE_CAPS:
                value = stats.get_stat(stat)
                cap = self.FLAT_ITEM_ATTRIBUTE_CAPS[stat]
                if stat == ItemAttribute.ATTACK_SPEED:
                    attack_speed = self.god.get_stat_at_level(stat, 20)
                    value = attack_speed + attack_speed * value
                    cap = float(cap) / (attack_speed + 1)
                if stat in (ItemAttribute.MAGICAL_PENETRATION, ItemAttribute.PHYSICAL_PENETRATION):
                    if self.god.role == GodRole.ASSASSIN:
                        value.flat += self.god.get_stat_at_level(stat, 20)
                    if float(f'{value.flat:.2f}') > float(f'{cap:.2f}'):
                        stats.set_stat(stat, _Penetration(cap, value.percent))
                    continue
                if float(f'{value:.2f}') > float(f'{cap:.2f}'):
                    stats.set_stat(stat, value)
            if stat in self.PERCENT_ITEM_ATTRIBUTE_CAPS:
                value = stats.get_stat(stat)
                cap = self.PERCENT_ITEM_ATTRIBUTE_CAPS[stat]
                if stat in (ItemAttribute.MAGICAL_PENETRATION, ItemAttribute.PHYSICAL_PENETRATION):
                    if float(f'{value.percent:.2f}') > float(f'{cap:.2f}'):
                        stats.set_stat(stat, _Penetration(value.flat, cap))
                    continue
                if stat == ItemAttribute.COOLDOWN_REDUCTION and self.god.role == GodRole.WARRIOR:
                    value += self.god.get_stat_at_level(stat, 20)
                if stat == ItemAttribute.CROWD_CONTROL_REDUCTION \
                        and self.god.role == GodRole.GUARDIAN:
                    value += self.god.get_stat_at_level(stat, 20)
                if float(f'{value:.2f}') > float(f'{cap:.2f}'):
                    stats.set_stat(stat, cap)
        return stats

    def _calculate_god_build_stats(self, level: int) -> _Stats:
        stats = self._calculate_god_stats(level)
        stats.merge(self._calculate_build_stats())
        stats = self._fix_overcapped(stats)
        return stats
