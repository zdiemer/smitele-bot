"""Generating builds: the Smite 2 randomizer and Aspect roll, and the Smite 1
archetype tables the optimizer reads.

The Smite 1 half is an audit rather than a behaviour test. Its findings were
that an enum value was duplicated, that three archetypes had no tables at all,
and that only hunters were ever really optimized; these keep all three from
coming back.
"""

from __future__ import annotations

import zlib

import random

import pytest

from build_optimizer import BuildArchetype, BuildOptimizer
from god import Aspect, God, GodStat, GodStats
from god_types import GodRole, GodType
from item import Item, ItemAttribute, ItemProperty, ItemType
from HirezAPI import PlayerRole
from game import Game
import god_builder
from god_builder import (
    BuildCommandType,
    BuildOptions,
    BuildPrioritization,
    GeneratedBuild,
    GodBuilder,
)


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


def make_item(name, properties=None, tier=3, cost=2500, starter=False, relic=False):
    item = Item()
    item.name = name
    item.id = _stable_id(name)
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


def make_god(name="Test", aspect=None, positions=None, scaling="int"):
    god = God()
    god.name = name
    god.id = _stable_id(name)
    god.type = GodType.MAGICAL
    god.role = None
    god.scaling = scaling
    god.role_scaling = {}
    god.positions = positions if positions is not None else [PlayerRole.MID]
    god.specs = ["Nuker"]
    god.aspect = aspect
    god.stats = GodStats()
    god.stats.values = {ItemAttribute.HEALTH: GodStat(2000, 0, curve=[2000] * 20)}
    return god


class FakeProvider:
    def __init__(self, gods, items, game=Game.SMITE_2):
        self.gods = gods
        self.items = items
        self.game = game

    def random_god_id(self):
        return next(iter(self.gods))


def smite2_catalogue():
    items = [
        make_item(f"Int{i}", [ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=70)])
        for i in range(14)
    ]
    items += [
        make_item(f"Tank{i}", [ItemProperty(ItemAttribute.HEALTH, flat_value=400)])
        for i in range(6)
    ]
    items.append(make_item("Starter", [], tier=1, cost=1500, starter=True))
    items.append(make_item("Relic", [], tier=0, cost=0, relic=True))
    return {item.id: item for item in items}


def build_for(god, items, **kwargs) -> GeneratedBuild:
    provider = FakeProvider({god.id: god}, items)
    builder = GodBuilder({god.id: god}, items, provider)
    options = BuildOptions(
        god_id=god.id,
        build_type=BuildCommandType.RANDOM,
        provider=provider,
        **kwargs,
    )
    return builder.random(options)


class TestSmite2Randomizer:
    def test_produces_six_items_a_starter_and_a_relic(self):
        result = build_for(make_god(), smite2_catalogue())
        assert len(result.build) == 6
        assert any(item.is_starter for item in result.relics)
        assert any(item.type is ItemType.RELIC for item in result.relics)

    def test_items_are_never_repeated(self):
        items = smite2_catalogue()
        for seed in range(10):
            random.seed(seed)
            result = build_for(make_god(), items)
            assert len({item.id for item in result.build}) == 6

    def test_draws_uniformly_rather_than_sensibly(self):
        """The one thing a randomiser must not do is produce good builds.

        An earlier version sampled the optimizer's shortlist, which made it
        `/optimize` with extra steps: the tank items never appeared for a
        damage god, because they scored badly. Every legal item has to be
        reachable.
        """
        god = make_god()
        items = smite2_catalogue()
        seen = set()
        for seed in range(60):
            random.seed(seed)
            seen.update(item.name for item in build_for(god, items).build)

        core = {
            item.name
            for item in items.values()
            if item.tier == 3 and not item.is_starter and item.type is ItemType.ITEM
        }
        # Including the ones a scoring optimizer would never pick for a mage.
        assert {name for name in core if name.startswith("Tank")} <= seen
        assert len(seen) == len(core)

    def test_claims_no_lane_it_did_not_build_for(self):
        god = make_god(positions=[PlayerRole.SOLO, PlayerRole.JUNGLE])
        random.seed(1)
        description = build_for(god, smite2_catalogue()).description
        for role in PlayerRole:
            assert role.value.title() not in description

    def test_does_not_always_return_the_same_build(self):
        items = smite2_catalogue()
        god = make_god()
        builds = set()
        for seed in range(10):
            random.seed(seed)
            builds.add(tuple(sorted(i.id for i in build_for(god, items).build)))
        assert len(builds) > 1

    def test_prioritizing_defense_avoids_pure_damage_items(self):
        random.seed(3)
        result = build_for(
            make_god(), smite2_catalogue(), prioritization=BuildPrioritization.DEFENSE
        )
        assert all(item.name.startswith("Tank") for item in result.build)

    def test_a_pool_too_small_to_build_from_fails_cleanly(self):
        items = {
            item.id: item
            for item in [
                make_item("Only", [ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=70)])
            ]
        }
        with pytest.raises(god_builder.BuildFailedError):
            build_for(make_god(), items)


