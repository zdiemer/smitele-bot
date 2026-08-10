"""What `/build` actually picks, and why.

`build_ranker` decides every build the command has ever shown, and until now
nothing here touched it — `grep` found it in the test suite only by way of the
Smite-le game round. These pin the parts where being wrong is silent: a ranking
that quietly prefers a lucky five-game build looks exactly like a ranking that
works, and the description it prints alongside is arithmetic nobody re-derives.

The selection-bias case at the bottom is the one worth reading. A fixed 95%
lower bound is a *per-comparison* bound, and `best_build` takes the argmax over
every distinct build a god has — thousands of them on a real corpus. Tested one
pair at a time the estimator behaves; asked to pick a winner out of a crowd it
returns whichever build got lucky.
"""

from __future__ import annotations

import numpy as np
import pytest

pd = pytest.importorskip("pandas")
build_ranker = pytest.importorskip("build_ranker")

GROUP_KEYS = build_ranker.GROUP_KEYS
ITEM_COLUMNS = build_ranker.ITEM_COLUMNS

SIX = (100, 101, 102, 103, 104, 105)
OTHER_SIX = (106, 107, 108, 109, 110, 111)
WITH_STARTER = (200, 101, 102, 103, 104, 105)

STARTER_IDS = (200, 201)


def build_row(
    build_hash: int,
    plays: int,
    wins: int,
    god_id: int = 1,
    queue_id: int = 451,
    role: str = "Mid",
    high_mmr: bool = False,
    weight: float = 1.0,
    **sums,
) -> dict:
    row = {
        "GodId": god_id,
        "match_queue_id": queue_id,
        "Role": role,
        "HighMmr": high_mmr,
        "BuildHash": build_hash,
        "plays": plays,
        "wins": wins,
        "wplays": plays * weight,
        "wwins": wins * weight,
    }
    for column in build_ranker.GROUP_KEYS:
        row.setdefault(column, None)
    defaults = {
        "sum_Kills_Player": 0.0,
        "sum_Deaths": 0.0,
        "sum_Assists": 0.0,
        "sum_Damage_Player": 0.0,
        "sum_rating": 0.0,
        "sum_tier": 0.0,
        "rated_wins": 0.0,
    }
    defaults.update(sums)
    row.update(defaults)
    return row


def items_row(build_hash: int, items) -> dict:
    row = {"BuildHash": build_hash}
    row.update({column: item for column, item in zip(ITEM_COLUMNS, items)})
    return row


def stats_from(builds, items=None, relics=None, gods=None) -> "build_ranker.BuildStats":
    items = items or [items_row(row["BuildHash"], SIX) for row in builds]
    return build_ranker.BuildStats(
        pd.DataFrame(builds),
        pd.DataFrame(items).drop_duplicates(subset=["BuildHash"]),
        pd.DataFrame(relics or []),
        pd.DataFrame(gods or []),
    )


class TestTheEstimator:
    def test_the_lower_bound_matches_the_formula(self):
        """Recomputed by hand, so a refactor of the vectorised form is caught."""
        plays, wins = 100.0, 60.0
        kappa = build_ranker.KAPPA
        nest = plays + kappa**2
        pest = (wins + kappa**2 / 2) / nest
        expected = pest - kappa * np.sqrt(pest * (1 - pest) / nest)

        actual = build_ranker.agresti_coull_lower(
            np.array([plays]), np.array([wins])
        )[0]
        assert actual == pytest.approx(expected)

    def test_it_never_goes_below_zero(self):
        """A build that lost every game has a bound of zero, not a negative."""
        bound = build_ranker.agresti_coull_lower(np.array([40.0]), np.array([0.0]))
        assert bound[0] == 0.0

    def test_more_evidence_narrows_the_interval(self):
        """The same win rate scores higher the more games back it."""
        few = build_ranker.agresti_coull_lower(np.array([10.0]), np.array([6.0]))[0]
        many = build_ranker.agresti_coull_lower(np.array([1000.0]), np.array([600.0]))[0]
        assert many > few

    def test_a_tiny_sample_loses_to_a_large_one(self):
        """The docstring's claim: 58% over 2,000 beats 80% over 5."""
        small = build_ranker.agresti_coull_lower(np.array([5.0]), np.array([4.0]))[0]
        large = build_ranker.agresti_coull_lower(np.array([2000.0]), np.array([1160.0]))[0]
        assert large > small


