"""The Smite 2 optimizer: which stat a god builds, and what the scoring will
and will not spend a slot on.

The parts worth pinning are the ones that were wrong first time and produced
believable output anyway — a god built with the wrong damage stat, a build over
the penetration cap, a build costing more gold than a game hands out.
"""

from __future__ import annotations

import pytest

import smite2_stats
from god import God, GodStat, GodStats
from god_types import GodType
from item import Item, ItemAttribute, ItemProperty, ItemType
from HirezAPI import PlayerRole
from smite2_optimizer import Smite2BuildOptimizer, damage_stat, primary_role


def make_item(name, properties=None, tier=3, cost=2500, starter=False, relic=False):
    item = Item()
    item.name = name
    item.id = abs(hash(name)) % 10_000_000
    item.tier = tier
    item.price = cost
    item.total_cost = cost
    item.active = True
    item.is_starter = starter
    item.type = ItemType.RELIC if relic else ItemType.ITEM
    item.item_properties = properties or []
    item.passive = None
    item.icon_url = ""
    item.restricted_roles = []
    item.glyph = False
    return item


def make_god(
    name="Test",
    god_type=GodType.MAGICAL,
    scaling=None,
    role_scaling=None,
    positions=None,
    specs=None,
):
    god = God()
    god.name = name
    god.id = abs(hash(name)) % 100_000
    god.type = god_type
    god.role = None
    god.scaling = scaling
    god.role_scaling = role_scaling or {}
    god.positions = positions or []
    god.specs = specs or []
    god.stats = GodStats()
    god.stats.values = {ItemAttribute.HEALTH: GodStat(2000, 0, curve=[2000] * 20)}
    return god


def catalogue(items):
    return {item.id: item for item in items}


class TestDamageStat:
    def test_the_stores_lane_filter_wins(self):
        god = make_god(
            god_type=GodType.PHYSICAL,
            scaling="hybrid",
            role_scaling={
                PlayerRole.CARRY: ItemAttribute.STRENGTH,
                PlayerRole.MID: ItemAttribute.INTELLIGENCE,
            },
        )
        assert damage_stat(god, PlayerRole.CARRY) is ItemAttribute.STRENGTH
        assert damage_stat(god, PlayerRole.MID) is ItemAttribute.INTELLIGENCE

    def test_scaling_keyword_covers_lanes_the_store_does_not(self):
        god = make_god(god_type=GodType.PHYSICAL, scaling="int")
        assert damage_stat(god, PlayerRole.SOLO) is ItemAttribute.INTELLIGENCE

    def test_damage_type_is_the_last_resort(self):
        assert damage_stat(make_god(god_type=GodType.MAGICAL)) is ItemAttribute.INTELLIGENCE
        assert damage_stat(make_god(god_type=GodType.PHYSICAL)) is ItemAttribute.STRENGTH

    def test_a_physical_god_can_build_intelligence(self):
        """Neith and Danzaburou hit physically and are built with Intelligence
        in most lanes. Keying on damage type hands them a build nobody plays."""
        god = make_god(
            god_type=GodType.PHYSICAL,
            role_scaling={PlayerRole.MID: ItemAttribute.INTELLIGENCE},
        )
        assert damage_stat(god, PlayerRole.MID) is ItemAttribute.INTELLIGENCE

    def test_no_god_at_all_still_answers(self):
        assert damage_stat(None) is ItemAttribute.STRENGTH


class TestPrimaryRole:
    def test_explicit_role_wins(self):
        god = make_god(positions=[PlayerRole.SOLO])
        assert primary_role(god, PlayerRole.CARRY) is PlayerRole.CARRY

    def test_falls_back_to_the_first_published_position(self):
        assert primary_role(make_god(positions=[PlayerRole.JUNGLE])) is PlayerRole.JUNGLE

    def test_a_god_with_no_position_is_not_assumed_to_be_a_tank(self):
        assert primary_role(make_god()) is PlayerRole.MID


class TestProfiles:
    @pytest.mark.parametrize("role", list(PlayerRole))
    @pytest.mark.parametrize(
        "stat", [ItemAttribute.STRENGTH, ItemAttribute.INTELLIGENCE]
    )
    def test_every_lane_and_stat_resolves_to_a_profile(self, role, stat):
        """Not every pair was measured — no Strength mid cleared the sample
        floor — so each must fall back rather than raise."""
        god = make_god(scaling="str" if stat is ItemAttribute.STRENGTH else "int")
        optimizer = Smite2BuildOptimizer(god, {}, role=role)
        assert optimizer.flat_targets or optimizer.percent_targets

    def test_targets_never_exceed_the_games_caps(self):
        for role in PlayerRole:
            optimizer = Smite2BuildOptimizer(make_god(), {}, role=role)
            for attribute, cap in smite2_stats.FLAT_CAPS.items():
                assert optimizer.flat_targets.get(attribute, 0) <= cap
            for attribute, cap in smite2_stats.PERCENT_CAPS.items():
                assert optimizer.percent_targets.get(attribute, 0) <= cap


