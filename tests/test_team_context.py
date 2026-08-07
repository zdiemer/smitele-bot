"""Reading the lobby, and what both games do about it.

The build changes here are the ones that are wrong in a specific, expensive way
when ignored: an even protection split against a team that deals one kind of
damage, no anti-heal against a healer, tenacity against a team with no crowd
control.
"""

from __future__ import annotations

import zlib

import pytest

import smite2_stats
import team_context
from build_optimizer import BuildArchetype, BuildOptimizer
from god import God, GodStat, GodStats
from god_types import GodPro, GodRole, GodType
from item import Item, ItemAttribute, ItemProperty, ItemType
from HirezAPI import PlayerRole
from smite2_optimizer import Smite2BuildOptimizer


def _stable_id(name: str) -> int:
    """A deterministic id for a fake item or god.

    Not `hash()`: Python randomises string hashing per process, so ids
    built from it differ between runs. Scoring breaks ties on id, and a
    fake catalogue hits ties often — every item past the point a target
    saturates scores identically — so hash-derived ids made these tests
    pass or fail depending on the seed the interpreter happened to start
    with."""
    return zlib.crc32(name.encode()) % 10_000_000



class _BasicAttack:
    """The shape `God.get_stat_at_level` expects for BASIC_ATTACK_DAMAGE."""

    base_damage = 0.0
    per_level = 0.0
    scaling = 0.0
    base_damage_back = 0.0
    per_level_back = 0.0
    scaling_back = 0.0
    progression = None


def smite2_god(name="S2", specs=None, god_type=GodType.MAGICAL):
    god = God()
    god.name = name
    god.id = _stable_id(name)
    god.type = god_type
    god.role = None
    god.scaling = "int"
    god.role_scaling = {}
    god.positions = [PlayerRole.SUPPORT]
    god.specs = specs or []
    god.stats = GodStats()
    god.stats.values = {ItemAttribute.HEALTH: GodStat(2000, 0, curve=[2000] * 20)}
    god.stats.basic_attack = _BasicAttack()
    return god


def smite1_god(name="S1", pros=None, role=GodRole.GUARDIAN, god_type=GodType.MAGICAL):
    god = God()
    god.name = name
    god.id = _stable_id(name)
    god.type = god_type
    god.role = role
    god.pros = pros or []
    god.stats = GodStats()
    god.stats.values = {}
    god.stats.basic_attack = _BasicAttack()
    return god


def item(name, properties=None, passive=None):
    made = Item()
    made.name = name
    made.id = _stable_id(name)
    made.tier = 3
    made.price = made.total_cost = 2500
    made.active = True
    made.is_starter = False
    made.type = ItemType.ITEM
    made.item_properties = properties or []
    made.passive = passive
    made.icon_url = ""
    made.restricted_roles = []
    made.glyph = False
    return made


class TestReadingALobby:
    def test_no_lobby_assumes_an_even_split(self):
        context = team_context.read()
        assert context.physical_share == 0.5
        assert not context.known

    def test_damage_split_follows_the_enemy_types(self):
        enemies = [smite2_god(f"P{i}", god_type=GodType.PHYSICAL) for i in range(3)]
        enemies.append(smite2_god("M", god_type=GodType.MAGICAL))
        assert team_context.read(enemies).physical_share == pytest.approx(0.75)

    def test_healers_are_read_from_either_games_vocabulary(self):
        assert team_context.read([smite2_god(specs=["Healing"])]).enemy_healers == 1
        assert (
            team_context.read([smite1_god(pros=[GodPro.HIGH_SUSTAIN])]).enemy_healers
            == 1
        )

    def test_crowd_control_is_read_from_either_games_vocabulary(self):
        assert (
            team_context.read([smite2_god(specs=["Lockdown"])]).enemy_crowd_control == 1
        )
        assert (
            team_context.read(
                [smite1_god(pros=[GodPro.HIGH_CROWD_CONTROL])]
            ).enemy_crowd_control
            == 1
        )

    def test_allied_front_lines_are_counted(self):
        allies = [smite2_god(specs=["Tank"]), smite1_god(role=GodRole.GUARDIAN)]
        assert team_context.read(allies=allies).allied_tanks == 2

    def test_unresolved_gods_are_skipped_not_fatal(self):
        context = team_context.read([None, smite2_god(god_type=GodType.PHYSICAL)])
        assert context.enemy_count == 1
        assert context.physical_share == 1.0


