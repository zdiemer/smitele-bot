import copy
import random

from enum import Enum
from typing import Dict, List, NamedTuple, Tuple, Union

from god import God, GodId, GodRole, GodType
from item import Item, ItemAttribute


class BaseCalculator:
    @staticmethod
    def basic_attack_damage(
        base: float,
        per_level: float,
        level: int,
        power: int,
        scaling: float = 1,
        progression: float = 1,
        is_crit: bool = False,
        crit_bonus: float = 1.75,
    ):
        # per_level applies from level 2, so a level-1 god has exactly its base
        # damage. This counted one level too many, and disagreed with
        # God.get_stat_at_level, which had it right — the same quantity coming
        # out differently depending on which one you asked.
        return (
            (crit_bonus if is_crit else 1)
            * progression
            * (base + (per_level * (level - 1)) + (scaling * power))
        )

    @staticmethod
    def protections(
        prots: float, red_pct: float, red_flat: float, pen_pct: float, pen_flat: int
    ):
        # Floored at zero. Penetration removes protection, it does not invert
        # it — and damage_dealt divides by (protections + 100), so unclamped
        # negatives inflate damage and anything past -100 flips its sign.
        return max(0.0, (prots * (1 - red_pct) - red_flat) * (1 - pen_pct) - pen_flat)

    @staticmethod
    def damage_dealt(
        damage: float,
        prots: float,
        red_pct: float,
        red_flat: float,
        pen_pct: float,
        pen_flat: int,
        damage_mit: float,
        damage_red: int,
        is_true: bool = False,
    ):
        return ((100 * (damage - damage_red)) * (1 - damage_mit)) / (
            (
                0
                if is_true
                else BaseCalculator.protections(
                    prots, red_pct, red_flat, pen_pct, pen_flat
                )
            )
            + 100
        )

    @staticmethod
    def attack_speed(base: float, increase: float, decrease: float):
        return base * (1 + (increase - decrease))


class _Penetration:
    flat: int
    percent: float

    def __init__(self, flat: int, percent: float):
        self.flat = flat
        self.percent = percent


class _Stats:
    stats: Dict[ItemAttribute, float | _Penetration]
    overcapped_stats: Dict[ItemAttribute, float | _Penetration]

    def __init__(self):
        self.stats = {}
        self.overcapped_stats = {}

    def set_stat(self, stat: ItemAttribute, value: float | _Penetration):
        self.stats[stat] = value

    def add_or_set_stat(self, stat: ItemAttribute, value: float | _Penetration):
        if self.has_stat(stat):
            if stat in (
                ItemAttribute.MAGICAL_PENETRATION,
                ItemAttribute.PHYSICAL_PENETRATION,
            ):
                pen = self.get_stat(stat)
                self.set_stat(
                    stat,
                    _Penetration(pen.flat + value.flat, pen.percent + value.percent),
                )
                return
            self.set_stat(stat, self.get_stat(stat) + value)
        else:
            self.set_stat(stat, value)

    def get_stat(self, stat: ItemAttribute) -> float | _Penetration:
        return self.stats[stat]

    def has_stat(self, stat: ItemAttribute) -> bool:
        return stat in self.stats

    def remove_stat(self, stat: ItemAttribute):
        del self.stats[stat]

    def merge(self, other):
        for stat in other.stats.keys():
            if self.has_stat(stat):
                if stat in (
                    ItemAttribute.MAGICAL_PENETRATION,
                    ItemAttribute.PHYSICAL_PENETRATION,
                ):
                    first = self.get_stat(stat)
                    second = other.get_stat(stat)
                    updated_tuple = _Penetration(
                        first.flat + second.flat, first.percent + second.percent
                    )
                    self.set_stat(stat, updated_tuple)
                    continue
                self.set_stat(stat, self.get_stat(stat) + other.get_stat(stat))
            else:
                self.set_stat(stat, other.get_stat(stat))


class GodBuild:
    god: God
    build: List[Item]
    level: int

    def __init__(self, god: God, build: List[Item], level: int):
        self.god = god
        self.build = build
        self.level = level


