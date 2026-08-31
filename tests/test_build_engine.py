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
# Defensive, but still a variation on DAMAGE rather than a different build. A
# branch has to be one of these to be drawn — see MAX_DIVERGENCE.
KIN_TANK = [POWER, HEALTH, PROTS]


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
            candidates(DAMAGE, MIXED, KIN_TANK), resolver(DAMAGE, MIXED, KIN_TANK)
        )
        assert picked is not None
        assert picked["neutral"] == DAMAGE
        assert picked["ahead"] == DAMAGE
        assert picked["behind"] == KIN_TANK

    def test_the_best_of_the_aggressive_builds_wins_a_tie(self):
        """Two equally offensive builds: the branch is the higher-ranked one,
        not whichever the scan reached last."""
        first = [POWER, STRENGTH, PROTS]
        second = [POWER, STRENGTH, MAGIC_PROTS]
        picked = build_engine.branches(
            candidates(KIN_TANK, first, second), resolver(KIN_TANK, first, second)
        )
        assert picked["ahead"] == first

    def test_a_branch_must_be_a_variation_on_the_recommendation(self):
        """The other half of the diagram fix.

        Drawing the most offensive build in the top twelve as "what to buy when
        you are ahead" only makes sense if it is the same plan adjusted. On the
        real aggregate it was routinely not: Bacchus is a support, and his
        "ahead" row came back a full damage warrior build, because that is what
        happened to rank inside the top twelve.
        """
        stranger = [item(50, prop("HEALTH", 400)), item(51, prop("PHYSICAL_PROTECTION", 70)), item(52, prop("MAGICAL_PROTECTION", 70))]
        picked = build_engine.branches(
            candidates(DAMAGE, stranger), resolver(DAMAGE, stranger)
        )
        assert picked is None

    def test_kinship_is_measured_against_the_recommendation_not_the_pool(self):
        """Two builds may each be kin of neutral without being kin of each
        other, and that is a fork worth drawing."""
        assert build_engine.is_kin(DAMAGE, MIXED)
        assert build_engine.is_kin(DAMAGE, KIN_TANK)
        assert not build_engine.is_kin(DAMAGE, TANK)

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


class TestTheRestOfTheLobby:
    """The two things `team_context` counted that nothing ever read."""

    def lobby(self, **kwargs):
        base = dict(
            physical_share=0.5,
            wants_anti_heal=False,
            crowd_control_share=0.0,
            allied_tanks=0,
            known=True,
        )
        base.update(kwargs)
        return types.SimpleNamespace(**base)

    def test_tenacity_is_worth_something_against_a_lockdown_team(self):
        tenacious = [item(60, prop("TENACITY", 20))]
        plain = [item(61, prop("HEALTH", 400))]
        against_cc = self.lobby(crowd_control_share=0.8)
        assert build_engine.matchup_fit(tenacious, against_cc) > (
            build_engine.matchup_fit(plain, against_cc)
        )

    def test_tenacity_is_worth_nothing_against_a_team_without_crowd_control(self):
        """Half the roster carries a lockdown tag, so one stun must not read as
        a team that chains them."""
        tenacious = [item(62, prop("TENACITY", 20))]
        assert build_engine.matchup_fit(
            tenacious, self.lobby(crowd_control_share=0.2)
        ) == 0.0

    def test_an_allied_front_line_tilts_toward_damage(self):
        """`allies:` was a documented option that moved nothing: zero builds of
        eighty-eight changed for a four-tank ally team.

        Asserted as the change to each build's own score rather than as one
        build beating another, so the protection term — which TANK scores well
        on and DAMAGE cannot score on at all — is held constant.
        """
        behind = self.lobby(allied_tanks=2)
        alone = self.lobby(allied_tanks=0)
        assert build_engine.matchup_fit(DAMAGE, behind) > (
            build_engine.matchup_fit(DAMAGE, alone)
        )
        assert build_engine.matchup_fit(TANK, behind) < (
            build_engine.matchup_fit(TANK, alone)
        )

    def test_the_ally_tilt_stops_at_two(self):
        """A team of five tanks does not make your build a carry's."""
        two = build_engine.matchup_fit(DAMAGE, self.lobby(allied_tanks=2))
        five = build_engine.matchup_fit(DAMAGE, self.lobby(allied_tanks=5))
        assert two == five

    def test_no_allies_means_no_tilt(self):
        assert build_engine.matchup_fit(DAMAGE, self.lobby()) == 0.0


