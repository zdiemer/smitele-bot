"""Does Pareto dominance on the role vector agree with what won?

`ttk_validate` asks whether one number (kill speed) orders builds the way the
corpus does. This asks the same question of the role-shaped *vector* in
`role_score`, and it cannot use a correlation, because the vector defines a
partial order, not a ranking — some builds are deliberately incomparable.

So the measure is pairwise concordance:

    among build pairs where one Pareto-dominates the other on the role vector,
    what fraction have the dominant build also winning more in the corpus?

and its companion, coverage — the share of all pairs that are comparable at
all. A partial order that is right whenever it speaks but speaks rarely (high
concordance, low coverage) is still useful: you break the incomparable pairs
with a secondary signal. One that speaks often but is frequently wrong is not.

Both are reported against a scalar baseline: raw kill-speed sum, ordered as a
total order, concordant on every pair. The vector earns its complexity only if
it is *more* concordant on the pairs it commits to than the scalar is across
all of them — most of all for the roles (Solo, Support) where kill speed alone
was weakest in ttk_validate.

    python src/tools/role_validate.py --aggregate <dir> --roles Solo,Support

Win rate is the shrunk estimate, as everywhere else, so the comparison is
against the ordering the evidence supports rather than sampling noise.

Measured 2026-08-11, Smite 1 production aggregate, all five roles
----------------------------------------------------------------

    role      pareto  coverage  scalar
    Carry      0.633     53%    0.575
    Solo       0.608     99%    0.566
    Support    0.560     59%    0.509
    Mid        0.535     58%    0.524
    Jungle     0.524     91%    0.538   (kill-speed x ehp; see below)

The vector beats scalar kill-speed on four of five roles, and the two widest
margins — Support +5pp, Carry +6pp — bracket the point: the vector helps most
where kill speed alone was weakest (Support) and where a survival floor sharpens
an already-good signal (Carry). Solo's single ehp*damage axis is a near-total
order (99% coverage) and still beats pure kill speed, which is the product doing
its job. Mid is a marginal win.

Jungle was the honest miss, and chasing it taught the model something. The
first jungle vector scored one-rotation burst, on the assumption an assassin
deletes on contact — but the corpus rejects that for Smite 1: per-cell
concordance against win rate is 0.554 for sustained kill speed and 0.517, a
coin, for a single ability rotation. Junglers weave to a kill, so the sustained
sim is the right model, and burst was the wrong one. The vector is now sustained
kill speed into the backline times survival, a single axis like solo's. That did
not move the concordance number (0.524, versus 0.525 for the old burst vector) —
jungle simply is the hardest role to order, because it is won on snowball and map
pressure more than on the six items a build hash holds — but it nearly doubled
*coverage*, from 57% to 91%, so the order now speaks to almost every pair at the
same accuracy instead of committing on half of them. A more useful ranker at an
honest ceiling, not a higher number pretending the ceiling moved.

Coverage is the other half of the story. Every multi-axis role leaves ~40% of
pairs incomparable (Solo, single-axis, does not); the concordance figures are
the accuracy *on the pairs the order commits to*. The intended use follows
directly: rank by Pareto where it speaks, break the incomparable pairs with a
secondary signal, and never invent a weight to force a total order.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), part)
    for part in ("HirezAPI", "SmiteBot", "ml")
]

import numpy as np  # noqa: E402

import build_accuracy  # noqa: E402
from build_ranker import BuildStats, shrunk_rate  # noqa: E402
from role_score import RoleVector, role_vector  # noqa: E402
from stat_calculator import DamageCalculator, GodBuild  # noqa: E402

# Reuse ttk_validate's catalogue loader, defender selection and role weave.
import ttk_validate  # noqa: E402


def concordance_scalar(
    scores: np.ndarray, rates: np.ndarray
) -> Tuple[int, int]:
    """(agreeing, total) over all pairs, ordered by a more-is-better scalar."""
    agree = total = 0
    n = len(scores)
    for i in range(n):
        for j in range(i + 1, n):
            if scores[i] == scores[j] or rates[i] == rates[j]:
                continue
            total += 1
            if (scores[i] > scores[j]) == (rates[i] > rates[j]):
                agree += 1
    return agree, total


def concordance_pareto(
    vectors: List[RoleVector], rates: np.ndarray
) -> Tuple[int, int, int]:
    """(agreeing, comparable, all-pairs) for the vector's Pareto order.

    A pair is comparable when one vector dominates the other; it agrees when
    the dominant build also has the higher win rate.
    """
    agree = comparable = 0
    n = len(vectors)
    all_pairs = n * (n - 1) // 2
    for i in range(n):
        for j in range(i + 1, n):
            if rates[i] == rates[j]:
                continue
            if vectors[i].dominates(vectors[j]):
                comparable += 1
                agree += int(rates[i] > rates[j])
            elif vectors[j].dominates(vectors[i]):
                comparable += 1
                agree += int(rates[j] > rates[i])
    return agree, comparable, all_pairs


def validate_cell(
    stats: BuildStats,
    gods: Dict,
    items: Dict,
    god_id: int,
    role: str,
    frontline: GodBuild,
    backline: GodBuild,
    defenders,
    min_plays: int,
    limit: int,
    trials: int,
    weave: float,
) -> Optional[Dict]:
    god = gods.get(god_id)
    if god is None:
        return None
    candidates = stats.ranked_builds(
        god_id,
        role=role,
        ranking=lambda wplays, wwins: np.asarray(wplays),
        min_plays=min_plays,
        limit=limit,
    )
    candidates = [c for c in candidates if c["plays"] >= min_plays]
    builds, plays, wins = [], [], []
    kill_speed = []
    for cand in candidates:
        if not all(i in items for i in cand["items"]):
            continue
        attacker = GodBuild(god, [items[i] for i in cand["items"]], 20)
        builds.append(attacker)
        plays.append(cand["plays"])
        wins.append(cand["wins"])
        # Scalar baseline: summed kill speed vs every defender, the ttk_validate
        # quantity but oriented more-is-better.
        speed = 0.0
        for _, defender in defenders:
            ttk = DamageCalculator.calculate_basic_ttk(
                attacker, defender, assume_item_passives_stacked=True, weave=weave
            )
            speed += 1.0 / ttk if ttk > 0 else 0.0
        kill_speed.append(speed)

    if len(builds) < 8:
        return None
    plays = np.array(plays, float)
    wins = np.array(wins, float)
    rates = shrunk_rate(plays, wins, pessimism=0.0)

    vectors = [role_vector(role, b, frontline, backline, weave) for b in builds]
    p_agree, p_comparable, all_pairs = concordance_pareto(vectors, rates)
    s_agree, s_total = concordance_scalar(np.array(kill_speed), rates)

    return {
        "god": god.name,
        "role": role,
        "builds": len(builds),
        "pareto_agree": p_agree,
        "pareto_comparable": p_comparable,
        "all_pairs": all_pairs,
        "scalar_agree": s_agree,
        "scalar_total": s_total,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aggregate", required=True)
    parser.add_argument("--gods-json", default=None)
    parser.add_argument("--items-json", default=None)
    parser.add_argument("--roles", default="Carry,Solo,Jungle,Mid,Support")
    parser.add_argument("--queue", type=int, default=None)
    parser.add_argument("--min-plays", type=int, default=30)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--max-gods", type=int, default=0)
    args = parser.parse_args()

    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    gods, items = ttk_validate.catalogue(
        args.gods_json or os.path.join(here, "gods.json"),
        args.items_json or os.path.join(here, "items.json"),
    )
    stats = BuildStats.load(args.aggregate)
    if stats is None:
        print(f"No aggregate in {args.aggregate}", file=sys.stderr)
        return 1

    defenders = ttk_validate.corpus_defenders(stats, gods, items, args.queue)
    by_role = {role: build for role, build in defenders}
    # The frontline is the tankiest corpus opponent (Solo), the backline the
    # squishiest (Carry); both are real builds the aggregate chose.
    frontline = by_role.get("Solo") or defenders[0][1]
    backline = by_role.get("Carry") or defenders[-1][1]

    print("Frontline:", frontline.god.name, "| Backline:", backline.god.name)

    per_role: Dict[str, List[Dict]] = {}
    for role in [r.strip() for r in args.roles.split(",")]:
        pool = stats.builds[stats.builds["Role"].astype(str) == role]
        if args.queue is not None:
            pool = pool[pool["match_queue_id"] == args.queue]
        god_ids = [
            int(g)
            for g in pool.groupby("GodId", observed=True)["plays"]
            .sum()
            .sort_values(ascending=False)
            .index
        ]
        if args.max_gods:
            god_ids = god_ids[: args.max_gods]
        weave = ttk_validate.ROLE_WEAVE.get(role, 1.0)
        for god_id in god_ids:
            cell = validate_cell(
                stats, gods, items, god_id, role, frontline, backline,
                defenders, args.min_plays, args.limit, args.trials, weave,
            )
            if cell is not None:
                per_role.setdefault(role, []).append(cell)

    print(
        f"\n{'role':8s} {'cells':>5} {'pareto':>18} {'coverage':>9} "
        f"{'scalar':>16}"
    )
    for role, cells in per_role.items():
        pa = sum(c["pareto_agree"] for c in cells)
        pc = sum(c["pareto_comparable"] for c in cells)
        ap = sum(c["all_pairs"] for c in cells)
        sa = sum(c["scalar_agree"] for c in cells)
        st = sum(c["scalar_total"] for c in cells)
        p_conc = pa / pc if pc else float("nan")
        coverage = pc / ap if ap else float("nan")
        s_conc = sa / st if st else float("nan")
        print(
            f"{role:8s} {len(cells):>5} "
            f"{p_conc:>10.3f} ({pc:>5} pr) {coverage:>8.1%} "
            f"{s_conc:>10.3f} ({st:>5} pr)"
        )
    print(
        "\npareto = concordance on comparable pairs; coverage = share of pairs "
        "that are comparable; scalar = kill-speed concordance on all pairs."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
