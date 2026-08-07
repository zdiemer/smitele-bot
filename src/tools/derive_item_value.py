"""Derive what each item is worth beyond its stat line, from the corpus.

The stat model reads an item's numbers and the readable parts of its passive.
What it cannot read is the rest: an execute threshold, a cooldown refund on a
kill, a slow, a shield, an aura. That is a large part of why the corpus's
favourite items are its favourites, and no amount of weighting stat lines
recovers it.

This measures it instead. For each item, the win rate of builds containing it
against the win rate of builds that do not, over the same lane — a lift. An item
whose passive wins games shows a positive lift the stat model cannot explain,
and that residual is exactly the number the optimizer is missing.

The output is a *static table*, checked in and reviewed like the lane profiles.
`/optimize` never reads an aggregate at runtime: that is `/build`'s job, and the
separation is the point of having two commands. This is calibration performed
once, by hand, against data — the same thing a hand-tuned weight is, only
measured.

    python src/tools/derive_item_value.py --game smite2 \\
        --aggregate /matchdata/smite2 --out src/SmiteBot/item_value_smite2.json

Keyed by item *name* rather than id: Smite 1's ids churn between patches, and a
stale id silently scores nothing while a stale name is visible in review.

Read the output before trusting it. Smite 1's table bottoms out at exactly the
un-upgraded starters — Bluestone Pendant, Warrior's Axe, Bumba's Dagger, Charon's
Coin — all pinned at the clip. That is not a claim that starters lose games; it
is that a build still *holding* an un-upgraded starter at the end is a build from
a game that ended early or badly, so the lift measures the game rather than the
item. Anything using these tables for Smite 1 should restrict itself to the
tier-3 items that can actually hold a core slot.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), part)
    for part in ("HirezAPI", "SmiteBot", "ml")
]

import pandas as pd  # noqa: E402

ITEM_COLUMNS = tuple(f"ItemId{index}" for index in range(1, 7))

# Conquest only for Smite 2, where Arena and Assault are most of the corpus and
# have no lanes. Smite 1's corpus is far broader and its queue ids differ, so it
# is filtered by the same names via its own enum.
SMITE2_CONQUEST = (2_100_001, 2_100_002)

# An item needs to appear this often before its lift means anything. Below it,
# one lucky game moves the number more than the item does.
MIN_PLAYS = 40.0

# Lift is clipped to this before scaling, so a rarely-played item that happened
# to win cannot dominate. Win rates in a balanced game live within a few points
# of 50%, so a tenth is already an enormous edge.
MAX_LIFT = 0.10

# Evidence needed before a measured lift is taken at face value. An item with
# this many plays keeps half of what it measured.
SHRINKAGE = 200.0


async def smite2_catalogue(cache_path=None):
    from smite2 import items as s2items  # noqa: PLC0415
    from smite2.wiki_client import WikiClient  # noqa: PLC0415

    async with WikiClient(silent=True, cache_path=cache_path) as client:
        items, _ = await s2items.load(client, silent=True)
    return items


def smite1_catalogue(path: str):
    from item import Item  # noqa: PLC0415

    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    items = {}
    for obj in raw:
        try:
            made = Item.from_json(obj)
        except Exception:  # noqa: BLE001 — a retired item need not parse
            continue
        items[made.id] = made
    return items


def derive(frame: pd.DataFrame, items) -> dict:
    """Per-item lift in weighted win rate, against the rest of its lane."""
    plays = collections.Counter()
    wins = collections.Counter()
    lane_plays = collections.Counter()
    lane_wins = collections.Counter()
    item_lane_plays = collections.defaultdict(collections.Counter)
    item_lane_wins = collections.defaultdict(collections.Counter)

    for _, row in frame.iterrows():
        build = [int(row[column]) for column in ITEM_COLUMNS]
        known = [items[i].name for i in build if i in items]
        if len(known) < 6:
            continue
        lane = str(row["Role"])
        weight = float(row["wplays"])
        won = float(row["wwins"])
        lane_plays[lane] += weight
        lane_wins[lane] += won
        for name in set(known):
            plays[name] += weight
            wins[name] += won
            item_lane_plays[name][lane] += weight
            item_lane_wins[name][lane] += won

    values = {}
    for name, played in plays.items():
        if played < MIN_PLAYS:
            continue
        # Against the builds that did *not* take it, in the lanes it is actually
        # played in. Both halves matter. Comparing to the lane as a whole
        # includes the item in its own baseline, which drives the lift of a
        # popular item to zero by construction — Rod of Tahuti is in a third of
        # all mid builds, so measured that way the most-picked item in the game
        # looks worth nothing. And weighting by lane keeps a support item from
        # being judged against carry games.
        baseline_plays = baseline_wins = 0.0
        for lane, lane_played in item_lane_plays[name].items():
            share = lane_played / played
            without_plays = lane_plays[lane] - lane_played
            without_wins = lane_wins[lane] - item_lane_wins[name][lane]
            if without_plays <= 0:
                continue
            baseline_plays += share * without_plays
            baseline_wins += share * without_wins
        if baseline_plays <= 0:
            continue

        lift = (wins[name] / played) - (baseline_wins / baseline_plays)
        # Shrink toward zero by how little evidence there is. An item with 40
        # plays keeps a third of its measured lift; one with 800 keeps nearly
        # all of it. Without this the table is a list of whichever rarely-built
        # items happened to win, which is what the first version produced.
        lift *= played / (played + SHRINKAGE)
        lift = max(-MAX_LIFT, min(MAX_LIFT, lift))
        values[name] = round(lift / MAX_LIFT, 4)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=("smite", "smite2"), required=True)
    parser.add_argument("--aggregate", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--wiki-cache", default=None)
    parser.add_argument(
        "--items-json",
        default=None,
        help="Smite 1 only: the bot's cached items.json",
    )
    args = parser.parse_args()

    stats = pd.read_parquet(os.path.join(args.aggregate, "build_stats.parquet"))
    build_items = pd.read_parquet(os.path.join(args.aggregate, "build_items.parquet"))
    frame = stats.merge(build_items, on="BuildHash", how="inner")

    if args.game == "smite2":
        frame = frame[frame["match_queue_id"].isin(SMITE2_CONQUEST)]
        items = asyncio.run(smite2_catalogue(args.wiki_cache))
    else:
        from HirezAPI import QueueId  # noqa: PLC0415

        conquest = [
            queue.value
            for queue in QueueId
            if "CONQUEST" in queue.name and not QueueId.is_vs_ai(queue)
        ]
        frame = frame[frame["match_queue_id"].isin(conquest)]
        if not args.items_json:
            print("--items-json is required for Smite 1")
            return 1
        items = smite1_catalogue(args.items_json)

    values = derive(frame, items)
    if not values:
        print("No item cleared the play threshold — is this the right aggregate?")
        return 1

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(dict(sorted(values.items())), handle, indent=1, sort_keys=True)
        handle.write("\n")

    ranked = sorted(values.items(), key=lambda pair: -pair[1])
    print(f"{len(values)} items scored -> {args.out}")
    print("\nBest:")
    for name, value in ranked[:10]:
        print(f"  {value:+.2f}  {name}")
    print("\nWorst:")
    for name, value in ranked[-10:]:
        print(f"  {value:+.2f}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
