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
        return (
            (crit_bonus if is_crit else 1)
            * progression
            * (base + (per_level * level) + (scaling * power))
        )

    @staticmethod
    def protections(
        prots: float, red_pct: float, red_flat: float, pen_pct: float, pen_flat: int
    ):
        return (prots * (1 - red_pct) - red_flat) * (1 - pen_pct) - pen_flat

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
                    attack_speed = self.god.god.get_stat_at_level(stat, 20)
                    value = attack_speed + attack_speed * value
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
            magic_power = stats.get_stat(ItemAttribute.MAGICAL_POWER)
            crit_chance = (
                (0.15 + (int((magic_power - 150) / 10) * 0.01))
                if magic_power >= 150
                else 0
            )
            stats.set_stat(ItemAttribute.CRITICAL_STRIKE_CHANCE, max(0.70, crit_chance))
        for item in self.god.build:
            # Evolved Transcendence
            if item.id == 15767:
                mana = stats.get_stat(ItemAttribute.MANA)
                stats.add_or_set_stat(ItemAttribute.PHYSICAL_POWER, mana * 0.03)
            # The Heavy Executioner
            if item.id == 22960:
                attack_speed = stats.get_stat(ItemAttribute.ATTACK_SPEED)
                stats.set_stat(ItemAttribute.ATTACK_SPEED, min(1.9, attack_speed))
            # Silverbranch Bow
            if item.id == 14084:
                attack_speed = stats.get_stat(ItemAttribute.ATTACK_SPEED)
                cap = self.FLAT_ITEM_ATTRIBUTE_CAPS[ItemAttribute.ATTACK_SPEED]
                if attack_speed > cap:
                    stats.add_or_set_stat(
                        ItemAttribute.PHYSICAL_POWER, 2 * ((attack_speed - cap) / 0.02)
                    )
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
    ) -> Tuple[float, bool]:
        power_type = (
            ItemAttribute.PHYSICAL_POWER
            if attacking_god.god.type == GodType.PHYSICAL
            else ItemAttribute.MAGICAL_POWER
        )

        crit_chance = 0
        has_qins = False
        for item in attacking_god.build:
            # Deathbringer and glyphs
            if item.id in (7545, 21500, 21501):
                # Deathbringer increase Critical Strike bonus damage by 25%
                crit_bonus += 0.25
            # Qin's Sais
            if item.id == 7593:
                has_qins = True

        for item in defending_god.build:
            # Evolved Prophetic Cloak
            if item.id == 24172:
                total_prots = defending_stats.get_stat(
                    ItemAttribute.MAGICAL_PROTECTION
                ) + defending_stats.get_stat(ItemAttribute.PHYSICAL_PROTECTION)

                if total_prots > 600:
                    damage_mit += 0.20
                elif total_prots > 400:
                    damage_mit += 0.10
            # Sigil of the Old Guard
            if item.id == 19752:
                damage_mit += 0.05

        if (
            attacking_god.god.type == GodType.PHYSICAL
            or attacking_god.god.id == GodId.OLORUN
        ):
            if attacking_stats.has_stat(ItemAttribute.CRITICAL_STRIKE_CHANCE):
                crit_chance = attacking_stats.get_stat(
                    ItemAttribute.CRITICAL_STRIKE_CHANCE
                )

        is_crit = random.randrange(0, 100) < (crit_chance * 100)

        total_basic_damage = BaseCalculator.basic_attack_damage(
            attacking_god.god.stats.basic_attack.base_damage,
            attacking_god.god.stats.basic_attack.per_level,
            attacking_god.level,
            attacking_stats.get_stat(power_type),
            attacking_god.god.stats.basic_attack.scaling,
            progression,
            is_crit,
            crit_bonus,
        )

        if has_qins:
            defending_health = defending_stats.get_stat(ItemAttribute.HEALTH)
            qins_bonus = (
                0.03
                if defending_health < 2000
                else max(0.035 + (((defending_health - 2000) / 250) * 0.005), 0.05)
            )
            total_basic_damage += defending_health * qins_bonus

        if attacking_god.god.id == GodId.IZANAMI:
            is_crit = random.randrange(0, 100) < (crit_chance * 100)
            total_basic_damage += BaseCalculator.basic_attack_damage(
                attacking_god.god.stats.basic_attack.base_damage_back,
                attacking_god.god.stats.basic_attack.per_level_back,
                attacking_god.level,
                attacking_stats.get_stat(power_type),
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
            0,
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
    ) -> float:
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
        for item in attacking_god.build:
            # Death's Temper
            if item.id == 19587:
                if assume_item_passives_stacked:
                    basic_damage = attacking_god_stats.get_stat(
                        ItemAttribute.BASIC_ATTACK_DAMAGE
                    )
                    # 3.5% increased Basic Attack Damage, stacking 10 times
                    attacking_god_stats.set_stat(
                        ItemAttribute.BASIC_ATTACK_DAMAGE,
                        basic_damage + basic_damage * 0.035 * 10,
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
            # The Executioner, The Ferocious Executioner
            if item.id in (7575, 23135):
                has_executioner = True
            # The Heavy Executioner
            if item.id == 22960:
                has_heavy_executioner = True

        has_berserkers = False
        has_mail_of_renewal = False
        has_midgardian_mail = False
        has_oni_hunters = False
        has_spectral = False
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
            # Spectral Armor
            if item.id == 24187:
                print("Has spectral")
                has_spectral = True
            # Sentinel's Embrace
            if item.id == 19627:
                defending_god_stats.add_or_set_stat(
                    ItemAttribute.MAGICAL_PROTECTION, 40
                )
                defending_god_stats.add_or_set_stat(
                    ItemAttribute.PHYSICAL_PROTECTION, 40
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

        spectral_stacks: List[float] = []

        dmg = 0
        damage_mit = 0
        crit_bonus = 1.75
        is_crit = False

        if has_oni_hunters:
            damage_mit += 0.04

        if has_spectral:
            crit_bonus -= 0.40

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

        base_attack_speed = attacking_god_stats.get_stat(ItemAttribute.ATTACK_SPEED)
        if ItemAttribute.ATTACK_SPEED in attacking_god_stats.overcapped_stats:
            base_attack_speed = attacking_god_stats.overcapped_stats[
                ItemAttribute.ATTACK_SPEED
            ]

        def expire_renewal_stack(exp_time: float) -> bool:
            if seconds >= exp_time:
                defending_god_stats.add_or_set_stat(
                    ItemAttribute.MAGICAL_PROTECTION, -4
                )
                defending_god_stats.add_or_set_stat(
                    ItemAttribute.PHYSICAL_PROTECTION, -4
                )
                return True
            return False

        def expire_spectral_stack(exp_time: float) -> bool:
            if seconds >= exp_time:
                return True
            return False

        def expire_stack(exp_time: float) -> bool:
            if seconds >= exp_time:
                return True
            return False

        while defending_health > 0:
            pre_fire_seconds = seconds
            red_pct = 0
            attack_speed = BaseCalculator.attack_speed(
                base_attack_speed,
                increase=0.10 if seconds < demon_blade_exp else 0,
                decrease=len(midgardian_stacks) * 0.08,
            )

            # Update Silverbranch passive according to increase and decrease in attack speed
            if has_silverbranch:
                if base_attack_speed > attack_speed > 2.5:
                    attacking_god_stats.set_stat(
                        ItemAttribute.PHYSICAL_POWER,
                        og_power + 2 * ((attack_speed - 2.5) / 0.02),
                    )
                elif attack_speed <= 2.5 < base_attack_speed:
                    attacking_god_stats.add_or_set_stat(
                        ItemAttribute.PHYSICAL_POWER,
                        -2 * ((base_attack_speed - 2.5 / 0.02)),
                    )

            # Set prot reduction based on Executioner stacks
            if has_executioner:
                red_pct = 0.07 * executioner_stacks
            elif has_heavy_executioner:
                red_pct = 0.175 * executioner_stacks

            # Reset Demon Blade effects if it's expired
            if seconds >= demon_blade_exp:
                attacking_god_stats.set_stat(ItemAttribute.PHYSICAL_PENETRATION, og_pen)
                attacking_god_stats.set_stat(
                    ItemAttribute.ATTACK_SPEED, min(base_attack_speed, 2.5)
                )
                demon_blade_exp = 0

            # Reset Berserker's effects if it's expired
            if seconds >= berserkers_exp:
                damage_mit = 0

            # Clear expired Renewal stacks
            renewal_stacks[:] = [
                s for s in renewal_stacks if not expire_renewal_stack(s)
            ]

            # Clear expired Midgardian stacks
            midgardian_stacks[:] = [s for s in midgardian_stacks if not expire_stack(s)]

            # Clear expired Spectral stacks
            pre_expire_count = len(spectral_stacks)
            spectral_stacks[:] = [
                s for s in spectral_stacks if not expire_spectral_stack(s)
            ]
            expire_diff = pre_expire_count - len(spectral_stacks)
            if expire_diff > 0:
                crit_bonus += 0.05 * expire_diff

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
                )
                seconds += (1 / attack_speed) * progression.swing_time[p_idx]
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
                )
                seconds += 1 / attack_speed

            regenerated_health = (defending_hp5 / 5) * (seconds - pre_fire_seconds)
            defending_health -= dmg
            defending_health += regenerated_health

            # Proc Demon Blade
            if is_crit and has_demon_blade and demon_blade_exp == 0:
                # Add 10% Penetration without overcappingo on % Pen
                attacking_god_stats.set_stat(
                    ItemAttribute.PHYSICAL_PENETRATION,
                    _Penetration(og_pen.flat, max(og_pen.percent + 0.10, 0.40)),
                )

                demon_blade_exp = pre_fire_seconds + 4

            # Update Executioenr stacks
            if has_executioner:
                executioner_stacks = min(executioner_stacks + 1, 4)
            elif has_heavy_executioner:
                executioner_stacks = min(executioner_stacks + 1, 2)

            # Proc defender's Berserker's
            if (
                has_berserkers
                and defending_health > initial_health / 2
                and pre_fire_seconds >= berserkers_cd
            ):
                berserkers_exp = pre_fire_seconds + 5
                berserkers_cd = pre_fire_seconds + 15
                damage_mit = 0.10

            # Proc Mail of Renewal
            if (
                has_mail_of_renewal
                and pre_fire_seconds >= renewal_exp
                and pre_fire_seconds >= renewal_cd
            ):
                renewal_exp = pre_fire_seconds + 1
                renewal_stacks.append(pre_fire_seconds + 5)
                defending_god_stats.add_or_set_stat(ItemAttribute.MAGICAL_PROTECTION, 4)
                defending_god_stats.add_or_set_stat(
                    ItemAttribute.PHYSICAL_PROTECTION, 4
                )
                if len(renewal_stacks) == 5:
                    defending_health += (
                        defending_god_stats.get_stat(ItemAttribute.HEALTH) * 0.15
                    )
                    renewal_cd = pre_fire_seconds + 60
                    renewal_stacks = []

            # Proc Midgardian Mail
            if has_midgardian_mail and len(midgardian_stacks) < 3:
                midgardian_stacks.append(pre_fire_seconds + 2)

            # Proc Spectral
            if has_spectral and is_crit and len(spectral_stacks) < 4:
                crit_bonus -= 0.05
                spectral_stacks.append(pre_fire_seconds + 8)

        return seconds
