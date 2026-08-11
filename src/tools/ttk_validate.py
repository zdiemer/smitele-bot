"""Does the TTK simulator order builds the way the corpus does?

`calculate_basic_ttk` is the candidate metric for grading the stat-model
optimizer, whose builds the corpus cannot score (they have a median of one or
two held-out plays — see the comment above `EMPIRICAL_WEIGHT` in
`smite2_optimizer.py`). Before the sim is allowed to grade anything, it has to
pass the one test the corpus *can* run: among builds real players ran for the
same god in the same lane, does simulated TTK agree with how often those builds
actually won?

    python src/tools/ttk_validate.py --aggregate <dir with build_stats.parquet>

Per cell (god, role) — queue is summed over unless --queue is given, because
the Role column only exists in conquest-shaped queues anyway — the harness
takes the most-played builds, simulates each one attacking the same corpus-
derived defenders, and reports the Spearman correlation between TTK and the
build's empirical win rate. Lower TTK should mean more wins: healthy cells are
*negative*.

Choices that are load-bearing rather than convenient:

- Candidate builds are selected by *plays*, not by any ranking that touches
  wins. Selecting on the outcome variable would clip the very spread the
  correlation is computed over.
- Defenders are the most-played build of the most-played god in each of the
  five roles, from the same aggregate. The corpus cannot score hypothetical
  builds, but it states exactly what a build will face.
- The correlation is also reported against the *shrunk* win rate (the bot's
  own estimator) because a 30-play build's raw rate is mostly noise, and the
  question is whether TTK agrees with the ordering the evidence supports, not
  with sampling error.
- Crits make the sim stochastic, so each matchup is averaged over --trials
  seeded runs; the seed is derived from (build, defender, trial) so every
  strategy sees the same dice.

This validates ordering only. TTK is computed from the same stat model the
optimizer optimises, so agreement here cannot prove the model right — it can
only prove the sim is not *internally* wrong. That bound is the point: a sim
that cannot reproduce the corpus ordering where basic attacks dominate (Carry)
has no business grading anything.

Measured 2026-08-10, Smite 1 production aggregate, Carry cells
--------------------------------------------------------------

Before the sim's item passives were checked against the wiki, the correlation
ran the *wrong way*: median rho +0.04, negative in only 13/34 meta cells. The
audit found Qin's at double its real value, Demon Blade's cap written as a
floor (`max` for `min`, so any crit granted >=40% pen), Silverbranch at 2
power per 0.02 instead of 3, Berserker's triggering above half health instead
of below 60%, invented Spectral stacking, and more. After fixing exactly what
the wiki says and nothing else:

    meta pool (>=30 plays, top 60): median rho -0.10, negative in 24/34 cells
    wide pool (>=10 plays, top 200): median rho -0.06, negative in 37/49 cells

Right direction, modest size. The residual is explained by what the sim still
does not model (Odysseus' Bow, Fail-Not, Asi, Ichaival and friends contribute
nothing but their stats) and by what no duel sim can see: the strongest
win-rate separators among meta builds are evolved-versus-base item states
(Ornate vs Gilded Arrow at +4 vs -11 points), which partly proxy how far ahead
the game already was. Where the pool spans genuinely bad builds the signal is
much larger (Poseidon-as-carry rho -0.55, best-third TTK 60s vs worst 142s) —
and grading an optimizer, whose builds can land anywhere in build space, leans
on exactly that gross discrimination.

The same day, three more layers (each measured on the meta pool):

    variant ids, mana-to-power, the attack speed pipeline:   median -0.13
    --stacked (gold- and kill-fed passives at full stacks):  median -0.22
    --stacked --abilities (rotation + hunter steroids):      median -0.31,
        negative in 27/34 cells, best third TTK-faster in 28/34

The attack speed fix *lowered* rho relative to the broken pipeline it
replaced (every build had been pinned to the fire cap, and the spurious
Silverbranch overcap power was compensating for then-unmodelled damage) —
kept anyway, because it is verifiably correct against a hand-computed build
and the metric is a guide, not the definition of truth. --stacked is the
right mode for grading finished builds: corpus builds are end-of-game
snapshots, and the assumption alone is worth roughly a doubling. Abilities
are what moved the ability-led cells: Medusa, inverted at +0.45 through
every item fix, sits at +0.18 with her rotation modelled — the rest of her
gap is the sim's remaining blind spot for ability-item interplay, not a
reason to distrust the carry signal.

Every role, --stacked --abilities, same day:

    Carry:  median -0.31, negative in 27/34 cells
    Solo:   median -0.23, negative in 55/74
    Mid:    median -0.20, negative in 34/52
    Jungle: median -0.10, negative in 41/58

Solo ordering this well on raw TTK — before the planned TTK-times-EHP
hybrid — was not expected. The known bias, measured rather than assumed:
correlating each cell's rho against how much attack speed its corpus builds
carry gives +0.37 (Mid) and +0.40 (Solo) — the *high*-attack-speed cells
order worse — against -0.14 for Carry. The sim weaves basics at full uptime
between casts, which only carries actually do, so attack speed is worth
more in the sim than in the hands of a god who also aims, casts and
repositions. The next dial is a per-role weave fraction, sweepable against
exactly these cells.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from typing import Dict, List, Optional, Tuple

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), part)
    for part in ("HirezAPI", "SmiteBot", "ml")
]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import build_accuracy  # noqa: E402
from build_ranker import BuildStats, shrunk_rate  # noqa: E402
from ability_kit import STEROIDS, parse_kit  # noqa: E402
from stat_calculator import DamageCalculator, GodBuild  # noqa: E402

DEFENDER_ROLES = ("Solo", "Jungle", "Mid", "Support", "Carry")


def catalogue(gods_json: str, items_json: str) -> Tuple[Dict, Dict]:
    """The bot's parsed god and item tables, keyed by plain int id."""
    gods, items = build_accuracy.smite1_catalogue(gods_json, items_json)
    return (
        {int(god.id.value): god for god in gods.values()},
        {int(item.id): item for item in items.values()},
    )


