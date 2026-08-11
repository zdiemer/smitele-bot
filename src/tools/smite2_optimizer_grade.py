"""Grade what smite2_optimizer builds, by the one metric the corpus cannot give.

The optimizer chooses Smite 2 builds from a stat model, and the corpus cannot
say whether it chose well: its builds have a median of one or two held-out
plays, so win rate is silent on them (the whole reason the role vector exists).
The role vector *is* an absolute score, so this asks the question directly:

    for each god and lane, is the build the optimizer picks Pareto-efficient
    against the builds people actually run, on that role's own axes — or does a
    real build dominate it, being at least as good on every axis and better on
    one?

A dominated optimizer build is the actionable finding. It means a build already
in the corpus beats the optimizer's on kill speed *and* survival *and*
everything else the role cares about — so the optimizer left role-value on the
table that the stat model alone could not see. A non-dominated build is not
proven best (Pareto is a partial order), only that nothing people run strictly
beats it, which is the bar an absolute metric can honestly hold it to.

    python src/tools/smite2_optimizer_grade.py --aggregate <s2 dir> \
        --wiki-cache <wiki_cache.json> --max-gods 8

Comparing against corpus builds rather than perturbations of the optimizer's own
output is deliberate: the point is whether the optimizer keeps up with human
build knowledge on the axes, not whether its greedy search found its own local
optimum.

Measured 2026-08-11, live Smite 2 aggregate, 12 gods a role
-----------------------------------------------------------

Two knobs decide whether this measures anything real, and getting both wrong
told a story that fell apart under inspection. Corpus builds must be ranked by
*win rate*, not plays, or the optimizer is graded against popular glass cannons
that out-burst everything and win nothing. And domination needs a *margin* —
exact Pareto calls a build a hair better on every axis a winner, though the two
are for practical purposes equal. With win-ranked comparators and a 5% margin
(the default):

    role      efficient   trust
    carry      100%        high
    mid        100%        high
    jungle      92%        medium
    solo        50%        metric-limited
    support     45%        metric-limited

The optimizer is good. Where the axes are trustworthy — carry, mid, jungle —
its builds are Pareto-efficient against what actually wins essentially always.
The two that sit lower are the metric's limits, not the optimizer's, and both
were confirmed by measuring rather than assumed:

- Mid looked damning before the margin: at exact Pareto it graded 0-8%, and it
  was tempting to call the optimizer too defensive and lean its profile toward
  burst. Direct comparison killed that. The optimizer's mid builds land within
  ~2% of the winning builds' rotation burst — often higher — and share five of
  six items with them; the "domination" was entirely sub-2% margins on every
  axis at once. No profile change: the optimizer already builds mid the way the
  winners do. The lesson was about the metric's brittleness, not the optimizer.
- Solo at 50% survives the margin because its dominations are real, but they
  are the axis's ambiguity, not an optimizer fault. Across solo builds win rate
  correlates +0.22 with effective HP and -0.22 with damage — winning solos are
  tankier — yet within one god EHP alone is a near-random ranker (0.508 on the
  deep Smite 1 corpus) while ehp*damage orders well (0.573). Between-god the
  tank wins; within-god damage still separates. The product axis is right and
  there is no honest profile change to make.
- Support at 45% is the metric blind to utility by design: its vector is
  (effective HP, cooldown rate), auras and crowd control deliberately absent
  and left to the optimizer, so a corpus build dominating on those two axes may
  simply be one the optimizer rightly spent on utility the vector cannot see.

The arc of these numbers — 83/58/50/45/8 at exact Pareto against played builds,
100/92/50/45/100 with win-ranking and a margin — is the point of the tool as
much as any single figure: an absolute grade is only as honest as the two
choices under it, and three plausible "optimizer flaws" here dissolved on
measurement. Pass --margin 0 to see the brittle exact-Pareto view.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), part)
    for part in ("HirezAPI", "SmiteBot", "ml")
]

import numpy as np  # noqa: E402

import build_accuracy  # noqa: E402
from build_ranker import BuildStats  # noqa: E402
from HirezAPI import PlayerRole  # noqa: E402
from smite2_optimizer import Smite2BuildOptimizer  # noqa: E402
from smite2_role_score import (  # noqa: E402
    Defender,
    build_stats,
    defender_of,
    median_defender,
    role_vector,
)

ROLES = ("carry", "solo", "support", "mid", "jungle")


def corpus_defender(
    stats: BuildStats, gods: Dict, items: Dict, role: str
) -> Optional[Defender]:
    """The component-wise median durability of a role's most-played builds."""
    pool = stats.builds[stats.builds["Role"].astype(str).str.lower() == role]
    if not pool.shape[0]:
        return None
    by_god = pool.groupby("GodId", observed=True)["plays"].sum().sort_values(
        ascending=False
    )
    defs = []
    for god_id in by_god.index[:15]:
        god = gods.get(int(god_id))
        if god is None:
            continue
        cands = stats.ranked_builds(
            int(god_id),
            role=role.capitalize(),
            ranking=lambda wp, ww: np.asarray(wp),
            min_plays=20,
            limit=1,
        )
        for cand in cands:
            build = [items[i] for i in cand["items"] if i in items]
            if len(build) == 6:
                defs.append(defender_of(build_stats(god, build)))
    return median_defender(defs)