class BuildStatCalculator:
    god: GodBuild

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

    def __init__(self, god: GodBuild):
        self.god = god

    def calculate_god_stats(self) -> _Stats:
        god_stats = _Stats()
        for stat in list(ItemAttribute):
            stat_at_level = self.god.god.get_stat_at_level(stat, self.god.level)
            if stat_at_level > 0:
                if stat == ItemAttribute.PHYSICAL_PENETRATION:
                    god_stats.set_stat(stat, _Penetration(stat_at_level, 0))
                    continue
                god_stats.set_stat(stat, stat_at_level)
        return god_stats

    def calculate_item_stats(self, item: Item) -> _Stats:
        item_stats = _Stats()

        # Heartward Amulet
        if item.id in (21504, 21505, 11116):
            item_stats.add_or_set_stat(ItemAttribute.MAGICAL_PROTECTION, 20)
            if self.god.god.id in (GodId.CU_CHULAINN, GodId.YEMOJA):
                item_stats.add_or_set_stat(ItemAttribute.HP5, 30)
            else:
                item_stats.add_or_set_stat(ItemAttribute.MP5, 30)
        # Evolved Gauntlet of Thebes
        if item.id == 15594:
            item_stats.add_or_set_stat(ItemAttribute.MAGICAL_PROTECTION, 10)
            item_stats.add_or_set_stat(ItemAttribute.PHYSICAL_PROTECTION, 10)
        # Evolved Prophetic Cloak
        if item.id == 24172:
            item_stats.add_or_set_stat(ItemAttribute.MAGICAL_PROTECTION, 30)
            item_stats.add_or_set_stat(ItemAttribute.PHYSICAL_PROTECTION, 30)

        for prop in item.item_properties:
            if (
                prop.attribute.god_type is not None
                and prop.attribute.god_type != self.god.god.type
            ):
                continue
            if (
                prop.attribute == ItemAttribute.ATTACK_SPEED
                and self.god.god.id == GodId.KING_ARTHUR
            ):
                continue
            if self.god.god.id in (GodId.CU_CHULAINN, GodId.YEMOJA):
                if prop.attribute == ItemAttribute.MANA:
                    item_stats.add_or_set_stat(ItemAttribute.HEALTH, prop.flat_value)
                    continue
                if prop.attribute == ItemAttribute.MP5:
                    item_stats.add_or_set_stat(ItemAttribute.HP5, prop.flat_value)
                    continue
                if prop.attribute == ItemAttribute.HP5_AND_MP5:
                    item_stats.add_or_set_stat(ItemAttribute.HP5, prop.flat_value * 2)
                    continue
            if prop.attribute in (
                ItemAttribute.MAGICAL_PENETRATION,
                ItemAttribute.PHYSICAL_PENETRATION,
                ItemAttribute.PENETRATION,
            ):
                stat = prop.attribute
                if prop.attribute == ItemAttribute.PENETRATION:
                    stat = (
                        ItemAttribute.MAGICAL_PENETRATION
                        if self.god.god.type == GodType.MAGICAL
                        else ItemAttribute.PHYSICAL_PENETRATION
                    )
                item_stats.add_or_set_stat(
                    stat, _Penetration(prop.flat_value or 0, prop.percent_value or 0)
                )
                continue
            if (
                prop.attribute == ItemAttribute.PHYSICAL_CRITICAL_STRIKE_CHANCE
                and self.god.god.type == GodType.PHYSICAL
            ):
                item_stats.add_or_set_stat(
                    ItemAttribute.CRITICAL_STRIKE_CHANCE, prop.percent_value
                )
            if prop.attribute == ItemAttribute.LIFESTEAL:
                if self.god.god.type == GodType.PHYSICAL:
                    item_stats.add_or_set_stat(
                        ItemAttribute.PHYSICAL_LIFESTEAL, prop.percent_value
                    )
                else:
                    item_stats.add_or_set_stat(
                        ItemAttribute.MAGICAL_LIFESTEAL, prop.percent_value
                    )
            item_stats.add_or_set_stat(
                prop.attribute, prop.flat_value or prop.percent_value
            )
        return item_stats

    def calculate_build_stats(self) -> _Stats:
        build_stats = _Stats()
        for item in self.god.build:
            build_stats.merge(self.calculate_item_stats(item))
        return build_stats

    def _fix_overcapped(self, stats: _Stats) -> _Stats:
        for stat in stats.stats.keys():
            if stat in self.FLAT_ITEM_ATTRIBUTE_CAPS:
                value = stats.get_stat(stat)
                cap = self.FLAT_ITEM_ATTRIBUTE_CAPS[stat]
                if stat == ItemAttribute.ATTACK_SPEED:
                    # The merged stat is the god's absolute attack speed plus
                    # the *sum of item multipliers*. Converting by treating the
                    # whole thing as a multiplier counted the base twice over —
                    # a plain hunter build came out at 4.07 attacks a second
                    # and every build in the corpus sat at the fire cap, which
                    # erased attack speed as a difference between builds.
                    base = self.god.god.get_stat_at_level(stat, self.god.level)
                    multiplier = max(value - base, 0.0)
                    value = base * (1 + multiplier)
                    if float(f"{value:.2f}") > float(f"{cap:.2f}"):
                        stats.overcapped_stats[stat] = value
                    # Always written back as the absolute: leaving the raw
                    # multiplier in place whenever a build happened to stay
                    # under the cap fed 0.85 to anything reading it as
                    # attacks-per-second.
                    stats.set_stat(stat, min(value, cap))
                    continue
                if stat in (
                    ItemAttribute.MAGICAL_PENETRATION,
                    ItemAttribute.PHYSICAL_PENETRATION,
                ):
                    if self.god.god.role == GodRole.ASSASSIN:
                        value.flat += self.god.god.get_stat_at_level(stat, 20)
                    if float(f"{value.flat:.2f}") > float(f"{cap:.2f}"):
                        if stat in stats.overcapped_stats:
                            overcapped_pen = stats.overcapped_stats[stat]
                            overcapped_pen.flat = value.flat
                            stats.overcapped_stats[stat] = overcapped_pen
                        else:
                            stats.overcapped_stats[stat] = _Penetration(value.flat, 0)
                        stats.set_stat(stat, _Penetration(cap, value.percent))
                    continue
                if float(f"{value:.2f}") > float(f"{cap:.2f}"):
                    stats.overcapped_stats[stat] = value
                    stats.set_stat(stat, value)
            if stat in self.PERCENT_ITEM_ATTRIBUTE_CAPS:
                value = stats.get_stat(stat)
                cap = self.PERCENT_ITEM_ATTRIBUTE_CAPS[stat]
                if stat in (
                    ItemAttribute.MAGICAL_PENETRATION,
                    ItemAttribute.PHYSICAL_PENETRATION,
                ):
                    if float(f"{value.percent:.2f}") > float(f"{cap:.2f}"):
                        if stat in stats.overcapped_stats:
                            overcapped_pen = stats.overcapped_stats[stat]
                            overcapped_pen.percent = value.percent
                            stats.overcapped_stats[stat] = overcapped_pen
                        else:
                            stats.overcapped_stats[stat] = _Penetration(
                                0, value.percent
                            )
                        stats.set_stat(stat, _Penetration(value.flat, cap))
                    continue
                if (
                    stat == ItemAttribute.COOLDOWN_REDUCTION
                    and self.god.god.role == GodRole.WARRIOR
                ):
                    value += self.god.god.get_stat_at_level(stat, 20)
                if (
                    stat == ItemAttribute.CROWD_CONTROL_REDUCTION
                    and self.god.god.role == GodRole.GUARDIAN
                ):
                    value += self.god.god.get_stat_at_level(stat, 20)
                if float(f"{value:.2f}") > float(f"{cap:.2f}"):
                    stats.overcapped_stats[stat] = value
                    stats.set_stat(stat, cap)
        return stats

    def calculate_god_build_stats(self) -> _Stats:
        stats = self.calculate_god_stats()
        stats.merge(self.calculate_build_stats())
        if ItemAttribute.MAXIMUM_HEALTH in stats.stats:
            health = stats.get_stat(ItemAttribute.HEALTH)
            max_multiplier = stats.get_stat(ItemAttribute.MAXIMUM_HEALTH)
            stats.set_stat(ItemAttribute.HEALTH, health + (health * max_multiplier))
            stats.remove_stat(ItemAttribute.MAXIMUM_HEALTH)
        if ItemAttribute.PROTECTIONS in stats.stats:
            mag_prots = stats.get_stat(ItemAttribute.MAGICAL_PROTECTION)
            phys_prots = stats.get_stat(ItemAttribute.PHYSICAL_PROTECTION)
            multiplier = stats.get_stat(ItemAttribute.PROTECTIONS)
            stats.set_stat(
                ItemAttribute.MAGICAL_PROTECTION, mag_prots + (mag_prots * multiplier)
            )
            stats.set_stat(
                ItemAttribute.PHYSICAL_PROTECTION,
                phys_prots + (phys_prots * multiplier),
            )
            stats.remove_stat(ItemAttribute.PROTECTIONS)
        if self.god.god.id == GodId.OLORUN:
            # Touch of Fate, final values: 20% critical chance on reaching 100
            # Magical Power from items, 2% more per 12 beyond, capped at 100%
            # (reached at 580). The 15%-at-150 formula here was two patches
            # stale. Magical gods have no base power, so the power stat is
            # item power. His crits deal 140%, enforced by the damage side.
            magic_power = (
                stats.get_stat(ItemAttribute.MAGICAL_POWER)
                if stats.has_stat(ItemAttribute.MAGICAL_POWER)
                else 0
            )
            crit_chance = (
                0.20 + ((magic_power - 100) / 12) * 0.02 if magic_power >= 100 else 0
            )
            stats.set_stat(ItemAttribute.CRITICAL_STRIKE_CHANCE, min(1.0, crit_chance))
        for item in self.god.build:
            # Evolved Transcendence, both id generations: Physical Power equal
            # to 2% of Maximum Mana. This checked only the retired id with a
            # made-up 3%, so the build everyone actually runs got nothing.
            if item.id in (15767, 25675):
                mana = stats.get_stat(ItemAttribute.MANA)
                stats.add_or_set_stat(ItemAttribute.PHYSICAL_POWER, mana * 0.02)
            # Evolved Book of Thoth: Magical Power equal to 7% of *item* mana,
            # which is total mana less what the god has bare.
            if item.id == 25673:
                item_mana = stats.get_stat(ItemAttribute.MANA) - (
                    self.god.god.get_stat_at_level(ItemAttribute.MANA, self.god.level)
                )
                if item_mana > 0:
                    stats.add_or_set_stat(
                        ItemAttribute.MAGICAL_POWER, item_mana * 0.07
                    )
            # Devoted Deathbringer's glyph: crit chance is multiplied by 1.2,
            # and every 5% over 100% becomes 5 Physical Power. The percent cap
            # in _fix_overcapped would clamp to 100% and lose the overflow, so
            # both halves are applied here.
            if item.id == 25931 and stats.has_stat(
                ItemAttribute.CRITICAL_STRIKE_CHANCE
            ):
                crit = stats.get_stat(ItemAttribute.CRITICAL_STRIKE_CHANCE) * 1.2
                if crit > 1.0:
                    stats.add_or_set_stat(
                        ItemAttribute.PHYSICAL_POWER, ((crit - 1.0) / 0.05) * 5
                    )
                stats.set_stat(ItemAttribute.CRITICAL_STRIKE_CHANCE, min(crit, 1.0))
            # Sovereignty
            if item.id == 7528:
                stats.add_or_set_stat(ItemAttribute.PHYSICAL_PROTECTION, 15)
                stats.add_or_set_stat(ItemAttribute.HP5, 25)
            # Amulet of the Stronghold
            if item.id == 21505:
                physical_prots = stats.get_stat(ItemAttribute.PHYSICAL_PROTECTION)
                stats.add_or_set_stat(
                    ItemAttribute.MAGICAL_PROTECTION, physical_prots * 0.15
                )
        stats = self._fix_overcapped(stats)
        return stats


