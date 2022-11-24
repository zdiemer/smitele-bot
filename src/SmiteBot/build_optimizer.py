import asyncio
import itertools
import random
from math import log10
from typing import Dict, FrozenSet, List, Set, Tuple, Union

from god import God
from god_types import GodId, GodRole, GodType
from item import Item, ItemAttribute, ItemProperty

class BuildOptimizer:
    MAGIC_ACORN_ID = 18703

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
        'protection': set[(
            ItemAttribute.MAGICAL_PROTECTION,
            ItemAttribute.PHYSICAL_PROTECTION,
            ItemAttribute.PROTECTIONS)],
        'prots': set[(
            ItemAttribute.MAGICAL_PROTECTION,
            ItemAttribute.PHYSICAL_PROTECTION,
            ItemAttribute.PROTECTIONS)],
        'prot': set[(
            ItemAttribute.MAGICAL_PROTECTION,
            ItemAttribute.PHYSICAL_PROTECTION,
            ItemAttribute.PROTECTIONS)],
        'pen': set[(
            ItemAttribute.MAGICAL_PENETRATION,
            ItemAttribute.PENETRATION,
            ItemAttribute.PHYSICAL_PENETRATION)],
        'penetration': set[(
            ItemAttribute.MAGICAL_PENETRATION,
            ItemAttribute.PENETRATION,
            ItemAttribute.PHYSICAL_PENETRATION)],
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

    FLAT_ITEM_ATTRIBUTE_CAPS: Dict[ItemAttribute, float] = {
        ItemAttribute.ATTACK_SPEED: 2.5,
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

    GOD_ROLE_WEIGHT_MAPPINGS: Dict[GodRole, Dict[ItemAttribute, float]] = {
        GodRole.ASSASSIN: {
            ItemAttribute.ATTACK_SPEED: 0.5,
            ItemAttribute.BASIC_ATTACK_DAMAGE: 0.5,
            ItemAttribute.COOLDOWN_REDUCTION: 2.5,
            ItemAttribute.CRITICAL_STRIKE_CHANCE: -1,
            ItemAttribute.CROWD_CONTROL_REDUCTION: 0.5,
            ItemAttribute.DAMAGE_REDUCTION: 0.5,
            ItemAttribute.HP5: 1,
            ItemAttribute.HP5_AND_MP5: 1,
            ItemAttribute.HEALTH: 1,
            ItemAttribute.MP5: 1,
            ItemAttribute.MAGICAL_LIFESTEAL: None,
            ItemAttribute.MAGICAL_PENETRATION: None,
            ItemAttribute.MAGICAL_POWER: None,
            ItemAttribute.MAGICAL_PROTECTION: 0.4,
            ItemAttribute.MANA: 1,
            ItemAttribute.MAXIMUM_HEALTH: 1,
            ItemAttribute.MOVEMENT_SPEED: 1,
            ItemAttribute.PENETRATION: 1,
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE: -1,
            ItemAttribute.PHYSICAL_LIFESTEAL: -0.8,
            ItemAttribute.PHYSICAL_PENETRATION: 15,
            ItemAttribute.PHYSICAL_POWER: 10,
            ItemAttribute.PHYSICAL_PROTECTION: 0.4,
            ItemAttribute.PROTECTIONS: 0.4,
        },
        GodRole.GUARDIAN: {
            ItemAttribute.ATTACK_SPEED: -1,
            ItemAttribute.BASIC_ATTACK_DAMAGE: -1,
            ItemAttribute.COOLDOWN_REDUCTION: 5,
            ItemAttribute.CRITICAL_STRIKE_CHANCE: -1,
            ItemAttribute.CROWD_CONTROL_REDUCTION: 3,
            ItemAttribute.DAMAGE_REDUCTION: 1,
            ItemAttribute.HP5: 1,
            ItemAttribute.HP5_AND_MP5: 1,
            ItemAttribute.HEALTH: 5,
            ItemAttribute.MP5: 1,
            ItemAttribute.MAGICAL_LIFESTEAL: -1,
            ItemAttribute.MAGICAL_PENETRATION: -1,
            ItemAttribute.MAGICAL_POWER: -15,
            ItemAttribute.MAGICAL_PROTECTION: 10,
            ItemAttribute.MANA: 1,
            ItemAttribute.MAXIMUM_HEALTH: 1,
            ItemAttribute.MOVEMENT_SPEED: 1,
            ItemAttribute.PENETRATION: -1,
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE: None,
            ItemAttribute.PHYSICAL_LIFESTEAL: None,
            ItemAttribute.PHYSICAL_PENETRATION: None,
            ItemAttribute.PHYSICAL_POWER: None,
            ItemAttribute.PHYSICAL_PROTECTION: 10,
            ItemAttribute.PROTECTIONS: 10,
        },
        GodRole.HUNTER: {
            ItemAttribute.ATTACK_SPEED: 15,
            ItemAttribute.BASIC_ATTACK_DAMAGE: 1,
            ItemAttribute.COOLDOWN_REDUCTION: 0.1,
            ItemAttribute.CRITICAL_STRIKE_CHANCE: 5,
            ItemAttribute.CROWD_CONTROL_REDUCTION: 0.1,
            ItemAttribute.DAMAGE_REDUCTION: 0.1,
            ItemAttribute.HP5: 0.5,
            ItemAttribute.HP5_AND_MP5: 0.5,
            ItemAttribute.HEALTH: 0.5,
            ItemAttribute.MP5: 0.5,
            ItemAttribute.MAGICAL_LIFESTEAL: None,
            ItemAttribute.MAGICAL_PENETRATION: None,
            ItemAttribute.MAGICAL_POWER: None,
            ItemAttribute.MAGICAL_PROTECTION: 0.01,
            ItemAttribute.MANA: 0.5,
            ItemAttribute.MAXIMUM_HEALTH: 0.1,
            ItemAttribute.MOVEMENT_SPEED: 0.5,
            ItemAttribute.PENETRATION: 1,
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE: 1,
            ItemAttribute.PHYSICAL_LIFESTEAL: 6,
            ItemAttribute.PHYSICAL_PENETRATION: 1,
            ItemAttribute.PHYSICAL_POWER: 4,
            ItemAttribute.PHYSICAL_PROTECTION: 0.01,
            ItemAttribute.PROTECTIONS: 0.01,
        },
        GodRole.MAGE: {
            ItemAttribute.ATTACK_SPEED: 0.01,
            ItemAttribute.BASIC_ATTACK_DAMAGE: 0.01,
            ItemAttribute.COOLDOWN_REDUCTION: 100,
            ItemAttribute.CRITICAL_STRIKE_CHANCE: 0.01,
            ItemAttribute.CROWD_CONTROL_REDUCTION: 0.1,
            ItemAttribute.DAMAGE_REDUCTION: 0.1,
            ItemAttribute.HP5: 0.9,
            ItemAttribute.HP5_AND_MP5: 0.9,
            ItemAttribute.HEALTH: 0.8,
            ItemAttribute.MP5: 0.9,
            ItemAttribute.MAGICAL_LIFESTEAL: 10,
            ItemAttribute.MAGICAL_PENETRATION: 500,
            ItemAttribute.MAGICAL_POWER: 500,
            ItemAttribute.MAGICAL_PROTECTION: 0.1,
            ItemAttribute.MANA: 1,
            ItemAttribute.MAXIMUM_HEALTH: 0.9,
            ItemAttribute.MOVEMENT_SPEED: 0.9,
            ItemAttribute.PENETRATION: 0.5,
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE: None,
            ItemAttribute.PHYSICAL_LIFESTEAL: None,
            ItemAttribute.PHYSICAL_PENETRATION: None,
            ItemAttribute.PHYSICAL_POWER: None,
            ItemAttribute.PHYSICAL_PROTECTION: 0.1,
            ItemAttribute.PROTECTIONS: 0.1,
        },
        GodRole.WARRIOR: {
            ItemAttribute.ATTACK_SPEED: 0.1,
            ItemAttribute.BASIC_ATTACK_DAMAGE: 0.1,
            ItemAttribute.COOLDOWN_REDUCTION: 50,
            ItemAttribute.CRITICAL_STRIKE_CHANCE: 0.01,
            ItemAttribute.CROWD_CONTROL_REDUCTION: 30,
            ItemAttribute.DAMAGE_REDUCTION: 10,
            ItemAttribute.HP5: 0.9,
            ItemAttribute.HP5_AND_MP5: 0.9,
            ItemAttribute.HEALTH: 100,
            ItemAttribute.MP5: 0.9,
            ItemAttribute.MAGICAL_LIFESTEAL: None,
            ItemAttribute.MAGICAL_PENETRATION: None,
            ItemAttribute.MAGICAL_POWER: None,
            ItemAttribute.MAGICAL_PROTECTION: 500,
            ItemAttribute.MANA: 0.8,
            ItemAttribute.MAXIMUM_HEALTH: 1,
            ItemAttribute.MOVEMENT_SPEED: 0.9,
            ItemAttribute.PENETRATION: 0.5,
            ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE: 0.1,
            ItemAttribute.PHYSICAL_LIFESTEAL: 0.5,
            ItemAttribute.PHYSICAL_PENETRATION: 0.5,
            ItemAttribute.PHYSICAL_POWER: 0.5,
            ItemAttribute.PHYSICAL_PROTECTION: 500,
            ItemAttribute.PROTECTIONS: 100,
        },
    }

    # Some gods differ from their roles and need to optimize other stats (e.g. Kali, Bakasura)
    # Overrides GOD_ROLE_WEIGHT_MAPPINGS
    GOD_ID_WEIGHT_MAPPINGS: Dict[GodId, Set[ItemAttribute]] = {}

    GOD_ROLE_PREFERRED_STARTER: Dict[GodRole, Set[int]] = {
        GodRole.ASSASSIN: set([
            19500, # Manikin Scepter
            19502, # Bumba's Dagger
            19694, # Eye of the Jungle
            23048, # Protector's Mask
        ]),
        GodRole.GUARDIAN: set([
            19609, # Sentinel's Gift
            19634, # Benevolence
            20698, # War Flag
            16397, # Fighter's Mask
        ]),
        GodRole.HUNTER: set([
            19492, # Death's Toll
            19494, # Gilded Arrow
            19672, # Leather Cowl
            23048, # Protector's Mask
        ]),
        GodRole.MAGE: set([
            19508, # Sands of Time
            19510, # Vampiric Shroud
            19677, # Conduit Gem
            23048, # Protector's Mask
        ]),
        GodRole.WARRIOR: set([
            19490, # Bluestone Pendant
            19492, # Death's Toll
            19496, # Warrior's Axe
            19640, # Tainted Steel
            19751, # Warding Sigil
            16397, # Fighter's Mask
        ]),
    }

    # Overrides for preferred starters
    GOD_ID_PREFERRED_STARTER: Dict[GodId, Set[int]] = {}

    god: God
    valid_items: List[Item]
    __all_items: Dict[int, Item]
    __item_scores: Dict[int, float]
    __stat: Union[ItemAttribute, Set[ItemAttribute]] = None

    def __init__(self, god: God, valid_items: List[Item],
            all_items: Dict[int, Item], stat: str = None):
        self.god = god
        self.valid_items = valid_items
        self.__all_items = all_items
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
        if isinstance(self.__stat, ItemAttribute) and \
                self.__stat.god_type is not None and self.__stat.god_type != self.god.type:
            raise ValueError(self.__stat.display_name, ' is not a valid stat for ', self.god.name)

    def compute_build_stats(self, items: List[Item]) -> Dict[ItemAttribute, ItemProperty]:
        attributes: Dict[ItemAttribute, ItemProperty] = {}
        god_weights = self.GOD_ROLE_WEIGHT_MAPPINGS[self.god.role]
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
                if god_weights[attr] == None:
                    continue
                if attr == ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE:
                    attr = ItemAttribute.CRITICAL_STRIKE_CHANCE
                if attr == ItemAttribute.PENETRATION:
                    if self.god.type == GodType.MAGICAL:
                        attr = ItemAttribute.MAGICAL_PENETRATION
                    else:
                        attr = ItemAttribute.PHYSICAL_PENETRATION
                if attr == ItemAttribute.HP5_AND_MP5:
                    add_attribute(ItemAttribute.HP5, prop)
                    add_attribute(ItemAttribute.MP5, prop)
                    continue
                add_attribute(attr, prop)
        return attributes

    def __compute_item_score(
            self,
            item: Item,
            weights: Dict[ItemAttribute, float]) -> float:
        build_stats = self.compute_build_stats([item])
        return self.__compute_properties_score(build_stats, weights)

    @staticmethod
    def __compute_properties_score(
            build_stats: Dict[ItemAttribute, ItemProperty],
            weights: Dict[ItemAttribute, float]) -> float:
        score = 0
        for attr, prop in build_stats.items():
            if prop.flat_value > 0:
                score += log10(prop.flat_value) * weights[attr]
            if prop.percent_value > 0:
                score += log10(prop.percent_value * 100 * 6) * weights[attr]
        return score

    def __compute_build_score(self, items: List[Item]) -> float:
        total_score = 0
        for item in items:
            total_score += self.__item_scores[item.id]
        return total_score

    def compute_price_item(self, item: Item) -> int:
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
            total_price += self.compute_price_item(item)
        return total_price

    # Returns true if stats_1 > stats_2
    def __compare_builds(self, build_1: List[Item], build_2: List[Item]) -> bool:
        return \
            self.__compute_build_score(build_1) > \
                self.__compute_build_score(build_2)

    def __check_overcapped(self, stats: Dict[ItemAttribute, ItemProperty]) -> bool:
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
                    return True
            if attr in self.PERCENT_ITEM_ATTRIBUTE_CAPS:
                pct_value = prop.percent_value
                pct_cap = self.PERCENT_ITEM_ATTRIBUTE_CAPS[attr]
                if pct_value > pct_cap:
                    return True

    def set_stat(self, stat_name: str):
        self.__init_stat(stat_name)

    def __get_weights(self) -> Dict[ItemAttribute, float]:
        inverted_type = GodType.PHYSICAL if self.god.type != GodType.PHYSICAL else GodType.MAGICAL
        self_stat: Set[ItemAttribute] = set() if self.__stat is None else self.__stat if \
            isinstance(self.__stat, set) else set([self.__stat])
        stats_to_optimize: Set[ItemAttribute] = self_stat\
            .difference(self.GOD_TYPE_MAPPINGS[inverted_type])
        god_weights = self.GOD_ROLE_WEIGHT_MAPPINGS[self.god.role]
        if any(stats_to_optimize):
            for stat in stats_to_optimize:
                god_weights[stat] = 1_000
        return god_weights

    def __compute_scores(self, weights: Dict[ItemAttribute, float]) -> Dict[ItemAttribute, float]:
        self.__item_scores = {}
        for item in self.valid_items:
            self.__item_scores[item.id] = self.__compute_item_score(item, weights)

    async def optimize(self) -> Tuple[List[Item], int]:
        god_weights = self.__get_weights()
        self.__compute_scores(god_weights)
        best_build: FrozenSet[Item] = frozenset()
        starters = self.get_preferred_starters()

        sorted_ids = list([id for id, _ in sorted(
            self.__item_scores.items(), key=lambda item: item[1], reverse=True)])
        sorted_ids = sorted_ids[:int(len(sorted_ids) / 2)] # Top 50th percentile of item rankings
        rated_items = [self.__all_items[id] for id in sorted_ids]

        glyphs = self.get_glyphs(rated_items)
        items = self.filter_evolution_parents(
            self.filter_tiers(rated_items))

        checked_builds = set()
        unique_samples = set()
        async def check_combinations(
                existing_build: FrozenSet[Item],
                existing_best: FrozenSet[Item],
                combo_items: List[Item], size: int) -> Tuple[FrozenSet[Item], int]:
            build_n = 1
            combinations = itertools.combinations(combo_items, size)
            combo_best = existing_best
            for combo in combinations:
                await asyncio.sleep(0)
                random_sample = frozenset(combo)
                if random_sample in unique_samples:
                    continue
                unique_samples.add(random_sample)
                item_build = existing_build.union(random_sample)
                if item_build in checked_builds:
                    continue
                build_n += 1
                properties = self.compute_build_stats(item_build)
                checked_builds.add(item_build)
                if self.__check_overcapped(properties):
                    continue
                if self.__all_items[19514] in item_build:
                    print(f'{[i.name for i in item_build]}: {self.__compute_build_score(item_build)}')
                if not any(combo_best):
                    combo_best = item_build
                if self.__compare_builds(item_build, combo_best):
                    combo_best = item_build
            return (combo_best, build_n)

        build_size = 5
        iterations = 0
        for starter in starters:
            starter_build: FrozenSet[Item] = frozenset([starter])
            for glyph in glyphs:
                glyph_build = starter_build.union(frozenset([glyph]))
                non_glyph_items = self.filter_glyph_parent(items, glyph)
                best_build, build_n = await check_combinations(
                    glyph_build, best_build, non_glyph_items, build_size - 1)
                iterations += build_n
            best_build, build_n = await check_combinations(
                starter_build, best_build, items, build_size)
            iterations += build_n

        output_list = list(best_build)
        output_list[1:] = sorted(
            output_list[1:],
            key=lambda i: self.compute_price_item(i) if not i.glyph \
                else self.compute_price_item(self.__all_items[i.parent_item_id]))
        starter_idx = 0
        evos = []

        # Place starter first
        for idx, item in enumerate(output_list):
            if item.is_starter:
                starter_idx = idx
        if starter_idx != 0:
            output_list[starter_idx], output_list[0] = output_list[0], output_list[starter_idx]

        # Place evolved items second
        for item in output_list:
            if item.tier == 4 and not item.glyph:
                evos.append(item)
        for evo in evos:
            output_list.remove(evo)
        for evo in reversed(evos):
            output_list.insert(1, evo)

        return (output_list, iterations)

    @staticmethod
    def filter_unwanted_items(items: List[Item], weights: Dict[ItemAttribute, float]) -> List[Item]:
        def all_item_properties_unwanted(item: Item) -> bool:
            for prop in item.item_properties:
                if weights[prop.attribute] >= 1:
                    return True
            return False
        return list(filter(all_item_properties_unwanted, items))

    @staticmethod
    def filter_tiers(items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.tier >= 3 and not item.glyph, items))

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
            ItemAttribute.HEALTH,
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
                in self.GOD_ROLE_PREFERRED_STARTER[self.god.role],
            self.__all_items.values())))

    def get_starters(self, items: List[Item]) -> List[Item]:
        return list(filter(lambda item: item.tier == 2 and
            item.parent_item_id in self.__all_items and
            self.__all_items[item.parent_item_id].is_starter and
            item.root_item_id != self.MAGIC_ACORN_ID, items))