class TestSelectionBias:
    """Why ranking thousands of candidates is not ranking two.

    Worth being precise about what is and is not wrong here, because the
    obvious story is wrong. Twenty-five wins in thirty games is not a fluke —
    against a 58% baseline it is significant on its own — and no estimator
    should be asked to rank it below a 58% build on a pairwise reading. The
    defect only appears in a pool: with thousands of candidates, *some* build
    reaches 25/30 by luck alone, and the argmax finds precisely that one.
    """

    def test_a_lucky_build_still_wins_a_pairwise_comparison(self):
        """And should. Recorded so the fix is not mistaken for this.

        Both the old bound and the shrunk posterior prefer the smaller sample
        here, which is the correct answer to the question as posed.
        """
        stats = stats_from(
            [
                build_row(1, plays=2000, wins=1160),
                build_row(2, plays=30, wins=25),
            ],
            items=[items_row(1, SIX), items_row(2, OTHER_SIX)],
        )
        assert stats.best_build(god_id=1, ranking="lower_bound")["build_hash"] == 2
        assert stats.best_build(god_id=1, ranking="shrunk")["build_hash"] == 2

    def test_the_bound_is_not_corrected_for_how_many_builds_it_chose_among(self):
        """One candidate or a thousand, the interval is the same width.

        A 95% interval is wrong one time in twenty by construction. Taking the
        argmax over a thousand of them means the winner is, in expectation, one
        of the fifty that were wrong. This is the defect; the pairwise case
        above is not.
        """
        one = build_ranker.agresti_coull_lower(np.array([50.0]), np.array([30.0]))[0]
        crowd = build_ranker.agresti_coull_lower(
            np.full(1000, 50.0), np.full(1000, 30.0)
        )
        assert crowd[0] == pytest.approx(one)

    def test_the_corrected_bound_does_widen_with_the_crowd(self):
        one = build_ranker.corrected_lower(np.array([50.0]), np.array([30.0]))[0]
        crowd = build_ranker.corrected_lower(
            np.full(1000, 50.0), np.full(1000, 30.0)
        )[0]
        assert crowd < one

    def test_shrinking_does_not_on_its_own_resolve_the_crowd(self):
        """Recorded because it is tempting to believe otherwise.

        One build with fifteen hundred games at 58%, buried among two hundred
        thirty-game builds that average out to a coin flip. The shrunk ranking
        still takes one of the lucky thirty-game builds, because at this prior
        strength 25-and-5 survives being pulled toward 50%.

        So the default did not change because the arithmetic here is clean. It
        changed because held-out days said it wins about seven times in ten
        against what shipped. That is a weaker claim than "the bias is fixed",
        and it is the one the evidence actually supports — the remaining gap is
        what `build_eval`'s oracle row is measuring.
        """
        crowd = [build_row(1, plays=1500, wins=870)]
        crowd += [build_row(n, plays=30, wins=25) for n in range(2, 102)]
        crowd += [build_row(n, plays=30, wins=5) for n in range(102, 202)]
        items = [items_row(1, SIX)] + [
            items_row(n, OTHER_SIX) for n in range(2, 202)
        ]
        stats = stats_from(crowd, items=items)

        assert stats.best_build(god_id=1, ranking="lower_bound")["build_hash"] != 1
        assert stats.best_build(god_id=1, ranking="shrunk")["build_hash"] != 1

    def test_a_much_stronger_prior_does_resolve_it(self):
        """Which is where the remaining headroom probably is.

        Untested against held-out days at production scale, so it is not the
        default — a fortnight of corpus has too few well-supported builds for
        the sweep to tell the settings apart.
        """
        crowd = [build_row(1, plays=1500, wins=870)]
        crowd += [build_row(n, plays=30, wins=25) for n in range(2, 102)]
        crowd += [build_row(n, plays=30, wins=5) for n in range(102, 202)]
        items = [items_row(1, SIX)] + [
            items_row(n, OTHER_SIX) for n in range(2, 202)
        ]
        stats = stats_from(crowd, items=items)

        strong = lambda plays, wins: build_ranker.shrunk_rate(  # noqa: E731
            plays, wins, strength=400.0
        )
        assert stats.best_build(god_id=1, ranking=strong)["build_hash"] == 1