class TestPromotingAntiHeal:
    """Anti-heal is a requirement against a healer, not a tie-break.

    It reached the top of the ranking for six gods of eighty-eight when it was
    weighted like one, because the best anti-heal build sits at a median
    position of eighteen and a tie-break is worth two or three places.
    """

    def pool(self, *entries):
        return [
            {"items": [i.id for i in build], "rank": rank} for build, rank in entries
        ]

    def carries(self, items):
        return any(i.id == PEN.id for i in items)

    def test_a_close_enough_anti_heal_build_is_promoted(self):
        pool = self.pool((TANK, 0.53), (MIXED, 0.52), (DAMAGE, 0.51))
        promoted = build_engine.promote_anti_heal(
            pool, resolver(TANK, MIXED, DAMAGE), self.carries, tolerance=0.05
        )
        assert promoted[0]["items"] == [i.id for i in DAMAGE]

    def test_a_build_that_costs_too_much_is_left_where_it_is(self):
        pool = self.pool((TANK, 0.60), (MIXED, 0.59), (DAMAGE, 0.30))
        promoted = build_engine.promote_anti_heal(
            pool, resolver(TANK, MIXED, DAMAGE), self.carries, tolerance=0.05
        )
        assert promoted[0]["items"] == [i.id for i in TANK]

    def test_a_leader_that_already_carries_it_is_not_disturbed(self):
        pool = self.pool((DAMAGE, 0.53), (TANK, 0.52))
        promoted = build_engine.promote_anti_heal(
            pool, resolver(DAMAGE, TANK), self.carries, tolerance=0.05
        )
        assert promoted == pool

    def test_the_best_anti_heal_build_wins_not_the_first_one_seen(self):
        """`for_lobby` has already reordered by fit, so the pool is no longer
        sorted by rank and scanning until the score drops would stop early."""
        other = [PEN, HEALTH, PROTS]
        pool = self.pool((TANK, 0.55), (other, 0.51), (DAMAGE, 0.54))
        promoted = build_engine.promote_anti_heal(
            pool, resolver(TANK, other, DAMAGE), self.carries, tolerance=0.05
        )
        assert promoted[0]["items"] == [i.id for i in DAMAGE]

    def test_nothing_in_the_pool_carries_it(self):
        pool = self.pool((TANK, 0.53), (HEALTH_ONLY := [HEALTH], 0.52))
        assert (
            build_engine.promote_anti_heal(
                pool, resolver(TANK, HEALTH_ONLY), self.carries
            )
            == pool
        )


class TestReorderingForALobby:
    def lobby(self, physical_share=0.5, known=True):
        return types.SimpleNamespace(
            physical_share=physical_share,
            wants_anti_heal=False,
            crowd_control_share=0.0,
            allied_tanks=0,
            known=known,
        )

    def ranked(self, *entries):
        return [
            {"items": [i.id for i in build], "rank": rank} for build, rank in entries
        ]

    def test_no_lobby_leaves_the_ranking_alone(self):
        pool = candidates(DAMAGE, TANK)
        assert build_engine.for_lobby(pool, resolver(DAMAGE, TANK), None) == pool

    def test_a_near_tie_is_broken_by_the_lobby(self):
        """The build aimed at the right damage type climbs past the one above
        it, which is the whole point."""
        physical = [item(30, prop("PHYSICAL_PROTECTION", 70))]
        magical = [item(31, prop("MAGICAL_PROTECTION", 70))]
        pool = self.ranked((magical, 0.521), (physical, 0.520))
        reordered = build_engine.for_lobby(
            pool, resolver(magical, physical), self.lobby(physical_share=1.0)
        )
        assert reordered[0]["items"] == [physical[0].id]

    def test_it_cannot_overrule_a_real_difference_in_score(self):
        """The budget is in win rate, not in places.

        Positions were the old currency and they could not work: on a pool of
        four thousand candidates two places is nothing, and the scheme moved
        zero builds of eighty-eight between an all-physical and an all-magical
        enemy team. But it still has to lose to a build that is genuinely, and
        not marginally, better.
        """
        magical = [item(32, prop("MAGICAL_PROTECTION", 70))]
        physical = [item(33, prop("PHYSICAL_PROTECTION", 70))]
        pool = self.ranked((magical, 0.60), (physical, 0.50))
        reordered = build_engine.for_lobby(
            pool, resolver(magical, physical), self.lobby(physical_share=1.0)
        )
        assert reordered[0]["items"] == [magical[0].id]

    def test_it_can_reach_past_a_build_the_ranking_barely_prefers(self):
        """The case the position scheme could not reach: many near-identical
        candidates above the one that fits, separated by noise."""
        magical = [item(34, prop("MAGICAL_PROTECTION", 70))]
        physical = [item(35, prop("PHYSICAL_PROTECTION", 70))]
        pool = self.ranked(
            *[(magical, 0.5200 - n * 0.0001) for n in range(20)], (physical, 0.5150)
        )
        reordered = build_engine.for_lobby(
            pool, resolver(magical, physical), self.lobby(physical_share=1.0)
        )
        assert reordered[0]["items"] == [physical[0].id]

    def test_ties_keep_their_original_order(self):
        """Two builds the lobby cannot separate must come back in ranking
        order, or the same request would answer differently twice.

        Same protections in both, so there is genuinely nothing to separate —
        which is the only way to test the tie-break rather than the term.
        """
        first = [POWER, PROTS, MAGIC_PROTS]
        second = [STRENGTH, PROTS, MAGIC_PROTS]
        pool = self.ranked((first, 0.53), (second, 0.53))
        reordered = build_engine.for_lobby(
            pool, resolver(first, second), self.lobby(physical_share=0.5)
        )
        assert [c["items"] for c in reordered] == [c["items"] for c in pool]
