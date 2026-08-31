"""Does knowing a Smite 2 player's Aspect make a better build recommendation?

The collector records an `Aspect` per row and the aggregate throws it away —
`build_aggregate.GROUP_KEYS` does not include it — so every (god, lane) cell
pools players who took the Aspect with players who did not, and `/build` has
never been able to say which of the two its answer is for.

That is either a harmless simplification or a serious one, and which it is
cannot be argued from first principles. Sol's Aspect of Conflagration removes
her Strength and gives Intelligence: for her the pooled cell is two incompatible
item sets under one hash pool, and averaging them describes nobody. Most Aspects
do nothing of the kind. So this measures it, per god and lane:

    python src/tools/aspect_value.py --wiki-cache <wiki_cache.json> --days 180

Three numbers, and the third is the one that decides
----------------------------------------------------

uptake    the share of rows in the cell that took the Aspect. Below a few
          percent there is nothing to condition on and nothing to gain.
divergence how much the two populations build differently: one minus the
          overlap of their six most-bought items. Zero means the Aspect changes
          how the god plays but not what it buys, which is the common case and
          the reason this cannot be answered by reading Aspect descriptions.
lift      the honest one. Hold out the last fortnight, recommend from the train
          window twice — once from the pooled cell, once from the player's own
          Aspect state — and score both against the held-out rows *in that
          state*. If conditioning is worth doing, the conditioned recommendation
          wins on rows where the Aspect matches.

Read the lift the way `build_eval` asks you to read its own: as an ordering
between two strategies over a shared set of cells, not as a causal effect. The
support column is there because a cell where only forty players took the Aspect
gives a lift computed over forty rows, and a big number over forty rows is
mostly noise.

What to do with the answer is deliberately not decided here. If conditioning
wins, `Aspect` belongs in `GROUP_KEYS` and `/build` needs an `aspect:` option —
that is a change to the aggregate, the bot and the embed, and it should be made
because a number said so rather than because Aspects sound important.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), part)
    for part in ("HirezAPI", "SmiteBot", "match_data_collector")
]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import build_accuracy  # noqa: E402
import build_aggregate  # noqa: E402
import build_ranker  # noqa: E402
import match_storage  # noqa: E402
from game import Game  # noqa: E402

ITEM_COLUMNS: List[str] = [f"ItemId{slot}" for slot in range(1, 7)]
CONQUEST = (2_100_001, 2_100_002)

COLUMNS = [
    "GodId",
    "Role",
    "Win_Status",
    "match_queue_id",
    "Aspect",
    "Conquest_Tier",
] + ITEM_COLUMNS


def load(paths: Sequence[str], verbose: bool) -> pd.DataFrame:
    """Conquest rows with a complete build, and the Aspect they were played on."""
    frames = []
    for index, path in enumerate(paths, start=1):
        frame = match_storage.read_frame_columns(path, COLUMNS)
        if not frame.shape[0]:
            continue
        frame = frame[
            pd.to_numeric(frame["match_queue_id"], errors="coerce").isin(CONQUEST)
        ]
        if not frame.shape[0]:
            continue
        frame = frame.copy()
        for column in ITEM_COLUMNS + ["GodId", "Aspect"]:
            frame[column] = (
                pd.to_numeric(frame[column], errors="coerce").fillna(0).astype("int64")
            )
        frame = frame[(frame[ITEM_COLUMNS] > 0).all(axis=1) & (frame["GodId"] != 0)]
        frame["won"] = (frame["Win_Status"] == "Winner").astype(np.int8)
        frame["Role"] = frame["Role"].astype(str).str.strip().str.title()
        frame["band"] = (
            pd.to_numeric(frame["Conquest_Tier"], errors="coerce")
            .fillna(0)
            .astype(np.int16)
        )
        frame["day"] = build_aggregate.corpus_date(path)
        if frame.shape[0]:
            frames.append(
                frame[["GodId", "Role", "Aspect", "won", "band", "day"] + ITEM_COLUMNS]
            )
        if verbose and index % 20 == 0:
            print(f"  read {index}/{len(paths)}", file=sys.stderr, flush=True)
    if not frames:
        return pd.DataFrame(columns=["GodId", "Role", "Aspect", "won", "band", "day"])
    return pd.concat(frames, ignore_index=True)


def top_items(frame: pd.DataFrame, count: int = 6) -> set:
    """The `count` most-bought items in a population."""
    counter = collections.Counter()
    for column in ITEM_COLUMNS:
        counter.update(frame[column].tolist())
    return {item for item, _ in counter.most_common(count)}


def recommend(frame: pd.DataFrame) -> Optional[Tuple[int, ...]]:
    """The shipped ranking's pick, computed directly off a set of rows.

    `BuildStats` reads the aggregate's tables; here there is only a frame, and
    building a whole aggregate per Aspect subset to reuse it would cost more
    than the arithmetic it saves. The estimator is the same one — that is what
    matters — called with the same weighted counts it would receive.
    """
    if not frame.shape[0]:
        return None
    builds = frame.groupby(
        [frame[column] for column in ITEM_COLUMNS], observed=True
    )["won"].agg(["size", "sum"])
    if not builds.shape[0]:
        return None
    rank = build_ranker.shrunk_rate(
        builds["size"].to_numpy(dtype=float), builds["sum"].to_numpy(dtype=float)
    )
    return tuple(int(value) for value in builds.index[int(np.argmax(rank))])


def lift(build: Sequence[int], holdout: pd.DataFrame, overlap: int) -> Optional[
    Tuple[float, int]
]:
    """`build_eval`'s skill-stratified lift, over one set of held-out rows."""
    if not holdout.shape[0]:
        return None
    wanted = np.asarray(sorted(set(int(i) for i in build)), np.int64)
    items = holdout[ITEM_COLUMNS].to_numpy(np.int64)
    near = np.isin(items, wanted).sum(axis=1) >= overlap
    support = int(near.sum())
    if not support:
        return None

    won = holdout["won"].to_numpy(np.float64)
    band = holdout["band"].to_numpy(np.int16)
    baseline = {
        int(value): float(won[band == value].mean()) for value in np.unique(band)
    }

    total = 0.0
    for value in np.unique(band[near]):
        in_band = (band == value) & near
        share = float(in_band.sum()) / support
        total += share * (float(won[in_band].mean()) - baseline[int(value)])
    return total, support


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--eval-days", type=int, default=14)
    parser.add_argument("--overlap", type=int, default=5)
    parser.add_argument("--wiki-cache", default=None)
    parser.add_argument(
        "--min-aspect-rows",
        type=int,
        default=150,
        help="train rows a cell needs in the Aspect-on state to be measured",
    )
    parser.add_argument(
        "--min-off-rows",
        type=int,
        default=0,
        help=(
            "train rows a cell needs in the Aspect-off state too. Raise it to "
            "look only at lanes people play both ways: a cell that is 98% "
            "Aspect-on is one where the Aspect *is* the lane, and there is "
            "nothing there to condition on"
        ),
    )
    parser.add_argument(
        "--min-support",
        type=int,
        default=20,
        help="held-out rows each recommendation needs before its lift counts",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    verbose = not args.quiet

    config = build_aggregate.GameConfig(Game.SMITE_2)
    corpus = match_storage.corpus_paths(*config.corpus_dirs)[-args.days :]
    if len(corpus) <= args.eval_days:
        print("Not enough corpus for a holdout.", file=sys.stderr)
        return 1

    gods, items = asyncio.run(build_accuracy.smite2_catalogue(args.wiki_cache))
    names = {int(god_id): god.name for god_id, god in gods.items()}
    item_names = {int(item.id): item.name for item in items.values()}

    print(f"Reading {len(corpus)} days…", flush=True)
    frame = load(corpus, verbose)
    if not frame.shape[0]:
        print("No Conquest rows with a complete build.", file=sys.stderr)
        return 1

    days = sorted(day for day in frame["day"].unique() if day is not None)
    cutoff = days[-args.eval_days]
    train = frame[frame["day"] < cutoff]
    holdout = frame[frame["day"] >= cutoff]
    print(
        f"{train.shape[0]:,} train rows, {holdout.shape[0]:,} held out from "
        f"{cutoff}. Aspect uptake overall: "
        f"{(frame['Aspect'] != 0).mean():.1%}",
        flush=True,
    )

    rows = []
    for (god_id, role), cell in train.groupby(["GodId", "Role"], observed=True):
        if role in ("Unknown", "Adc") or cell.shape[0] < args.min_aspect_rows:
            continue
        # The Aspect a cell actually uses, rather than "any Aspect": a god with
        # one Aspect has one alternative, and treating several as a single
        # "on" state would compare a mixture against a mixture.
        taken = cell[cell["Aspect"] != 0]
        if not taken.shape[0]:
            continue
        aspect_id = int(taken["Aspect"].value_counts().idxmax())
        on = cell[cell["Aspect"] == aspect_id]
        off = cell[cell["Aspect"] == 0]
        if on.shape[0] < args.min_aspect_rows or off.shape[0] < args.min_off_rows:
            continue

        pooled_build = recommend(cell)
        aspect_build = recommend(on)
        if pooled_build is None or aspect_build is None:
            continue

        held = holdout[
            (holdout["GodId"] == god_id)
            & (holdout["Role"] == role)
            & (holdout["Aspect"] == aspect_id)
        ]
        pooled = lift(pooled_build, held, args.overlap)
        aspect = lift(aspect_build, held, args.overlap)

        rows.append(
            {
                "god": names.get(int(god_id), str(god_id)),
                "lane": str(role),
                "uptake": on.shape[0] / cell.shape[0],
                "divergence": 1.0
                - len(top_items(on) & top_items(off)) / 6.0,
                "same_pick": pooled_build == aspect_build,
                "pooled_lift": pooled[0] if pooled else None,
                "aspect_lift": aspect[0] if aspect else None,
                "support": min(
                    pooled[1] if pooled else 0, aspect[1] if aspect else 0
                ),
                "aspect_items": [
                    item_names.get(i, str(i)) for i in aspect_build
                ],
            }
        )

    if not rows:
        print(
            f"No cell reached {args.min_aspect_rows} Aspect rows. Try --days.",
            file=sys.stderr,
        )
        return 1

    table = pd.DataFrame(rows).sort_values("uptake", ascending=False)
    pd.set_option("display.width", 200)
    print(
        f"\n{'god':16}{'lane':9}{'uptake':>8}{'diverge':>9}{'pooled':>9}"
        f"{'aspect':>9}{'support':>9}  same pick"
    )
    print("-" * 78)
    for _, row in table.iterrows():
        pooled = (
            f"{row['pooled_lift']:>+8.2%}" if row["pooled_lift"] is not None else "       -"
        )
        aspect = (
            f"{row['aspect_lift']:>+8.2%}" if row["aspect_lift"] is not None else "       -"
        )
        print(
            f"{row['god']:16}{row['lane']:9}{row['uptake']:>7.1%}"
            f"{row['divergence']:>9.2f}{pooled:>9}{aspect:>9}"
            f"{row['support']:>9}  {'yes' if row['same_pick'] else 'NO'}"
        )

    decided = table[
        table["pooled_lift"].notna() & table["aspect_lift"].notna()
    ]
    both = decided[decided["support"] >= args.min_support]
    print(
        f"\n{len(table)} cells measured, {len(both)} with "
        f"{args.min_support}+ held-out rows each."
    )
    if len(both):
        wins = int((both["aspect_lift"] > both["pooled_lift"]).sum())
        losses = int((both["aspect_lift"] < both["pooled_lift"]).sum())
        print(
            f"Conditioning on the Aspect beats the pooled cell in {wins} of "
            f"{wins + losses} decided cells."
        )
        print(
            f"Mean lift, support-weighted:  pooled "
            f"{np.average(both['pooled_lift'], weights=both['support']):+.2%}   "
            f"aspect-conditioned "
            f"{np.average(both['aspect_lift'], weights=both['support']):+.2%}"
        )
    changed = int((~table["same_pick"]).sum())
    print(
        f"The Aspect changes the recommended build in {changed} of "
        f"{len(table)} cells; median divergence in what the two populations "
        f"buy is {table['divergence'].median():.2f}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