class TestPosteriorUncertainty:
    """`pessimism`, and the pool-only failure it exists to stop.

    A build with almost no evidence has a posterior mean equal to the prior,
    and the prior is set by whichever build dominates the pool — so without a
    penalty for uncertainty the thin build lands a hair above the very build
    that defined the number it was shrunk to, and wins.
    """

    def test_without_it_a_thin_build_edges_out_the_one_that_set_the_prior(self):
        plays = np.array([5.0, 100.0])
        wins = np.array([3.5, 65.0])
        means = build_ranker.shrunk_rate(plays, wins, pessimism=0.0)
        assert means[0] > means[1]

    def test_with_it_the_evidenced_build_wins(self):
        plays = np.array([5.0, 100.0])
        wins = np.array([3.5, 65.0])
        scored = build_ranker.shrunk_rate(plays, wins, pessimism=0.5)
        assert scored[1] > scored[0]

    def test_it_never_reorders_two_builds_with_equal_evidence(self):
        """The penalty depends only on the sample size, so a like-for-like
        comparison is decided by the win rate exactly as before."""
        plays = np.array([200.0, 200.0])
        wins = np.array([90.0, 130.0])
        scored = build_ranker.shrunk_rate(plays, wins)
        assert scored[1] > scored[0]


class TestFiltering:
    def test_an_unspecified_queue_sums_across_queues(self):
        """"Any queue" is a coarser grouping, not a separate table."""
        stats = stats_from(
            [
                build_row(1, plays=100, wins=60, queue_id=451),
                build_row(1, plays=100, wins=60, queue_id=426),
            ]
        )
        best = stats.best_build(god_id=1)
        assert best["plays"] == 200
        assert best["wins"] == 120

    def test_naming_a_queue_restricts_to_it(self):
        stats = stats_from(
            [
                build_row(1, plays=100, wins=60, queue_id=451),
                build_row(1, plays=100, wins=60, queue_id=426),
            ]
        )
        assert stats.best_build(god_id=1, queue_id=451)["plays"] == 100

    def test_role_matching_ignores_case(self):
        """The command sends "Mid"; the aggregate stores whatever it stored."""
        stats = stats_from([build_row(1, plays=100, wins=60, role="Mid")])
        assert stats.best_build(god_id=1, role="mid") is not None
        assert stats.best_build(god_id=1, role="MID") is not None

    def test_high_mmr_keeps_only_the_flagged_rows(self):
        stats = stats_from(
            [
                build_row(1, plays=500, wins=300, high_mmr=False),
                build_row(2, plays=40, wins=30, high_mmr=True),
            ],
            items=[items_row(1, SIX), items_row(2, OTHER_SIX)],
        )
        assert stats.best_build(god_id=1, high_mmr=True)["build_hash"] == 2

    def test_a_god_with_no_rows_gets_nothing(self):
        """Absence returns None rather than raising; /build turns it into prose."""
        stats = stats_from([build_row(1, plays=100, wins=60, god_id=1)])
        assert stats.best_build(god_id=999) is None


class TestTheStarterRequirement:
    def test_builds_without_a_starter_are_dropped_when_others_qualify(self):
        stats = stats_from(
            [
                build_row(1, plays=1000, wins=700),
                build_row(2, plays=1000, wins=600),
            ],
            items=[items_row(1, SIX), items_row(2, WITH_STARTER)],
        )
        best = stats.best_build(
            god_id=1, require_starter=True, starter_ids=STARTER_IDS
        )
        assert best["build_hash"] == 2

    def test_a_god_whose_builds_never_carry_one_still_gets_an_answer(self):
        """The filter is applied only if something survives it.

        Dropping every candidate would turn "this god's players don't buy a
        starter" into "no build exists", which is a worse answer than the
        slightly wrong one.
        """
        stats = stats_from(
            [build_row(1, plays=1000, wins=700), build_row(2, plays=900, wins=600)],
            items=[items_row(1, SIX), items_row(2, OTHER_SIX)],
        )
        best = stats.best_build(
            god_id=1, require_starter=True, starter_ids=STARTER_IDS
        )
        assert best is not None
        assert best["build_hash"] == 1

    def test_no_starter_ids_disables_the_filter(self):
        stats = stats_from([build_row(1, plays=100, wins=60)])
        assert stats.best_build(god_id=1, require_starter=True, starter_ids=()) is not None