def corpus_defenders(
    stats: BuildStats, gods: Dict, items: Dict, queue_id: Optional[int]
) -> List[Tuple[str, GodBuild]]:
    """One defender per role: the most-played build of its most-played god.

    Walks down the popularity order until it finds a (god, build) whose god
    and all six items still parse from the catalogue — a retired god or item
    would otherwise silently drop a role.
    """
    frame = stats.builds
    if queue_id is not None:
        frame = frame[frame["match_queue_id"] == queue_id]
    out: List[Tuple[str, GodBuild]] = []
    for role in DEFENDER_ROLES:
        pool = frame[frame["Role"].astype(str) == role]
        by_god = pool.groupby("GodId", observed=True)["plays"].sum()
        for god_id in by_god.sort_values(ascending=False).index:
            god = gods.get(int(god_id))
            if god is None:
                continue
            grouped = (
                pool[pool["GodId"] == god_id]
                .groupby("BuildHash", observed=True)["plays"]
                .sum()
            )
            build = None
            for build_hash in grouped.sort_values(ascending=False).index:
                ids = stats.items_for(build_hash)
                if len(ids) == 6 and all(i in items for i in ids):
                    build = [items[i] for i in ids]
                    break
            if build is not None:
                out.append((role, GodBuild(god, build, 20)))
                break
    return out


def ttk_score(
    attacker: GodBuild,
    build_hash,
    defenders: List[Tuple[str, GodBuild]],
    trials: int,
    stacked: bool = False,
    abilities: bool = False,
) -> float:
    """Summed mean TTK against every defender, deterministically seeded."""
    kit = parse_kit(attacker.god) if abilities else None
    steroid = STEROIDS.get(attacker.god.name) if abilities else None
    total = 0.0
    for role, defender in defenders:
        acc = 0.0
        for trial in range(trials):
            random.seed(f"{build_hash}:{role}:{trial}")
            acc += DamageCalculator.calculate_basic_ttk(
                attacker,
                defender,
                assume_item_passives_stacked=stacked,
                kit=kit,
                steroid=steroid,
            )
        total += acc / trials
    return total


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    xr = pd.Series(x).rank().to_numpy()
    yr = pd.Series(y).rank().to_numpy()
    if np.std(xr) == 0 or np.std(yr) == 0:
        return float("nan")
    return float(np.corrcoef(xr, yr)[0, 1])