class TestScoring:
    def test_a_stat_stops_paying_at_its_target(self):
        optimizer = Smite2BuildOptimizer(make_god(), {}, role=PlayerRole.MID)
        target = optimizer.flat_targets[ItemAttribute.INTELLIGENCE]
        at_target = make_item(
            "At", [ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=target)]
        )
        over = make_item(
            "Over", [ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=target * 2)]
        )
        assert optimizer.score([over]) == pytest.approx(optimizer.score([at_target]))

    def test_a_stat_the_lane_does_not_want_is_worth_nothing(self):
        optimizer = Smite2BuildOptimizer(
            make_god(scaling="int"), {}, role=PlayerRole.MID
        )
        useless = make_item(
            "Plated", [ItemProperty(ItemAttribute.PLATED, flat_value=20)]
        )
        assert optimizer.score([useless]) == 0.0

    def test_flat_and_percent_penetration_are_scored_apart(self):
        """One shared reference valued ten flat Penetration at fifty times its
        worth, because flat is counted in points and percent in fractions."""
        optimizer = Smite2BuildOptimizer(
            make_god(scaling="int"), {}, role=PlayerRole.MID
        )
        flat = make_item(
            "Flat", [ItemProperty(ItemAttribute.PENETRATION, flat_value=10)]
        )
        percent = make_item(
            "Pct", [ItemProperty(ItemAttribute.PENETRATION, percent_value=0.10)]
        )
        # Neither may dwarf the other by an order of magnitude.
        assert 0.2 < optimizer.score([flat]) / optimizer.score([percent]) < 5


class TestSelection:
    @staticmethod
    def pool():
        items = []
        for index in range(12):
            items.append(
                make_item(
                    f"Int{index}",
                    [ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=70)],
                    cost=2500,
                )
            )
        items.append(make_item("Starter", [], tier=1, cost=1500, starter=True))
        items.append(make_item("Relic", [], tier=0, cost=0, relic=True))
        return items

    def test_picks_six_distinct_core_items(self):
        optimizer = Smite2BuildOptimizer(
            make_god(scaling="int"), catalogue(self.pool()), role=PlayerRole.MID
        )
        build = optimizer.optimize()
        assert len(build) == 6
        assert len({item.id for item in build}) == 6

    def test_never_picks_a_starter_or_relic_for_a_core_slot(self):
        optimizer = Smite2BuildOptimizer(
            make_god(scaling="int"), catalogue(self.pool()), role=PlayerRole.MID
        )
        for item in optimizer.optimize():
            assert not item.is_starter
            assert item.type is ItemType.ITEM

    def test_respects_the_gold_budget(self):
        """Six of the most expensive items in the game is not a build anyone
        can finish, and they are exactly the items with the biggest numbers."""
        expensive = [
            make_item(
                f"Big{index}",
                [ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=130)],
                cost=3500,
            )
            for index in range(12)
        ]
        optimizer = Smite2BuildOptimizer(
            make_god(scaling="int"),
            catalogue(expensive),
            role=PlayerRole.MID,
            budget=16_500,
        )
        assert optimizer.cost(optimizer.optimize()) <= 16_500

    def test_no_budget_means_no_constraint(self):
        expensive = [
            make_item(
                f"Big{index}",
                [ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=130)],
                cost=3500,
            )
            for index in range(8)
        ]
        optimizer = Smite2BuildOptimizer(
            make_god(scaling="int"), catalogue(expensive), role=PlayerRole.MID, budget=0
        )
        assert len(optimizer.optimize()) == 6

    def test_is_deterministic(self):
        items = catalogue(self.pool())
        first = Smite2BuildOptimizer(
            make_god(scaling="int"), items, role=PlayerRole.MID
        ).optimize()
        second = Smite2BuildOptimizer(
            make_god(scaling="int"), items, role=PlayerRole.MID
        ).optimize()
        assert [i.id for i in first] == [i.id for i in second]

    def test_sampling_stays_inside_the_pool_and_the_budget(self):
        import random

        items = catalogue(self.pool())
        optimizer = Smite2BuildOptimizer(
            make_god(scaling="int"), items, role=PlayerRole.MID
        )
        for seed in range(5):
            build = optimizer.sample(6, rng=random.Random(seed))
            assert len(build) == 6
            assert len({item.id for item in build}) == 6
            assert optimizer.cost(build) <= optimizer.budget

    def test_a_pool_smaller_than_a_build_does_not_raise(self):
        few = catalogue(
            [
                make_item(f"I{i}", [ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=70)])
                for i in range(3)
            ]
        )
        assert len(Smite2BuildOptimizer(make_god(), few).optimize()) == 3