class TestProtectionScales:
    def test_an_even_team_changes_nothing(self):
        enemies = [
            smite2_god("P", god_type=GodType.PHYSICAL),
            smite2_god("M", god_type=GodType.MAGICAL),
        ]
        physical, magical = team_context.protection_scales(team_context.read(enemies))
        assert physical == pytest.approx(1.0)
        assert magical == pytest.approx(1.0)

    def test_the_pair_always_averages_one(self):
        """Tilting the split must aim the defensive budget, not enlarge it."""
        for physical_count in range(6):
            enemies = [
                smite2_god(f"P{i}", god_type=GodType.PHYSICAL)
                for i in range(physical_count)
            ] + [
                smite2_god(f"M{i}", god_type=GodType.MAGICAL)
                for i in range(5 - physical_count)
            ]
            physical, magical = team_context.protection_scales(
                team_context.read(enemies)
            )
            assert (physical + magical) / 2 == pytest.approx(1.0)

    def test_an_all_physical_team_still_leaves_magical_protection(self):
        """One magical ultimate does not care that the rest are physical."""
        enemies = [smite2_god(f"P{i}", god_type=GodType.PHYSICAL) for i in range(5)]
        _physical, magical = team_context.protection_scales(team_context.read(enemies))
        assert magical > 0.3


class TestSmite2AimsAtTheLobby:
    @staticmethod
    def catalogue():
        items = [
            item(
                f"Prot{i}",
                [
                    ItemProperty(ItemAttribute.PHYSICAL_PROTECTION, flat_value=50),
                    ItemProperty(ItemAttribute.HEALTH, flat_value=300),
                ],
            )
            for i in range(6)
        ]
        items += [
            item(
                f"Magic{i}",
                [
                    ItemProperty(ItemAttribute.MAGICAL_PROTECTION, flat_value=50),
                    ItemProperty(ItemAttribute.HEALTH, flat_value=300),
                ],
            )
            for i in range(6)
        ]
        # Deliberately the worst item here on its stat line alone, so picking it
        # can only be the anti-heal rule and never the scoring.
        items.append(
            item(
                "Anti-heal",
                [ItemProperty(ItemAttribute.PHYSICAL_PROTECTION, flat_value=5)],
                passive="On God Damage Dealt: Apply 25% Healing Reduction for 5s",
            )
        )
        return {made.id: made for made in items}

    def test_protection_targets_follow_the_enemy_damage(self):
        physical_team = [
            smite2_god(f"P{i}", god_type=GodType.PHYSICAL) for i in range(4)
        ]
        against = Smite2BuildOptimizer(
            smite2_god(), {}, context=team_context.read(physical_team)
        )
        even = Smite2BuildOptimizer(smite2_god(), {})
        assert (
            against.flat_targets[ItemAttribute.PHYSICAL_PROTECTION]
            > even.flat_targets[ItemAttribute.PHYSICAL_PROTECTION]
        )
        assert (
            against.flat_targets[ItemAttribute.MAGICAL_PROTECTION]
            < even.flat_targets[ItemAttribute.MAGICAL_PROTECTION]
        )

    def test_anti_heal_is_bought_against_a_healer(self):
        """Weighting alone never buys it: anti-heal is capped at 25% and does
        not stack, so it reads as a small bonus and loses to bigger numbers."""
        items = self.catalogue()
        healers = [smite2_god("H", specs=["Healing"])]
        build = Smite2BuildOptimizer(
            smite2_god(), items, context=team_context.read(healers)
        ).optimize()
        assert any(smite2_stats.carries_anti_heal(made) for made in build)

    def test_anti_heal_is_not_bought_without_a_healer(self):
        build = Smite2BuildOptimizer(smite2_god(), self.catalogue()).optimize()
        assert not any(smite2_stats.carries_anti_heal(made) for made in build)

    def test_tenacity_rises_with_enemy_crowd_control(self):
        crowd = [smite2_god(f"C{i}", specs=["Lockdown"]) for i in range(4)]
        against = Smite2BuildOptimizer(
            smite2_god(), {}, context=team_context.read(crowd)
        )
        even = Smite2BuildOptimizer(smite2_god(), {})
        assert against.flat_targets.get(
            ItemAttribute.TENACITY, 0
        ) > even.flat_targets.get(ItemAttribute.TENACITY, 0)

    def test_an_allied_front_line_shifts_toward_damage(self):
        allies = [smite2_god("T", specs=["Tank"])]
        behind = Smite2BuildOptimizer(
            smite2_god(), {}, context=team_context.read(allies=allies)
        )
        alone = Smite2BuildOptimizer(smite2_god(), {})
        assert behind.balance < alone.balance

    def test_an_explicit_balance_is_not_second_guessed_by_allies(self):
        allies = [smite2_god("T", specs=["Tank"])]
        asked = Smite2BuildOptimizer(
            smite2_god(), {}, context=team_context.read(allies=allies), balance=0.85
        )
        assert asked.balance == 0.85