def validate_cell(
    stats: BuildStats,
    gods: Dict,
    items: Dict,
    god_id: int,
    role: str,
    queue_id: Optional[int],
    defenders: List[Tuple[str, GodBuild]],
    min_plays: int,
    limit: int,
    trials: int,
    stacked: bool = False,
    abilities: bool = False,
) -> Optional[Dict]:
    god = gods.get(god_id)
    if god is None:
        return None
    # Ranked by weighted plays alone: the candidate set must not be selected
    # on the thing the correlation is measured against.
    candidates = stats.ranked_builds(
        god_id,
        queue_id=queue_id,
        role=role,
        ranking=lambda wplays, wwins: np.asarray(wplays),
        min_plays=min_plays,
        limit=limit,
    )
    candidates = [c for c in candidates if c["plays"] >= min_plays]
    rows = []
    skipped = 0
    for cand in candidates:
        if not all(i in items for i in cand["items"]):
            skipped += 1
            continue
        attacker = GodBuild(god, [items[i] for i in cand["items"]], 20)
        rows.append(
            {
                "build_hash": str(cand["build_hash"]),
                "plays": cand["plays"],
                "wins": cand["wins"],
                "win_rate": cand["win_rate"],
                "ttk": ttk_score(
                    attacker, cand["build_hash"], defenders, trials, stacked,
                    abilities,
                ),
            }
        )
    if len(rows) < 8:
        return None
    plays = np.array([r["plays"] for r in rows], dtype=float)
    wins = np.array([r["wins"] for r in rows], dtype=float)
    ttk = np.array([r["ttk"] for r in rows])
    raw = wins / plays
    shrunk = shrunk_rate(plays, wins, pessimism=0.0)
    for row, s in zip(rows, shrunk):
        row["shrunk_rate"] = float(s)
    # Tercile contrast: the check a correlation can pass by accident on ties.
    order = np.argsort(shrunk)
    third = max(len(rows) // 3, 1)
    return {
        "god_id": god_id,
        "god": god.name,
        "role": role,
        "builds": len(rows),
        "skipped_unknown_items": skipped,
        "median_plays": float(np.median(plays)),
        "rho_raw": spearman(ttk, raw),
        "rho_shrunk": spearman(ttk, shrunk),
        "ttk_bottom_third": float(np.mean(ttk[order[:third]])),
        "ttk_top_third": float(np.mean(ttk[order[-third:]])),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aggregate", required=True)
    parser.add_argument("--gods-json", default=None)
    parser.add_argument("--items-json", default=None)
    parser.add_argument("--roles", default="Carry", help="comma-separated cells to test")
    parser.add_argument("--queue", type=int, default=None)
    parser.add_argument("--min-plays", type=int, default=30)
    parser.add_argument("--limit", type=int, default=60, help="builds per cell")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument(
        "--stacked",
        action="store_true",
        help="assume gold- and kill-fed item passives are at full stacks",
    )
    parser.add_argument(
        "--abilities",
        action="store_true",
        help="cast the god's damaging abilities on cooldown alongside basics",
    )
    parser.add_argument("--max-gods", type=int, default=0, help="0 = all")
    parser.add_argument("--out", default=None, help="write full per-build rows as JSON")
    args = parser.parse_args()

    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    gods, items = catalogue(
        args.gods_json or os.path.join(here, "gods.json"),
        args.items_json or os.path.join(here, "items.json"),
    )
    stats = BuildStats.load(args.aggregate)
    if stats is None:
        print(f"No aggregate in {args.aggregate}", file=sys.stderr)
        return 1

    defenders = corpus_defenders(stats, gods, items, args.queue)
    print("Defenders (most-played build of the role's most-played god):")
    for role, defender in defenders:
        names = ", ".join(item.name for item in defender.build)
        print(f"  {role:8s} {defender.god.name}: {names}")

    results = []
    for role in args.roles.split(","):
        role = role.strip()
        pool = stats.builds[stats.builds["Role"].astype(str) == role]
        if args.queue is not None:
            pool = pool[pool["match_queue_id"] == args.queue]
        by_god = (
            pool.groupby("GodId", observed=True)["plays"]
            .sum()
            .sort_values(ascending=False)
        )
        god_ids = [int(g) for g in by_god.index]
        if args.max_gods:
            god_ids = god_ids[: args.max_gods]
        for god_id in god_ids:
            cell = validate_cell(
                stats, gods, items, god_id, role, args.queue,
                defenders, args.min_plays, args.limit, args.trials, args.stacked,
                args.abilities,
            )
            if cell is None:
                continue
            results.append(cell)
            print(
                f"{cell['god']:15s} {role:6s} builds={cell['builds']:3d} "
                f"med_plays={cell['median_plays']:6.0f} "
                f"rho_raw={cell['rho_raw']:+.3f} rho_shrunk={cell['rho_shrunk']:+.3f} "
                f"ttk(best third)={cell['ttk_top_third']:6.1f} "
                f"ttk(worst third)={cell['ttk_bottom_third']:6.1f}",
                flush=True,
            )

    if not results:
        print("No cells had enough supported builds to test.")
        return 1

    rho_raw = np.array([c["rho_raw"] for c in results])
    rho_shrunk = np.array([c["rho_shrunk"] for c in results])
    builds = np.array([c["builds"] for c in results], dtype=float)
    print(f"\nCells tested: {len(results)}")
    print(
        f"rho_raw:    median {np.nanmedian(rho_raw):+.3f}, "
        f"weighted mean {np.nansum(rho_raw * builds) / builds.sum():+.3f}, "
        f"negative in {int((rho_raw < 0).sum())}/{len(results)} cells"
    )
    print(
        f"rho_shrunk: median {np.nanmedian(rho_shrunk):+.3f}, "
        f"weighted mean {np.nansum(rho_shrunk * builds) / builds.sum():+.3f}, "
        f"negative in {int((rho_shrunk < 0).sum())}/{len(results)} cells"
    )
    faster = int(
        (np.array([c["ttk_top_third"] for c in results])
         < np.array([c["ttk_bottom_third"] for c in results])).sum()
    )
    print(
        f"top-third-by-win-rate builds are TTK-faster than bottom-third in "
        f"{faster}/{len(results)} cells"
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=1)
        print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