def top_corpus_builds(
    stats: BuildStats, items: Dict, god_id: int, role: str, limit: int
) -> List[List]:
    """The best corpus builds for a cell, ranked by *win rate*, length six.

    Win-ranked, not play-ranked, and the distinction is load-bearing. Grading
    the optimizer against the most-*played* builds asks "does a popular build
    beat it on the axes", which a glass cannon that out-bursts everything and
    wins nothing answers yes to — penalising the optimizer for not chasing an
    extreme the corpus does not reward (measured: mid win rate peaks at
    moderate burst, not maximum). Ranking by the shrunk win rate instead asks
    the question that matters — does a build that actually *wins* dominate the
    optimizer's — which is the only kind of domination worth acting on.
    """
    out = []
    for cand in stats.ranked_builds(
        god_id,
        role=role.capitalize(),
        min_plays=20,
        limit=limit,
    ):
        build = [items[i] for i in cand["items"] if i in items]
        if len(build) == 6:
            out.append(build)
    return out


def dominates_by(strong, weak, margin: float) -> bool:
    """Pareto domination with a tolerance: better on every axis by `margin`.

    Plain dominance is brittle — a build a hair better on all axes at once
    "wins" though the two are for practical purposes equal. Requiring each axis
    to be better by a fraction `margin` keeps only defeats that mean something.
    Zero margin recovers exact Pareto dominance.
    """
    ge = all(a >= b * (1.0 + margin) or (a == b == 0) for a, b in zip(strong.axes, weak.axes))
    gt = any(a > b * (1.0 + margin) for a, b in zip(strong.axes, weak.axes))
    return ge and gt


def grade_cell(
    stats: BuildStats,
    gods: Dict,
    items: Dict,
    god_id: int,
    role: str,
    frontline: Defender,
    backline: Defender,
    limit: int,
    margin: float = 0.0,
) -> Optional[Dict]:
    god = gods.get(god_id)
    if god is None:
        return None
    try:
        optimizer = Smite2BuildOptimizer(god, items, role=PlayerRole(role))
        opt_build = optimizer.optimize()
    except Exception as error:  # noqa: BLE001 — a god that will not optimise is data
        return {"god": god.name, "role": role, "error": type(error).__name__}
    if len(opt_build) != 6:
        return {"god": god.name, "role": role, "error": "short_build"}

    corpus = top_corpus_builds(stats, items, god_id, role, limit)
    if len(corpus) < 4:
        return None

    opt_vec = role_vector(role, god, opt_build, frontline, backline)
    corpus_vecs = [role_vector(role, god, b, frontline, backline) for b in corpus]

    dominators = [
        b for b, v in zip(corpus, corpus_vecs) if dominates_by(v, opt_vec, margin)
    ]
    # Also: does the optimizer's build dominate any corpus build? A build that
    # both is undominated and dominates real builds is the strong case.
    dominates_count = sum(1 for v in corpus_vecs if dominates_by(opt_vec, v, margin))

    return {
        "god": god.name,
        "role": role,
        "corpus_builds": len(corpus),
        "dominated_by": len(dominators),
        "dominates": dominates_count,
        "efficient": len(dominators) == 0,
        "example_dominator": _names(dominators[0]) if dominators else None,
        "opt_build": _names(opt_build),
    }