class DamageCalculator:
    @staticmethod
    def calculate_basic_damage_dealt(
        attacking_god: GodBuild,
        defending_god: GodBuild,
        attacking_stats: _Stats,
        defending_stats: _Stats,
        progression: float = 1,
        pct_red: float = 0,
        damage_mit: float = 0,
        crit_bonus: float = 1.75,
        red_flat: float = 0,
    ) -> Tuple[float, bool]:
        power_type = (
            ItemAttribute.PHYSICAL_POWER
            if attacking_god.god.type == GodType.PHYSICAL
            else ItemAttribute.MAGICAL_POWER
        )

        crit_chance = 0
        has_qins = False
        for item in attacking_god.build:
            # Deathbringer and its glyphs, Devoted included
            if item.id in (7545, 21500, 21501, 25931):
                # Deathbringer increase Critical Strike bonus damage by 25%
                crit_bonus += 0.25
            # Qin's Sais
            if item.id == 7593:
                has_qins = True

        for item in defending_god.build:
            # Evolved Prophetic Cloak: 6% mitigation over 300 total protections,
            # another 6% over 500.
            if item.id == 24172:
                total_prots = defending_stats.get_stat(
                    ItemAttribute.MAGICAL_PROTECTION
                ) + defending_stats.get_stat(ItemAttribute.PHYSICAL_PROTECTION)

                if total_prots > 500:
                    damage_mit += 0.12
                elif total_prots > 300:
                    damage_mit += 0.06
            # Sigil of the Old Guard: 3% base. The Rebuke stacks need ability
            # hits, which a basic-attack duel never lands.
            if item.id == 19752:
                damage_mit += 0.03
            # Spectral Armor: an aura, not a stack — bonus damage from
            # physical critical strikes is reduced by 40%. Magical crits
            # (Olorun, Fail-not) go through untouched.
            if item.id == 24187 and attacking_god.god.type == GodType.PHYSICAL:
                crit_bonus = 1 + (crit_bonus - 1) * 0.60

        if (
            attacking_god.god.type == GodType.PHYSICAL
            or attacking_god.god.id == GodId.OLORUN
        ):
            if attacking_stats.has_stat(ItemAttribute.CRITICAL_STRIKE_CHANCE):
                crit_chance = attacking_stats.get_stat(
                    ItemAttribute.CRITICAL_STRIKE_CHANCE
                )

        # Olorun's converted crits deal 140%, not the usual 175%; the chance
        # itself is set by the stat calculator from his passive.
        if attacking_god.god.id == GodId.OLORUN:
            crit_bonus = min(crit_bonus, 1.40)

        is_crit = random.randrange(0, 100) < (crit_chance * 100)

        total_basic_damage = BaseCalculator.basic_attack_damage(
            attacking_god.god.stats.basic_attack.base_damage,
            attacking_god.god.stats.basic_attack.per_level,
            attacking_god.level,
            attacking_stats.get_stat(power_type)
            if attacking_stats.has_stat(power_type)
            else 0,
            attacking_god.god.stats.basic_attack.scaling,
            progression,
            is_crit,
            crit_bonus,
        )

        if has_qins:
            # 1.5% of the target's max health, scaling above 2,000 health up
            # to 6% at 2,750. The wiki states the endpoints; the ramp between
            # them is linear.
            defending_health = defending_stats.get_stat(ItemAttribute.HEALTH)
            qins_bonus = min(
                0.015 + (max(defending_health - 2000, 0) / 250) * 0.015, 0.06
            )
            total_basic_damage += defending_health * qins_bonus

        if attacking_god.god.id == GodId.IZANAMI:
            is_crit = random.randrange(0, 100) < (crit_chance * 100)
            total_basic_damage += BaseCalculator.basic_attack_damage(
                attacking_god.god.stats.basic_attack.base_damage_back,
                attacking_god.god.stats.basic_attack.per_level_back,
                attacking_god.level,
                attacking_stats.get_stat(power_type)
                if attacking_stats.has_stat(power_type)
                else 0,
                attacking_god.god.stats.basic_attack.scaling_back,
                progression,
                is_crit,
                crit_bonus,
            )

        pen_type = (
            ItemAttribute.PHYSICAL_PENETRATION
            if attacking_god.god.type == GodType.PHYSICAL
            else ItemAttribute.MAGICAL_PENETRATION
        )

        attacking_god_penetration = (
            attacking_stats.get_stat(pen_type)
            if attacking_stats.has_stat(pen_type)
            else _Penetration(0, 0)
        )

        total_damage_dealt = BaseCalculator.damage_dealt(
            total_basic_damage,
            defending_stats.get_stat(
                ItemAttribute.PHYSICAL_PROTECTION
                if attacking_god.god.type == GodType.PHYSICAL
                else ItemAttribute.MAGICAL_PROTECTION
            ),
            pct_red,
            red_flat,
            attacking_god_penetration.percent,
            attacking_god_penetration.flat,
            damage_mit,
            defending_stats.get_stat(ItemAttribute.DAMAGE_REDUCTION)
            if defending_stats.has_stat(ItemAttribute.DAMAGE_REDUCTION)
            else 0,
        )
        return (total_damage_dealt, is_crit)

    @staticmethod
    def calculate_basic_ttk(
        attacking_god: GodBuild,
        defending_god: GodBuild,
        assume_item_passives_stacked: bool = False,
        max_seconds: float = 999.0,
        kit=None,
        steroid=None,
    ) -> float:
        """Seconds for the attacker to kill the defender.

        Basics-only by default. Passing `kit` (an `ability_kit.AbilityKit`)
        adds the god's damaging abilities to the same timeline — cast the
        moment they come off cooldown, with ability-triggered item passives
        riding along — and `steroid` (an `ability_kit.Steroid`) folds in the
        god's own uptime-averaged contribution to their basics.
        """
        attacking_god_stats = BuildStatCalculator(
            attacking_god
        ).calculate_god_build_stats()
        defending_god_stats = BuildStatCalculator(
            defending_god
        ).calculate_god_build_stats()

        has_demon_blade = False
        has_silverbranch = False
        has_executioner = False
        has_heavy_executioner = False
        has_demonic_grip = False
        has_obow = False
        has_duality = False
        has_telkhines = False
        has_cyclopean = False
        has_manikin = False
        has_hecate = False
        has_tahuti = False
        for item in attacking_god.build:
            # Odysseus' Bow: every fourth basic chains for 15 + 60% of Basic
            # Attack Power (the full computed basic, not the power stat).
            # Item-effect damage: no crit, no on-hit triggers.
            if item.id == 10482:
                has_obow = True
            # Duality: every 3s the next basic adds 30% of Basic Attack Power
            # as Physical Ability damage.
            if item.id == 26147:
                has_duality = True
            # Telkhines Ring: flat bonus magical damage on every basic.
            if item.id == 25761:
                has_telkhines = True
            # Cyclopean Ring: 9% max-health magical damage, once per 8s,
            # reduced 2s per basic landed on a god.
            if item.id == 23869:
                has_cyclopean = True
            # Manikin Mace: each basic applies a 60-over-2s burn, and up to
            # four burns run concurrently.
            if item.id == 19513:
                has_manikin = True
            # Ring of Hecate: +5% magical power per basic landed, additive to
            # 15% at three stacks.
            if item.id == 26321:
                has_hecate = True
            # Rod of Tahuti and its glyphs: the scaling term is worth 15%
            # more against targets below 60% health.
            if item.id in (7600, 22941, 21484):
                has_tahuti = True
            # Ornate Arrow: 1.25% attack speed and 1% critical chance per 100
            # gold held, twenty stacks. Banked gold is a game state the sim
            # cannot see, so it rides the stacked-passives assumption, where a
            # level 20 carry holds that much.
            if item.id == 19650 and assume_item_passives_stacked:
                base = attacking_god.god.get_stat_at_level(
                    ItemAttribute.ATTACK_SPEED, attacking_god.level
                )
                current = attacking_god_stats.get_stat(ItemAttribute.ATTACK_SPEED)
                attacking_god_stats.set_stat(
                    ItemAttribute.ATTACK_SPEED, current + base * 0.25
                )
                crit = (
                    attacking_god_stats.get_stat(
                        ItemAttribute.CRITICAL_STRIKE_CHANCE
                    )
                    if attacking_god_stats.has_stat(
                        ItemAttribute.CRITICAL_STRIKE_CHANCE
                    )
                    else 0
                )
                attacking_god_stats.set_stat(
                    ItemAttribute.CRITICAL_STRIKE_CHANCE, min(crit + 0.20, 1.0)
                )
            # Death's Temper
            if item.id == 19587:
                if assume_item_passives_stacked:
                    basic_damage = attacking_god_stats.get_stat(
                        ItemAttribute.BASIC_ATTACK_DAMAGE
                    )
                    # 7% increased Basic Attack Damage, stacking 5 times per
                    # the item's own text (the wiki still shows the old 10).
                    attacking_god_stats.set_stat(
                        ItemAttribute.BASIC_ATTACK_DAMAGE,
                        basic_damage + basic_damage * 0.07 * 5,
                    )
            # Demon Blade
            if item.id == 12674:
                has_demon_blade = True
            # Dominance
            if item.id == 19924:
                pen = (
                    attacking_god_stats.get_stat(ItemAttribute.PHYSICAL_PENETRATION)
                    if attacking_god_stats.has_stat(ItemAttribute.PHYSICAL_PENETRATION)
                    else _Penetration(0, 0)
                )
                # Adds 20% Penetration to Basic Attacks, allows overcapping
                attacking_god_stats.set_stat(
                    ItemAttribute.PHYSICAL_PENETRATION,
                    _Penetration(pen.flat, pen.percent + 0.20),
                )
            # Silverbranch Bow
            if item.id == 14084:
                has_silverbranch = True
            # The Executioner, Ferocious and Envenomed: 7% physical protection
            # reduction a hit, four stacks
            if item.id in (7575, 23135, 25932):
                has_executioner = True
            # The Heavy Executioner
            if item.id == 22960:
                has_heavy_executioner = True
            # Demonic Grip: the magical analogue, 10% a hit, three stacks
            if item.id == 8564 and attacking_god.god.type == GodType.MAGICAL:
                has_demonic_grip = True

        has_berserkers = False
        has_mail_of_renewal = False
        has_midgardian_mail = False
        has_oni_hunters = False
        for item in defending_god.build:
            # Berserker's Shield
            if item.id == 16544:
                has_berserkers = True
            # Mail of Renewal
            if item.id == 20217:
                has_mail_of_renewal = True
            # Midgardian Mail
            if item.id == 7907:
                has_midgardian_mail = True
            # Oni Hunter's Garb
            if item.id == 12679:
                has_oni_hunters = True
            # Sentinel's Embrace: the aura splits 80 of each protection among
            # allies in range, but a defender standing alone gets only 30.
            if item.id == 19627:
                defending_god_stats.add_or_set_stat(
                    ItemAttribute.MAGICAL_PROTECTION, 30
                )
                defending_god_stats.add_or_set_stat(
                    ItemAttribute.PHYSICAL_PROTECTION, 30
                )

        seconds = 0
        defending_health = initial_health = defending_god_stats.get_stat(
            ItemAttribute.HEALTH
        )
        defending_hp5 = defending_god_stats.get_stat(ItemAttribute.HP5)
        p_idx = 0
        progression = attacking_god.god.stats.basic_attack.progression

        berserkers_exp = 0
        berserkers_cd = 0

        demon_blade_exp = 0

        executioner_stacks = 0

        renewal_exp = 0
        renewal_cd = 0
        renewal_stacks: List[float] = []

        midgardian_stacks: List[float] = []

        dmg = 0
        crit_bonus = 1.75
        is_crit = False

        # Mitigation the defender keeps for the whole fight: Oni Hunter's is
        # 4% per nearby enemy up to three, and a duel has exactly one. Kept as
        # a floor rather than folded into `damage_mit` because Berserker's
        # expiry resets to it — resetting to zero was silently deleting this.
        base_mit = 0.04 if has_oni_hunters else 0.0
        damage_mit = base_mit

        og_pen = (
            attacking_god_stats.get_stat(ItemAttribute.PHYSICAL_PENETRATION)
            if attacking_god_stats.has_stat(ItemAttribute.PHYSICAL_PENETRATION)
            else _Penetration(0, 0)
        )
        og_power = (
            attacking_god_stats.get_stat(ItemAttribute.PHYSICAL_POWER)
            if attacking_god.god.type == GodType.PHYSICAL
            else 0
        )
        og_magical_power = (
            attacking_god_stats.get_stat(ItemAttribute.MAGICAL_POWER)
            if attacking_god_stats.has_stat(ItemAttribute.MAGICAL_POWER)
            else 0
        )

        attacker_is_physical = attacking_god.god.type == GodType.PHYSICAL

        def proc_damage(amount: float, magical: bool, red: float, mit: float) -> float:
            """One item proc through the defender's protections.

            A proc of the attacker's own damage type rides the attacker's
            penetration and any protection reduction already on the target;
            an off-type proc meets the bare protections.
            """
            same_type = magical != attacker_is_physical
            pen = (
                attacking_god_stats.get_stat(
                    ItemAttribute.MAGICAL_PENETRATION
                    if magical
                    else ItemAttribute.PHYSICAL_PENETRATION
                )
                if same_type
                and attacking_god_stats.has_stat(
                    ItemAttribute.MAGICAL_PENETRATION
                    if magical
                    else ItemAttribute.PHYSICAL_PENETRATION
                )
                else _Penetration(0, 0)
            )
            return BaseCalculator.damage_dealt(
                amount,
                defending_god_stats.get_stat(
                    ItemAttribute.MAGICAL_PROTECTION
                    if magical
                    else ItemAttribute.PHYSICAL_PROTECTION
                ),
                red if same_type else 0,
                0,
                pen.percent,
                pen.flat,
                mit,
                0,
            )

        hits_landed = 0
        duality_ready = 0.0
        cyclopean_cd = 0.0
        manikin_burns: List[float] = []
        hecate_stacks = 0
        telkhines_bonus = 5 + 3 * attacking_god.level

        # The god's own steady-state contribution, if the caller supplied one.
        steroid_attack_speed = steroid.attack_speed if steroid else 0.0
        steroid_flat = steroid.flat_basic if steroid else 0.0
        steroid_prot_strip = steroid.prot_strip if steroid else 0.0
        if steroid and steroid.power_multiplier != 1.0:
            og_power *= steroid.power_multiplier
            og_magical_power *= steroid.power_multiplier
            for power_stat in (
                ItemAttribute.PHYSICAL_POWER,
                ItemAttribute.MAGICAL_POWER,
            ):
                if attacking_god_stats.has_stat(power_stat):
                    attacking_god_stats.set_stat(
                        power_stat,
                        attacking_god_stats.get_stat(power_stat)
                        * steroid.power_multiplier,
                    )
        if steroid and steroid.power_scale_basic:
            steroid_flat += og_power * steroid.power_scale_basic

        # Ability rotation state: abilities are cast the moment they are off
        # cooldown, each paying a short lockout before basics resume.
        has_heartseeker = False
        has_polynomicon = False
        has_hydras = False
        has_titans = False
        bluestone_flat = 0.0
        has_brooch = False
        has_crusher = False
        heartseeker_first = True
        bluestone_first = True
        hydras_window = 0.0
        poly_window = 0.0
        poly_icd = 0.0
        casts = []
        if kit is not None:
            for item in attacking_god.build:
                if item.id == 12680:
                    has_heartseeker = True
                if item.id == 25766:
                    has_polynomicon = True
                if item.id == 8550:
                    has_hydras = True
                if item.id == 7523:
                    has_titans = True
                # Bluestone Pendant / Brooch / Corrupted: flat damage over
                # time per damaging ability, taken in full on the first
                # application and halved on the follow-ups.
                if item.id == 23855:
                    bluestone_flat = 40.0
                if item.id == 23859:
                    bluestone_flat = 160.0
                    has_brooch = True
                if item.id == 23860:
                    bluestone_flat = 300.0
                if item.id == 23858:
                    has_crusher = True
            cdr = (
                attacking_god_stats.get_stat(ItemAttribute.COOLDOWN_REDUCTION)
                if attacking_god_stats.has_stat(ItemAttribute.COOLDOWN_REDUCTION)
                else 0.0
            )
            casts = [{"ability": a, "ready": 0.0} for a in kit.damaging]

        base_attack_speed = attacking_god_stats.get_stat(ItemAttribute.ATTACK_SPEED)
        if ItemAttribute.ATTACK_SPEED in attacking_god_stats.overcapped_stats:
            base_attack_speed = attacking_god_stats.overcapped_stats[
                ItemAttribute.ATTACK_SPEED
            ]

        def expire_renewal_stack(exp_time: float) -> bool:
            if seconds >= exp_time:
                defending_god_stats.add_or_set_stat(
                    ItemAttribute.MAGICAL_PROTECTION, -5
                )
                defending_god_stats.add_or_set_stat(
                    ItemAttribute.PHYSICAL_PROTECTION, -5
                )
                return True
            return False

        def expire_stack(exp_time: float) -> bool:
            if seconds >= exp_time:
                return True
            return False

        while defending_health > 0:
            # A build that cannot out-damage the defender's regen never ends
            # the fight; without a ceiling that is an infinite loop, hit in
            # practice the first time a corpus sweep fed this defensive builds.
            if seconds >= max_seconds:
                return max_seconds
            pre_fire_seconds = seconds
            red_pct = 0
            # Demon Blade's passive is penetration only; the +15% attack speed
            # on the item is a flat stat the build already counted.
            attack_speed = BaseCalculator.attack_speed(
                base_attack_speed,
                increase=steroid_attack_speed,
                decrease=len(midgardian_stacks) * 0.08,
            )

            # Cast anything off cooldown before swinging. Each cast pays a
            # short lockout, opens the empowered-basic windows, and drags its
            # ability item passives along.
            for cast in casts:
                if seconds < cast["ready"]:
                    continue
                ability = cast["ability"]
                magical_ability = ability.scaling_stat == "magical"
                ability_power = (
                    attacking_god_stats.get_stat(
                        ItemAttribute.MAGICAL_POWER
                        if magical_ability
                        else ItemAttribute.PHYSICAL_POWER
                    )
                    if attacking_god_stats.has_stat(
                        ItemAttribute.MAGICAL_POWER
                        if magical_ability
                        else ItemAttribute.PHYSICAL_POWER
                    )
                    else 0
                )
                amount = ability.total_base + ability.total_scaling * ability_power
                if has_crusher:
                    amount += og_power * 0.35 * (1.0 if bluestone_first else 0.5)
                if bluestone_flat:
                    amount += bluestone_flat * (1.0 if bluestone_first else 0.5)
                    if has_brooch:
                        amount += max(defending_health, 0) * 0.10 * (
                            1.0 if bluestone_first else 0.5
                        )
                    bluestone_first = False
                if has_heartseeker:
                    # 2% of max health, ramping to 6% at 250 Physical Power;
                    # follow-ups within 3s pay 75%.
                    ramp = (
                        0.02
                        if og_power <= 150
                        else min(0.02 + ((og_power - 150) / 100) * 0.04, 0.06)
                    )
                    amount += (
                        defending_god_stats.get_stat(ItemAttribute.HEALTH)
                        * ramp
                        * (1.0 if heartseeker_first else 0.75)
                    )
                    heartseeker_first = False
                # Titan's Bane: the first cast in each window carries 20%
                # penetration; with a rotation's cadence that is close to
                # every cast, so it rides them all, capped with the rest.
                extra_pen = 0.20 if has_titans and not magical_ability else 0.0
                pen_stat = (
                    ItemAttribute.MAGICAL_PENETRATION
                    if magical_ability
                    else ItemAttribute.PHYSICAL_PENETRATION
                )
                pen = (
                    attacking_god_stats.get_stat(pen_stat)
                    if attacking_god_stats.has_stat(pen_stat)
                    else _Penetration(0, 0)
                )
                dealt = BaseCalculator.damage_dealt(
                    amount,
                    defending_god_stats.get_stat(
                        ItemAttribute.MAGICAL_PROTECTION
                        if magical_ability
                        else ItemAttribute.PHYSICAL_PROTECTION
                    ),
                    red_pct if magical_ability != attacker_is_physical else 0,
                    steroid_prot_strip,
                    min(pen.percent + extra_pen, 0.40),
                    pen.flat,
                    damage_mit,
                    0,
                )
                defending_health -= dealt
                seconds += 0.4
                cast["ready"] = seconds + ability.cooldown * (1 - cdr)
                if has_hydras:
                    hydras_window = seconds + 8.0
                if has_polynomicon:
                    poly_window = seconds + 8.0
                if defending_health <= 0:
                    return seconds

            # Silverbranch: 3 Physical Power per 0.02 attack speed over the
            # 2.5 cap, itself capped at 120 bonus power.
            if has_silverbranch:
                overcap = max(attack_speed - 2.5, 0.0)
                attacking_god_stats.set_stat(
                    ItemAttribute.PHYSICAL_POWER,
                    og_power + min(3 * (overcap / 0.02), 120.0),
                )

            # Set prot reduction based on Executioner-style stacks
            if has_executioner:
                red_pct = 0.07 * executioner_stacks
            elif has_heavy_executioner:
                red_pct = 0.175 * executioner_stacks
            elif has_demonic_grip:
                red_pct = 0.10 * executioner_stacks

            # Reset Demon Blade's penetration if it's expired
            if seconds >= demon_blade_exp:
                attacking_god_stats.set_stat(ItemAttribute.PHYSICAL_PENETRATION, og_pen)
                demon_blade_exp = 0

            # Reset Berserker's effects if it's expired — back to the floor,
            # not to zero, or Oni Hunter's mitigation goes with it
            if seconds >= berserkers_exp:
                damage_mit = base_mit

            # Clear expired Renewal stacks
            renewal_stacks[:] = [
                s for s in renewal_stacks if not expire_renewal_stack(s)
            ]

            # Clear expired Midgardian stacks
            midgardian_stacks[:] = [s for s in midgardian_stacks if not expire_stack(s)]

            # Overcapped attack speed feeds Silverbranch, but swings still
            # come at most 2.5 a second — 1.9 under Heavy Executioner, whose
            # heavy swings trade rate for its doubled protection strip.
            fire_rate = min(attack_speed, 1.9 if has_heavy_executioner else 2.5)

            # Hecate ramps the attacker's power and Tahuti pays out against a
            # wounded target; both recomputed from the original so neither
            # compounds on itself or survives a Renewal heal it shouldn't.
            if (has_hecate or has_tahuti) and og_magical_power:
                multiplier = 1.0 + 0.05 * hecate_stacks
                if has_tahuti and defending_health < initial_health * 0.6:
                    multiplier *= 1.15
                attacking_god_stats.set_stat(
                    ItemAttribute.MAGICAL_POWER, og_magical_power * multiplier
                )

            # Calculate damage
            if progression is not None and progression.has_progression:
                dmg, is_crit = DamageCalculator.calculate_basic_damage_dealt(
                    attacking_god,
                    defending_god,
                    attacking_god_stats,
                    defending_god_stats,
                    progression.damage[p_idx],
                    red_pct,
                    damage_mit,
                    crit_bonus,
                    red_flat=steroid_prot_strip,
                )
                seconds += (1 / fire_rate) * progression.swing_time[p_idx]
                p_idx = 1 + p_idx if p_idx < len(progression.damage) else 0
            else:
                dmg, is_crit = DamageCalculator.calculate_basic_damage_dealt(
                    attacking_god,
                    defending_god,
                    attacking_god_stats,
                    defending_god_stats,
                    pct_red=red_pct,
                    damage_mit=damage_mit,
                    crit_bonus=crit_bonus,
                    red_flat=steroid_prot_strip,
                )
                seconds += 1 / fire_rate

            # Hydra's empowered basic: +25% on the first swing after a cast.
            if has_hydras and pre_fire_seconds < hydras_window:
                dmg *= 1.25
                hydras_window = 0.0

            # Polynomicon: the first basic within 8s of a cast carries 75% of
            # magical power as magical damage, at most once every 2s.
            if (
                has_polynomicon
                and pre_fire_seconds < poly_window
                and pre_fire_seconds >= poly_icd
            ):
                poly_icd = pre_fire_seconds + 2.0
                poly_window = 0.0
                dmg += proc_damage(
                    0.75
                    * (
                        attacking_god_stats.get_stat(ItemAttribute.MAGICAL_POWER)
                        if attacking_god_stats.has_stat(ItemAttribute.MAGICAL_POWER)
                        else 0
                    ),
                    True,
                    red_pct,
                    damage_mit,
                )

            if steroid_flat:
                dmg += proc_damage(
                    steroid_flat, not attacker_is_physical, red_pct, damage_mit
                )

            hits_landed += 1
            swing_interval = seconds - pre_fire_seconds

            # "Basic Attack Power" as the wiki defines it — the full computed
            # basic before crit — which is what O-bow and Duality scale from.
            if has_obow or has_duality:
                power_type = (
                    ItemAttribute.PHYSICAL_POWER
                    if attacker_is_physical
                    else ItemAttribute.MAGICAL_POWER
                )
                basic_attack_power = BaseCalculator.basic_attack_damage(
                    attacking_god.god.stats.basic_attack.base_damage,
                    attacking_god.god.stats.basic_attack.per_level,
                    attacking_god.level,
                    attacking_god_stats.get_stat(power_type)
                    if attacking_god_stats.has_stat(power_type)
                    else 0,
                    attacking_god.god.stats.basic_attack.scaling,
                )
                if has_obow and hits_landed % 4 == 0:
                    dmg += proc_damage(
                        15 + 0.60 * basic_attack_power,
                        not attacker_is_physical,
                        red_pct,
                        damage_mit,
                    )
                if has_duality and pre_fire_seconds >= duality_ready:
                    duality_ready = pre_fire_seconds + 3
                    dmg += proc_damage(
                        0.30 * basic_attack_power, False, red_pct, damage_mit
                    )

            if has_telkhines:
                dmg += proc_damage(telkhines_bonus, True, red_pct, damage_mit)

            # Cyclopean's clock runs on both time and hits: 8s, less 2s for
            # every basic landed on a god — the proc's own hit included.
            if has_cyclopean:
                if cyclopean_cd <= 0:
                    dmg += proc_damage(
                        defending_god_stats.get_stat(ItemAttribute.HEALTH) * 0.09,
                        True,
                        red_pct,
                        damage_mit,
                    )
                    cyclopean_cd = 8.0
                cyclopean_cd -= swing_interval + 2.0

            # Manikin burns tick at 30 a second each, up to four running.
            if has_manikin:
                manikin_burns[:] = [b for b in manikin_burns if b > pre_fire_seconds]
                if len(manikin_burns) < 4:
                    manikin_burns.append(pre_fire_seconds + 2.0)
                dmg += proc_damage(
                    30.0 * len(manikin_burns) * swing_interval,
                    not attacker_is_physical,
                    red_pct,
                    damage_mit,
                )

            if has_hecate:
                hecate_stacks = min(hecate_stacks + 1, 3)

            regenerated_health = (defending_hp5 / 5) * (seconds - pre_fire_seconds)
            defending_health -= dmg
            defending_health += regenerated_health

            # Proc Demon Blade: +10% Penetration for 4s, refreshed by every
            # crit, and capped at the 40% percent-pen ceiling — `max` here
            # used to hand any Demon Blade crit at least 40%.
            if is_crit and has_demon_blade:
                attacking_god_stats.set_stat(
                    ItemAttribute.PHYSICAL_PENETRATION,
                    _Penetration(og_pen.flat, min(og_pen.percent + 0.10, 0.40)),
                )

                demon_blade_exp = pre_fire_seconds + 4

            # Update Executioner-style stacks
            if has_executioner:
                executioner_stacks = min(executioner_stacks + 1, 4)
            elif has_heavy_executioner:
                executioner_stacks = min(executioner_stacks + 1, 2)
            elif has_demonic_grip:
                executioner_stacks = min(executioner_stacks + 1, 3)

            # Proc defender's Berserker's: Berserk triggers *below* 60%
            # health for 5% mitigation (and attack speed a defender that
            # never swings back cannot use), once every 15s.
            if (
                has_berserkers
                and defending_health < initial_health * 0.6
                and pre_fire_seconds >= berserkers_cd
            ):
                berserkers_exp = pre_fire_seconds + 5
                berserkers_cd = pre_fire_seconds + 15
                damage_mit = base_mit + 0.05

            # Proc Mail of Renewal: at most one stack a second, +5 of both
            # protections each, four stacks; the hit that lands at max stacks
            # consumes them to heal 10% max health, once every 30s.
            if has_mail_of_renewal and pre_fire_seconds >= renewal_exp:
                renewal_exp = pre_fire_seconds + 1
                if len(renewal_stacks) < 4:
                    renewal_stacks.append(pre_fire_seconds + 5)
                    defending_god_stats.add_or_set_stat(
                        ItemAttribute.MAGICAL_PROTECTION, 5
                    )
                    defending_god_stats.add_or_set_stat(
                        ItemAttribute.PHYSICAL_PROTECTION, 5
                    )
                elif pre_fire_seconds >= renewal_cd:
                    defending_health += (
                        defending_god_stats.get_stat(ItemAttribute.HEALTH) * 0.10
                    )
                    renewal_cd = pre_fire_seconds + 30
                    # The heal consumes the stacks, protections included —
                    # clearing the list without subtracting made them
                    # permanent.
                    for _ in renewal_stacks:
                        defending_god_stats.add_or_set_stat(
                            ItemAttribute.MAGICAL_PROTECTION, -5
                        )
                        defending_god_stats.add_or_set_stat(
                            ItemAttribute.PHYSICAL_PROTECTION, -5
                        )
                    renewal_stacks = []

            # Proc Midgardian Mail: 8% attack speed per stack for 3s, up to
            # four stacks.
            if has_midgardian_mail and len(midgardian_stacks) < 4:
                midgardian_stacks.append(pre_fire_seconds + 3)

        return seconds
