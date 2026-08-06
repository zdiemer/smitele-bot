"""Measure the Smite 2 optimizer against what actually wins.

The optimizer aims at the average stat shape of winning Conquest builds. This
asks the only question that matters about that: when it picks six items for a
god, how many of them are the six items that god's winners actually bought?

Run it after touching the profiles, the scoring, the item catalogue or the
adaptive/passive parsing. A change that improves the model raises the mean; one
that overfits a single god will show up as a wider spread rather than a higher
mean, which is why the distribution is printed too.

    python src/tools/smite2_accuracy.py [--min-wins 6] [--aggregate /matchdata/smite2]

Needs a built Smite 2 aggregate (`build_stats.parquet` and `build_items.parquet`)
and network access for the wiki catalogue, which it caches.

Conquest only, deliberately. Arena and Assault are more than three quarters of
the corpus and have no lanes; tracker.gg labels a role in them anyway, so an
"Arena support" is a full damage build. Scoring lane builds against that
population measures nothing at all — it was the first thing this harness got
wrong, and it made the optimizer look half as good as it was.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import sys

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), part)
    for part in ("HirezAPI", "SmiteBot")
]

import pandas as pd  # noqa: E402

from HirezAPI import PlayerRole  # noqa: E402
from smite2 import gods as s2gods, items as s2items  # noqa: E402
from smite2.wiki_client import WikiClient  # noqa: E402
from smite2_optimizer import Smite2BuildOptimizer  # noqa: E402

CONQUEST_QUEUES = (2_100_001, 2_100_002)
ITEM_COLUMNS = tuple(f"ItemId{index}" for index in range(1, 7))


async def catalogue(cache_path: str = None):
    async with WikiClient(silent=True, cache_path=cache_path) as client:
        items, _ = await s2items.load(client, silent=True)
        gods, _, _ = await s2gods.load(client, silent=True)
    return gods, items


def reference_builds(directory: str, min_wins: float):
    """Per god: its main lane, and its items ranked by win-weighted plays."""
    stats = pd.read_parquet(os.path.join(directory, "build_stats.parquet"))
    items = pd.read_parquet(os.path.join(directory, "build_items.parquet"))
    merged = stats.merge(items, on="BuildHash", how="inner")
    merged = merged[merged["match_queue_id"].isin(CONQUEST_QUEUES)]
    winners = merged[merged["wwins"] > 0]

    out = {}
    for god_id, group in winners.groupby("GodId"):
        role = group.groupby("Role", observed=True)["wwins"].sum().idxmax()
        in_role = group[group["Role"] == role]
        if float(in_role["wwins"].sum()) < min_wins:
            continue
        counts = collections.Counter()
        for _, row in in_role.iterrows():
            for column in ITEM_COLUMNS:
                counts[int(row[column])] += float(row["wwins"])
        out[int(god_id)] = (str(role), float(in_role["wwins"].sum()), counts)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aggregate",
        default=os.environ.get("SMITELE_MATCH_DATA_DIR", "."),
        help="directory holding build_stats.parquet and build_items.parquet",
    )
    parser.add_argument(
        "--min-wins",
        type=float,
        default=6.0,
        help="skip gods with fewer weighted Conquest wins in their main lane",
    )
    parser.add_argument("--wiki-cache", default=None)
    parser.add_argument(
        "--worst", type=int, default=10, help="how many mismatches to print"
    )
    args = parser.parse_args()

    references = reference_builds(args.aggregate, args.min_wins)
    if not references:
        print("No god cleared the win threshold — is this a Smite 2 aggregate?")
        return 1

    gods, items = asyncio.run(catalogue(args.wiki_cache))

    rows = []
    for god_id, (role, wins, counts) in references.items():
        god = gods.get(god_id)
        if god is None:
            continue
        try:
            player_role = PlayerRole(role.strip().lower())
        except ValueError:
            player_role = None
        optimizer = Smite2BuildOptimizer(god, items, role=player_role)
        # Only items that could still fill a core slot are comparable; an item
        # the corpus remembers but the catalogue no longer offers is not a miss.
        pool = {item.id for item in optimizer.core_items()}
        meta = [item for item, _ in counts.most_common() if item in pool][:6]
        if len(meta) < 6:
            continue
        picked = {item.id for item in optimizer.optimize()}
        rows.append(
            {
                "god": god.name,
                "role": role,
                "wins": wins,
                "overlap": len(picked & set(meta)),
                "meta": [items[i].name for i in meta],
                "picked": sorted(items[i].name for i in picked),
            }
        )

    rows.sort(key=lambda row: (row["overlap"], -row["wins"]))
    frame = pd.DataFrame(rows)
    print(f"gods scored: {len(frame)} (>= {args.min_wins:g} weighted Conquest wins)")
    print(f"mean overlap: {frame['overlap'].mean():.2f} / 6")
    print(f"median: {frame['overlap'].median():.0f}")
    print("distribution:")
    print(frame["overlap"].value_counts().sort_index().to_string())

    if args.worst:
        print(f"\nWorst {min(args.worst, len(rows))}:")
        for row in rows[: args.worst]:
            print(
                f"  {row['god']:<16} {row['role']:<8} {row['overlap']}/6"
                f"  wins={row['wins']:.0f}"
            )
            print(f"      meta:   {', '.join(row['meta'])}")
            print(f"      picked: {', '.join(row['picked'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
