"""Measure either game's optimizer against what actually wins.

The optimizers pick six items from a stat model. This asks the only question
that really tests one: when it builds a god, how many of those six are the six
items that god's winners actually bought?

    python src/tools/build_accuracy.py --game smite2 --aggregate /matchdata/smite2
    python src/tools/build_accuracy.py --game smite  --aggregate /matchdata

Run it after touching a scoring weight, a stat target, a passive pattern or the
item catalogue. A change that improves the model raises the mean; one that
overfits a single god shows up as a wider spread rather than a higher mean,
which is why the distribution is printed too.

Both games, because both have the same question and the same failure mode. What
differs is only where the catalogue comes from — the Smite 2 wiki, or the bot's
cached Hi-Rez responses — and how long it takes: Smite 2 scores a god in
milliseconds, Smite 1 searches combinations and takes seconds, so a full Smite 1
sweep is minutes and `--limit` exists for when that is too long.

Conquest only, in both. Arena and Assault have no lanes, tracker.gg labels a
role in them anyway, and an "Arena support" is a full damage build; scoring lane
builds against that population measures nothing at all. It was the first thing
this harness got wrong, and it made the Smite 2 optimizer look half as good as
it was.

The measure is agreement with what people *play*. That is not the same as what
wins — the two genuinely disagree on some items — so treat a movement of a few
hundredths as noise, and prefer a change that moves it by a lot over one that
moves it by a little.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
import time

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), part)
    for part in ("HirezAPI", "SmiteBot", "ml")
]

import pandas as pd  # noqa: E402

from god import God  # noqa: E402
from god_builder import valid_items_for_god  # noqa: E402
from item import Item  # noqa: E402
from HirezAPI import PlayerRole, QueueId  # noqa: E402

ITEM_COLUMNS = tuple(f"ItemId{index}" for index in range(1, 7))

SMITE2_CONQUEST = (2_100_001, 2_100_002)


def smite1_conquest_queues():
    return [
        queue.value
        for queue in QueueId
        if "CONQUEST" in queue.name and not QueueId.is_vs_ai(queue)
    ]


async def smite2_catalogue(cache_path=None):
    from smite2 import gods as s2gods, items as s2items  # noqa: PLC0415
    from smite2.wiki_client import WikiClient  # noqa: PLC0415

    async with WikiClient(silent=True, cache_path=cache_path) as client:
        items, _ = await s2items.load(client, silent=True)
        gods, _, _ = await s2gods.load(client, silent=True)
    return gods, items


def smite1_catalogue(gods_json: str, items_json: str):
    """The bot's cached Hi-Rez responses, parsed the way the bot parses them."""

    def load(path, builder):
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        out = {}
        for obj in raw:
            try:
                made = builder(obj)
            except Exception:  # noqa: BLE001 — a retired god or item need not parse
                continue
            out[made.id] = made
        return out

    return load(gods_json, God.from_json), load(items_json, Item.from_json)


def reference_builds(directory: str, queues, min_wins: float):
    """Per god: its main lane, and its items ranked by win-weighted plays."""
    stats = pd.read_parquet(os.path.join(directory, "build_stats.parquet"))
    items = pd.read_parquet(os.path.join(directory, "build_items.parquet"))
    merged = stats.merge(items, on="BuildHash", how="inner")
    merged = merged[merged["match_queue_id"].isin(queues)]
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


async def score_smite2(gods, items, references, verbose):
    from smite2_optimizer import Smite2BuildOptimizer  # noqa: PLC0415

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
        pool = {item.id for item in optimizer.core_items()}
        rows.append(_row(god, role, wins, counts, pool, optimizer.optimize(), items))
        if verbose:
            _progress(rows, len(references))
    return [row for row in rows if row]


async def score_smite1(gods, items, references, verbose, limit):
    from build_optimizer import BuildOptimizer  # noqa: PLC0415

    ranked = sorted(references.items(), key=lambda pair: -pair[1][1])
    if limit:
        ranked = ranked[:limit]

    rows = []
    for god_id, (role, wins, counts) in ranked:
        god = next((g for g in gods.values() if int(g.id.value) == god_id), None)
        if god is None or god.role is None:
            continue
        valid = valid_items_for_god(god, items)
        optimizer = BuildOptimizer(god, valid, items)
        builds, _iterations = await optimizer.optimize()
        if not builds:
            continue
        build = optimizer.rank_builds(builds)[0]
        # Only items that could hold a core slot are comparable; the reference
        # counts include starters, which the six-item search never picks.
        pool = {item.id for item in optimizer.filter_tiers_with_glyphs(valid)}
        rows.append(_row(god, role, wins, counts, pool, build, items))
        if verbose:
            _progress(rows, len(ranked))
    return [row for row in rows if row]


def _row(god, role, wins, counts, pool, build, items):
    """One god's comparison, or None when there is nothing comparable."""
    meta = [item for item, _ in counts.most_common() if item in pool][:6]
    if len(meta) < 6:
        return None
    picked = {item.id for item in build}
    return {
        "god": god.name,
        "role": str(role),
        "wins": wins,
        "overlap": len(picked & set(meta)),
        "meta": [items[i].name for i in meta if i in items],
        "picked": sorted(items[i].name for i in picked if i in items),
    }


_started = time.monotonic()


def _progress(rows, total):
    done = len(rows)
    if done % 5 and done != total:
        return
    elapsed = time.monotonic() - _started
    print(f"  … {done}/{total} gods, {elapsed:.0f}s", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=("smite", "smite2"), default="smite2")
    parser.add_argument(
        "--aggregate",
        default=os.environ.get("SMITELE_MATCH_DATA_DIR", "."),
        help="directory holding build_stats.parquet and build_items.parquet",
    )
    parser.add_argument(
        "--min-wins",
        type=float,
        default=None,
        help="skip gods with fewer weighted Conquest wins in their main lane",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Smite 1 only: score the N most-played gods rather than all of them",
    )
    parser.add_argument("--wiki-cache", default=None)
    parser.add_argument("--gods-json", default=None)
    parser.add_argument("--items-json", default=None)
    parser.add_argument("--worst", type=int, default=10)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    # Smite 1's corpus is far larger, so the same floor would admit gods with a
    # handful of games in Smite 2 and hundreds in Smite 1.
    min_wins = args.min_wins if args.min_wins is not None else (
        6.0 if args.game == "smite2" else 40.0
    )
    queues = SMITE2_CONQUEST if args.game == "smite2" else smite1_conquest_queues()

    references = reference_builds(args.aggregate, queues, min_wins)
    if not references:
        print(f"No god cleared {min_wins:g} wins — is this a {args.game} aggregate?")
        return 1

    if args.game == "smite2":
        gods, items = asyncio.run(smite2_catalogue(args.wiki_cache))
        rows = asyncio.run(score_smite2(gods, items, references, not args.quiet))
    else:
        data_dir = os.environ.get("SMITELE_DATA_DIR", ".")
        gods_json = args.gods_json or os.path.join(data_dir, "gods.json")
        items_json = args.items_json or os.path.join(data_dir, "items.json")
        gods, items = smite1_catalogue(gods_json, items_json)
        rows = asyncio.run(
            score_smite1(gods, items, references, not args.quiet, args.limit)
        )

    if not rows:
        print("Nothing comparable — no god had six core items in the corpus.")
        return 1

    rows.sort(key=lambda row: (row["overlap"], -row["wins"]))
    frame = pd.DataFrame(rows)
    print(f"\ngame: {args.game}")
    print(f"gods scored: {len(frame)} (>= {min_wins:g} weighted Conquest wins)")
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
