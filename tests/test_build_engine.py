"""Choosing the three builds the conditional tree forks between.

The tree used to be `/optimize`-only because it was made by re-scoring the stat
model at two other balances, and the corpus path has no balance to re-score at.
It finds its branches further down the same ranking instead, which means the
decision is now "which of these real builds is the aggressive one" — and getting
that backwards draws a confident diagram telling a player to buy protections
when they are ahead.

These pin the two ways it can be silently wrong: the sides being swapped, and a
fork being drawn where there is no real disagreement.
"""

from __future__ import annotations

import types

import pytest

build_engine = pytest.importorskip("build_engine")


def prop(name: str, flat: float = 0.0, percent: float = 0.0):
    return types.SimpleNamespace(
        attribute=types.SimpleNamespace(name=name),
        flat_value=flat,
        percent_value=percent,
    )


def item(item_id: int, *properties):
    return types.SimpleNamespace(id=item_id, name=f"item{item_id}", item_properties=list(properties))


POWER = item(1, prop("PHYSICAL_POWER", 60))
STRENGTH = item(2, prop("STRENGTH", 45))
PEN = item(3, prop("PENETRATION", 0, 0.20))
HEALTH = item(4, prop("HEALTH", 400))
PROTS = item(5, prop("PHYSICAL_PROTECTION", 70))
MAGIC_PROTS = item(6, prop("MAGICAL_PROTECTION", 70))
NOTHING = item(7)

DAMAGE = [POWER, STRENGTH, PEN]
TANK = [HEALTH, PROTS, MAGIC_PROTS]
MIXED = [POWER, STRENGTH, PROTS]


class TestOffensiveShare:
    def test_a_pure_damage_build_is_all_offence(self):
        assert build_engine.offensive_share(DAMAGE) == 1.0

    def test_a_pure_tank_build_is_no_offence(self):
        assert build_engine.offensive_share(TANK) == 0.0

    def test_a_mixed_build_lands_between(self):
        share = build_engine.offensive_share(MIXED)
        assert 0.0 < share < 1.0

    def test_a_build_with_no_stats_has_no_side(self):
        """None rather than zero: "no offence" and "unmeasurable" are
        different, and only one of them should order a branch."""
        assert build_engine.offensive_share([NOTHING]) is None

    def test_both_games_land_on_the_same_side(self):
        """Smite 1 has PHYSICAL_POWER and Smite 2 has STRENGTH; the enums share
        no members, and matching on names is what lets one function serve
        both."""
        assert build_engine.offensive_share([item(8, prop("PHYSICAL_POWER", 60))]) == 1.0
        assert build_engine.offensive_share([item(9, prop("STRENGTH", 60))]) == 1.0
        assert build_engine.offensive_share([item(10, prop("INTELLIGENCE", 60))]) == 1.0

    def test_percentages_are_scaled_before_being_added(self):
        """A raw 0.20 against 400 health would make 20% penetration invisible."""
        share = build_engine.offensive_share([PEN, HEALTH])
        assert share > 0.0


def candidates(*builds):
    return [{"items": [i.id for i in build]} for build in builds]


def resolver(*builds):
    by_id = {i.id: i for build in builds for i in build}

    def resolve(item_ids):
        found = [by_id[i] for i in item_ids if i in by_id]
        return found if len(found) == len(item_ids) else None

    return resolve


class TestPickingBranches:
    def test_the_first_candidate_is_the_neutral_build(self):
        picked = build_engine.branches(
            candidates(MIXED, DAMAGE, TANK), resolver(MIXED, DAMAGE, TANK)
        )
        assert picked["neutral"] == MIXED

    def test_the_aggressive_branch_is_the_more_offensive_one(self):
        """The assertion that fails if the sides are ever swapped."""
        picked = build_engine.branches(
            candidates(MIXED, DAMAGE, TANK), resolver(MIXED, DAMAGE, TANK)
        )
        assert picked["ahead"] == DAMAGE
        assert picked["behind"] == TANK

    def test_the_order_of_the_two_branches_in_the_ranking_does_not_matter(self):
        picked = build_engine.branches(
            candidates(MIXED, TANK, DAMAGE), resolver(MIXED, TANK, DAMAGE)
        )
        assert picked["ahead"] == DAMAGE
        assert picked["behind"] == TANK

    def test_no_fork_when_every_candidate_agrees(self):
        """A god with one settled build gets shown one build, not a tree whose
        halves are the same."""
        assert (
            build_engine.branches(
                candidates(MIXED, MIXED, MIXED), resolver(MIXED)
            )
            is None
        )

    def test_no_fork_when_only_one_side_exists(self):
        """Filling the missing side with the neutral build would tell the
        reader there is a decision here when there is not."""
        assert (
            build_engine.branches(
                candidates(MIXED, DAMAGE), resolver(MIXED, DAMAGE)
            )
            is None
        )

    def test_a_build_referencing_a_removed_item_is_skipped(self):
        """Item rotations happen between the corpus and the catalogue; the tree
        has to survive one."""
        rotated = candidates(MIXED, DAMAGE, TANK)
        rotated[1]["items"] = [POWER.id, 999]
        picked = build_engine.branches(rotated, resolver(MIXED, DAMAGE, TANK))
        assert picked is None or picked["ahead"] != DAMAGE

    def test_nothing_resolvable_yields_nothing(self):
        assert build_engine.branches(candidates(MIXED), lambda ids: None) is None

    def test_it_does_not_look_past_the_branch_depth(self):
        """The twentieth-best build is not "what to buy when winning", it is
        just worse."""
        filler = [MIXED] * (build_engine.BRANCH_DEPTH + 2)
        pool = candidates(*filler, DAMAGE, TANK)
        assert build_engine.branches(pool, resolver(MIXED, DAMAGE, TANK)) is None


class TestBuildingThePath:
    def test_a_fork_becomes_a_path_with_branches(self):
        path = build_engine.path_for(
            candidates(MIXED, DAMAGE, TANK),
            resolver(MIXED, DAMAGE, TANK),
            score=lambda items: float(len(items)),
            price=lambda item_: 1000,
        )
        assert path is not None
        assert path.forks

    def test_no_branches_means_no_path(self):
        assert (
            build_engine.path_for(
                candidates(MIXED, MIXED),
                resolver(MIXED),
                score=lambda items: float(len(items)),
                price=lambda item_: 1000,
            )
            is None
        )

    def test_a_scorer_that_raises_costs_the_drawing_not_the_build(self):
        def explode(_items):
            raise ValueError("no")

        assert (
            build_engine.path_for(
                candidates(MIXED, DAMAGE, TANK),
                resolver(MIXED, DAMAGE, TANK),
                score=explode,
                price=lambda item_: 1000,
            )
            is None
        )