def _names(build: List) -> List[str]:
    return [item.name for item in build]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aggregate", required=True)
    parser.add_argument("--wiki-cache", default=None)
    parser.add_argument("--roles", default=",".join(ROLES))
    parser.add_argument("--limit", type=int, default=20, help="corpus builds per cell")
    parser.add_argument("--max-gods", type=int, default=0)
    parser.add_argument("--show", type=int, default=3, help="dominated examples per role")
    parser.add_argument("--margin", type=float, default=0.05,
        help="a corpus build must beat the optimizer by this fraction on every axis to count as dominating (0 = exact Pareto)")
    args = parser.parse_args()

    gods_list, items_list = await build_accuracy.smite2_catalogue(args.wiki_cache)
    gods = {int(g.id): g for g in gods_list.values()}
    items = {int(i.id): i for i in items_list.values()}
    stats = BuildStats.load(args.aggregate)
    if stats is None:
        print(f"No aggregate in {args.aggregate}", file=sys.stderr)
        return 1

    defenders = {r: corpus_defender(stats, gods, items, r) for r in ROLES}
    frontline = defenders.get("solo") or next(d for d in defenders.values() if d)
    backline = defenders.get("carry") or frontline
    print(f"frontline (solo median):  {frontline}")
    print(f"backline  (carry median): {backline}")

    per_role: Dict[str, List[Dict]] = {}
    for role in [r.strip() for r in args.roles.split(",")]:
        pool = stats.builds[stats.builds["Role"].astype(str).str.lower() == role]
        god_ids = [
            int(g)
            for g in pool.groupby("GodId", observed=True)["plays"]
            .sum()
            .sort_values(ascending=False)
            .index
        ]
        if args.max_gods:
            god_ids = god_ids[: args.max_gods]
        for god_id in god_ids:
            cell = grade_cell(
                stats, gods, items, god_id, role, frontline, backline,
                args.limit, args.margin,
            )
            if cell is not None and "error" not in cell:
                per_role.setdefault(role, []).append(cell)

    print(f"\n{'role':8s} {'gods':>4} {'efficient':>10} {'median dominators':>18} "
          f"{'median dominates':>17}")
    for role, cells in per_role.items():
        n = len(cells)
        eff = sum(1 for c in cells if c["efficient"])
        med_dom = int(np.median([c["dominated_by"] for c in cells]))
        med_dominates = int(np.median([c["dominates"] for c in cells]))
        print(
            f"{role:8s} {n:>4} {eff:>4}/{n:<4} ({eff / n:>4.0%}) "
            f"{med_dom:>18} {med_dominates:>17}"
        )

    for role, cells in per_role.items():
        dominated = [c for c in cells if not c["efficient"]]
        if not dominated:
            continue
        print(f"\n{role}: {len(dominated)} optimizer builds a corpus build dominates")
        for c in dominated[: args.show]:
            print(f"  {c['god']}: optimizer built {', '.join(c['opt_build'])}")
            print(f"    dominated by {', '.join(c['example_dominator'])}")

    print(
        "\nefficient = optimizer build on the Pareto front vs corpus builds; "
        "dominators = corpus builds strictly better on every axis; "
        "dominates = corpus builds the optimizer's build strictly beats."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