class TestTheDescription:
    def test_performance_averages_divide_by_wins_not_plays(self):
        """The sums are over winning rows, so the divisor has to be the wins.

        Dividing by plays would report a losing player's stat line as the
        winners' — quietly, and always low.
        """
        stats = stats_from(
            [
                build_row(
                    1,
                    plays=100,
                    wins=50,
                    sum_Kills_Player=500.0,
                    sum_Deaths=250.0,
                    sum_Assists=400.0,
                    sum_Damage_Player=1_000_000.0,
                )
            ]
        )
        best = stats.best_build(god_id=1)
        assert best["avg_kills"] == pytest.approx(10.0)
        assert best["avg_deaths"] == pytest.approx(5.0)
        assert best["avg_assists"] == pytest.approx(8.0)
        assert best["avg_damage"] == pytest.approx(20_000.0)

    def test_rating_averages_divide_by_the_rated_wins_only(self):
        """Unranked wins contribute a zero rating and must not dilute the mean."""
        stats = stats_from(
            [
                build_row(
                    1, plays=100, wins=50, sum_rating=6000.0, sum_tier=60.0,
                    rated_wins=30.0,
                )
            ]
        )
        best = stats.best_build(god_id=1)
        assert best["avg_rating"] == pytest.approx(200.0)
        assert best["avg_tier"] == pytest.approx(2.0)

    def test_no_rated_wins_reports_zero_rather_than_dividing_by_zero(self):
        stats = stats_from([build_row(1, plays=10, wins=5, rated_wins=0.0)])
        best = stats.best_build(god_id=1)
        assert best["avg_rating"] == 0.0
        assert best["avg_tier"] == 0.0

    def test_win_rate_is_raw_and_unweighted(self):
        """What gets shown is the real count, not the recency-decayed one."""
        stats = stats_from([build_row(1, plays=200, wins=120, weight=0.25)])
        best = stats.best_build(god_id=1)
        assert best["plays"] == 200
        assert best["wins"] == 120
        assert best["win_rate"] == pytest.approx(0.6)

    def test_unique_builds_counts_the_candidates_considered(self):
        stats = stats_from(
            [build_row(n, plays=50, wins=30) for n in range(1, 6)],
            items=[items_row(n, SIX) for n in range(1, 6)],
        )
        assert stats.best_build(god_id=1)["unique_builds"] == 5


class TestRecencyWeighting:
    def test_ranking_reads_the_weighted_counts(self):
        """Two builds with identical raw counts; the older one has to lose."""
        stats = stats_from(
            [
                build_row(1, plays=100, wins=70, weight=0.05),
                build_row(2, plays=100, wins=65, weight=1.0),
            ],
            items=[items_row(1, SIX), items_row(2, OTHER_SIX)],
        )
        assert stats.best_build(god_id=1)["build_hash"] == 2


class TestItemsAndRoles:
    def test_items_come_back_in_slot_order(self):
        stats = stats_from([build_row(1, plays=10, wins=5)], items=[items_row(1, SIX)])
        assert stats.items_for(1) == list(SIX)

    def test_an_unknown_hash_yields_no_items(self):
        """A retired hash returns empty rather than raising; /build then fails
        cleanly rather than rendering a build it cannot name."""
        stats = stats_from([build_row(1, plays=10, wins=5)])
        assert stats.items_for(999) == []

    def test_common_role_is_the_most_played_one(self):
        gods = [
            {"GodId": 1, "match_queue_id": 451, "Role": "Mid", "HighMmr": False,
             "plays": 100, "wins": 50, "wplays": 100.0, "wwins": 50.0},
            {"GodId": 1, "match_queue_id": 451, "Role": "Solo", "HighMmr": False,
             "plays": 300, "wins": 150, "wplays": 300.0, "wwins": 150.0},
        ]
        stats = stats_from([build_row(1, plays=10, wins=5)], gods=gods)
        assert stats.common_role(1) == "Solo"

    def test_common_role_ignores_unknown(self):
        """Unknown is the bucket for unparseable roles, never an answer."""
        gods = [
            {"GodId": 1, "match_queue_id": 451, "Role": "Unknown", "HighMmr": False,
             "plays": 900, "wins": 450, "wplays": 900.0, "wwins": 450.0},
            {"GodId": 1, "match_queue_id": 451, "Role": "Jungle", "HighMmr": False,
             "plays": 100, "wins": 50, "wplays": 100.0, "wwins": 50.0},
        ]
        stats = stats_from([build_row(1, plays=10, wins=5)], gods=gods)
        assert stats.common_role(1) == "Jungle"