class TestAspectRoll:
    ASPECT = Aspect(
        name="Aspect of Testing",
        description="Changes everything.",
        icon_url="https://example.invalid/aspect.png",
        changed_abilities={"1st Ability": object()},
    )

    def test_a_god_without_one_never_gets_one(self):
        items = smite2_catalogue()
        for seed in range(10):
            random.seed(seed)
            assert build_for(make_god(aspect=None), items).aspect is None

    def test_a_god_with_one_sometimes_gets_it_and_sometimes_does_not(self):
        """It is the choice the game offers at god select, so both outcomes are
        real answers."""
        god = make_god(aspect=self.ASPECT)
        items = smite2_catalogue()
        rolled = set()
        for seed in range(30):
            random.seed(seed)
            rolled.add(build_for(god, items).aspect is not None)
        assert rolled == {True, False}

    def test_the_description_carries_the_aspects_details(self):
        god = make_god(aspect=self.ASPECT)
        items = smite2_catalogue()
        for seed in range(30):
            random.seed(seed)
            result = build_for(god, items)
            if result.aspect is None:
                continue
            assert "Aspect of Testing" in result.description
            assert "Changes everything." in result.description
            assert "1st Ability" in result.description
            return
        pytest.fail("never rolled an Aspect")

    def test_not_rolling_one_is_said_out_loud(self):
        god = make_god(aspect=self.ASPECT)
        items = smite2_catalogue()
        for seed in range(30):
            random.seed(seed)
            result = build_for(god, items)
            if result.aspect is None:
                assert "without an Aspect" in result.description
                return
        pytest.fail("always rolled an Aspect")

    def test_the_roll_carries_the_icon_for_the_embed_to_badge(self):
        god = make_god(aspect=self.ASPECT)
        items = smite2_catalogue()
        for seed in range(30):
            random.seed(seed)
            result = build_for(god, items)
            if result.aspect is not None:
                assert result.aspect.icon_url
                return
        pytest.fail("never rolled an Aspect")


class TestSmite1PassiveValue:
    """Smite 1 parsed its passives into attributes and never valued them, so an
    item whose whole point is its passive scored as though it had none."""

    @staticmethod
    def optimizer():
        god = God()
        god.name = "Audit"
        god.id = 1
        god.role = GodRole.MAGE
        god.type = GodType.MAGICAL
        god.pros = []
        god.stats = GodStats()
        god.stats.values = {}
        god.stats.basic_attack = _BasicAttack()
        return BuildOptimizer(god, [], {})

    def test_an_item_with_no_passive_is_worth_nothing_extra(self):
        item = make_item("Plain")
        item.passive_properties = set()
        assert self.optimizer().passive_score(item) == 0.0

    def test_a_real_passive_beats_a_filler_one(self):
        from passive_parser import PassiveAttribute

        strong = make_item("Strong")
        strong.passive_properties = {PassiveAttribute.ANTIHEAL}
        filler = make_item("Filler")
        filler.passive_properties = {PassiveAttribute.STACKS}
        optimizer = self.optimizer()
        assert optimizer.passive_score(strong) > optimizer.passive_score(filler)

    def test_how_a_passive_arrives_is_worth_nothing_by_itself(self):
        """Stacking and evolving describe delivery, not effect, and the effect
        they gate is already counted under its own attribute."""
        from passive_parser import PassiveAttribute

        for attribute in (
            PassiveAttribute.STACKS,
            PassiveAttribute.EVOLVES_WITH_GOD_KILLS,
            PassiveAttribute.EVOLVES_WITH_MINION_KILLS,
        ):
            item = make_item(f"Delivery{attribute.value}")
            item.passive_properties = {attribute}
            assert self.optimizer().passive_score(item) == 0.0

    def test_an_evolved_item_inherits_its_parents_passive(self):
        """Evolutions often carry no passive of their own; scoring them as
        passive-less is exactly the bug this exists to fix."""
        from passive_parser import PassiveAttribute

        parent = make_item("Parent")
        parent.passive_properties = {PassiveAttribute.ANTIHEAL}
        evolved = make_item("Evolved", tier=4)
        evolved.passive_properties = set()
        evolved.parent_item_id = parent.id

        god = self.optimizer().god
        optimizer = BuildOptimizer(god, [], {parent.id: parent})
        assert optimizer.passive_score(evolved) > 0


class TestSmite1ArchetypeTables:
    def test_no_two_archetypes_share_a_value(self):
        """An Enum turns a duplicated value into an alias, so
        ATTACK_SPEED_STIM_HUNTER *was* HEALER_GUARDIAN — same member, same
        targets, same weights."""
        names = list(BuildArchetype.__members__)
        assert len(names) == len(set(BuildArchetype))

    def test_attack_speed_stim_hunter_is_its_own_archetype(self):
        assert BuildArchetype.ATTACK_SPEED_STIM_HUNTER is not BuildArchetype.HEALER_GUARDIAN
        assert BuildArchetype.ATTACK_SPEED_STIM_HUNTER.name == "ATTACK_SPEED_STIM_HUNTER"

    @pytest.mark.parametrize("role", list(GodRole))
    def test_every_role_resolves_to_an_archetype_with_tables(self, role):
        """SUPPORT_ASSASSIN, MID_ASSASSIN and MID_GUARDIAN are declared with no
        stat targets and no weights; a god mapped to one used to raise a
        KeyError from inside the search."""
        god = God()
        god.name = "Audit"
        god.id = 1
        god.role = role
        god.type = GodType.PHYSICAL
        # Every Smite 1 god carries these; constructing the optimizer reads
        # both, via the archetype tables and the level-20 stat snapshot.
        god.pros = []
        god.stats = GodStats()
        god.stats.values = {}
        god.stats.basic_attack = _BasicAttack()
        optimizer = BuildOptimizer(god, [], {})
        archetype = optimizer._BuildOptimizer__current_archetype
        assert archetype in optimizer._BuildOptimizer__archetype_stat_targets
        assert archetype in optimizer._BuildOptimizer__archetype_weight_mappings
