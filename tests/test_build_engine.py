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

    def test_two_builds_that_differ_are_a_fork(self):
        """Even though neither straddles the neutral one — they are the same
        build here — because the branches are measured against each other."""
        picked = build_engine.branches(
            candidates(MIXED, DAMAGE), resolver(MIXED, DAMAGE)
        )
        assert picked is not None
        assert picked["ahead"] == DAMAGE
        assert picked["behind"] == MIXED

    def test_the_neutral_build_may_itself_be_the_aggressive_branch(self):
        """The case that kept the tree off almost every god.

        The highest-ranked build for a carry or a mid is routinely full damage,
        sitting at the top of the scale, and nothing can be more offensive than
        that. Requiring each branch to be more extreme than neutral made a fork
        impossible for exactly the gods people ask about most.
        """
        picked = build_engine.branches(
            candidates(DAMAGE, MIXED, TANK), resolver(DAMAGE, MIXED, TANK)
        )
        assert picked is not None
        assert picked["neutral"] == DAMAGE
        assert picked["ahead"] == DAMAGE
        assert picked["behind"] == TANK

    def test_the_best_of_the_aggressive_builds_wins_a_tie(self):
        """Two equally offensive builds: the branch is the higher-ranked one,
        not whichever the scan reached last."""
        first = [item(40, prop("PHYSICAL_POWER", 60))]
        second = [item(41, prop("PHYSICAL_POWER", 60))]
        picked = build_engine.branches(
            candidates(TANK, first, second), resolver(TANK, first, second)
        )
        assert picked["ahead"] == first

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


class TestMatchupFit:
    """Reordering a ranking by the lobby, without letting it overrule one."""

    def lobby(self, physical_share=0.5, wants_anti_heal=False, known=True):
        return types.SimpleNamespace(
            physical_share=physical_share,
            wants_anti_heal=wants_anti_heal,
            known=known,
        )

    def test_no_lobby_is_indifferent(self):
        assert build_engine.matchup_fit(MIXED, None) == 0.0

    def test_an_unknown_lobby_is_indifferent(self):
        """team_context reports 0.5 physical when it knows nothing, which must
        read as "no opinion" rather than "half and half is correct"."""
        assert build_engine.matchup_fit(MIXED, self.lobby(known=False)) == 0.0

    def test_physical_protections_score_higher_against_physical_damage(self):
        physical = [item(20, prop("PHYSICAL_PROTECTION", 70))]
        magical = [item(21, prop("MAGICAL_PROTECTION", 70))]
        against_physical = self.lobby(physical_share=1.0)
        assert build_engine.matchup_fit(physical, against_physical) > (
            build_engine.matchup_fit(magical, against_physical)
        )

    def test_the_preference_reverses_against_magical_damage(self):
        physical = [item(22, prop("PHYSICAL_PROTECTION", 70))]
        magical = [item(23, prop("MAGICAL_PROTECTION", 70))]
        against_magical = self.lobby(physical_share=0.0)
        assert build_engine.matchup_fit(magical, against_magical) > (
            build_engine.matchup_fit(physical, against_magical)
        )

    def test_a_build_with_no_protections_is_not_penalised(self):
        """A damage build has no split to align; it should score neutral rather
        than losing to every bruiser build in the pool."""
        assert build_engine.matchup_fit(DAMAGE, self.lobby(physical_share=1.0)) == 0.0

    def test_anti_heal_counts_only_when_a_healer_is_present(self):
        carries = lambda items: True  # noqa: E731
        with_healer = self.lobby(wants_anti_heal=True)
        without = self.lobby(wants_anti_heal=False)
        assert build_engine.matchup_fit(DAMAGE, with_healer, carries) > (
            build_engine.matchup_fit(DAMAGE, without, carries)
        )

    def test_an_unreadable_passive_does_not_raise(self):
        def explode(_items):
            raise ValueError("no")

        build_engine.matchup_fit(DAMAGE, self.lobby(wants_anti_heal=True), explode)


class TestReorderingForALobby:
    def lobby(self, physical_share=0.5, known=True):
        return types.SimpleNamespace(
            physical_share=physical_share, wants_anti_heal=False, known=known
        )

    def test_no_lobby_leaves_the_ranking_alone(self):
        pool = candidates(DAMAGE, TANK)
        assert build_engine.for_lobby(pool, resolver(DAMAGE, TANK), None) == pool

    def test_a_near_tie_is_broken_by_the_lobby(self):
        """The build aimed at the right damage type climbs past the one above
        it, which is the whole point."""
        physical = [item(30, prop("PHYSICAL_PROTECTION", 70))]
        magical = [item(31, prop("MAGICAL_PROTECTION", 70))]
        pool = candidates(magical, physical)
        reordered = build_engine.for_lobby(
            pool, resolver(magical, physical), self.lobby(physical_share=1.0)
        )
        assert reordered[0]["items"] == [physical[0].id]

    def test_it_cannot_promote_a_build_from_far_down_the_ranking(self):
        """A perfect fit is worth two places, not twenty. The ranking is
        measured against held-out win rates and this is not."""
        magical = [item(32, prop("MAGICAL_PROTECTION", 70))]
        physical = [item(33, prop("PHYSICAL_PROTECTION", 70))]
        pool = candidates(*([magical] * 6), physical)
        reordered = build_engine.for_lobby(
            pool, resolver(magical, physical), self.lobby(physical_share=1.0)
        )
        assert reordered[0]["items"] == [magical[0].id]

    def test_ties_keep_their_original_order(self):
        """Two builds the lobby cannot separate must come back in ranking
        order, or the same request would answer differently twice."""
        pool = candidates(DAMAGE, MIXED, TANK)
        reordered = build_engine.for_lobby(
            pool, resolver(DAMAGE, MIXED, TANK), self.lobby(physical_share=0.5)
        )
        assert [c["items"] for c in reordered] == [c["items"] for c in pool]