class TestSmite2AntiHealParsing:
    def test_apply_phrasing_is_read(self):
        """`Apply 25% Healing Reduction` has no leading plus, which is why the
        general percentage pattern missed all four items that have it."""
        made = item("X", passive="On God Damage Dealt: Apply 25% Healing Reduction for 5s")
        assert smite2_stats.carries_anti_heal(made)
        assert smite2_stats.passive_stats(made).get_percent(
            ItemAttribute.HEAL_REDUCTION
        ) == pytest.approx(0.25)

    def test_an_item_without_it_is_not_claimed(self):
        assert not smite2_stats.carries_anti_heal(item("Y", passive="+20% Penetration"))


class TestSmite1AimsAtTheLobby:
    def test_protection_targets_follow_the_enemy_damage(self):
        physical_team = [
            smite1_god(f"P{i}", god_type=GodType.PHYSICAL) for i in range(4)
        ]
        against = BuildOptimizer(
            smite1_god(), [], {}, context=team_context.read(physical_team)
        )
        even = BuildOptimizer(smite1_god(), [], {})
        archetype = BuildArchetype.SUPPORT_GUARDIAN
        against_targets = against._BuildOptimizer__archetype_stat_targets[archetype]
        even_targets = even._BuildOptimizer__archetype_stat_targets[archetype]
        assert (
            against_targets[ItemAttribute.PHYSICAL_PROTECTION]
            > even_targets[ItemAttribute.PHYSICAL_PROTECTION]
        )
        assert (
            against_targets[ItemAttribute.MAGICAL_PROTECTION]
            < even_targets[ItemAttribute.MAGICAL_PROTECTION]
        )

    def test_a_healer_makes_anti_heal_required(self):
        healers = [smite1_god("H", pros=[GodPro.HIGH_SUSTAIN])]
        optimizer = BuildOptimizer(
            smite1_god(), [], {}, context=team_context.read(healers)
        )
        from passive_parser import PassiveAttribute

        assert (
            PassiveAttribute.ANTIHEAL
            in optimizer._BuildOptimizer__required_passives
        )

    def test_no_healer_requires_nothing(self):
        optimizer = BuildOptimizer(smite1_god(), [], {})
        assert not optimizer._BuildOptimizer__required_passives


class TestBalance:
    def test_warriors_default_to_a_bruiser_split(self):
        """They optimized to six defensive items before this."""
        assert (
            BuildArchetype.ABILITY_BASED_WARRIOR
            in __import__("build_optimizer").ARCHETYPE_BALANCE
        )

    def test_guardians_are_left_alone(self):
        """A support that builds like a tank is correct."""
        balances = __import__("build_optimizer").ARCHETYPE_BALANCE
        assert BuildArchetype.SUPPORT_GUARDIAN not in balances
        assert BuildArchetype.SOLO_GUARDIAN not in balances

    def test_smite2_tilts_toward_defence_when_asked(self):
        tanky = Smite2BuildOptimizer(smite2_god(), {}, balance=0.85)
        damage = Smite2BuildOptimizer(smite2_god(), {}, balance=0.15)
        assert (
            tanky.flat_targets[ItemAttribute.HEALTH]
            > damage.flat_targets[ItemAttribute.HEALTH]
        )
        assert (
            tanky.flat_targets[ItemAttribute.INTELLIGENCE]
            < damage.flat_targets[ItemAttribute.INTELLIGENCE]
        )

    def test_neutral_stats_are_not_tilted(self):
        """A bruiser wants its cooldowns as much as a tank does."""
        tanky = Smite2BuildOptimizer(smite2_god(), {}, balance=0.85)
        damage = Smite2BuildOptimizer(smite2_god(), {}, balance=0.15)
        assert tanky.flat_targets[ItemAttribute.COOLDOWN_RATE] == pytest.approx(
            damage.flat_targets[ItemAttribute.COOLDOWN_RATE]
        )
