#!/usr/bin/env python3
"""The nightly Smite 2 crawl.

The Smite 1 collector enumerates a day: queue by queue, ten minutes at a time,
until the day is provably whole. tracker.gg has no time enumeration at all —
matches are reachable only through the players who played them — so this cannot
work the same way and does not try.

Instead it snowballs. Seed from the leaderboards, read a player's most recent
page, and everyone in those matches becomes queryable in turn. Rows are filed by
the day the match was played rather than the day it was found, so one night
backfills the three calendar days a page spans instead of discarding two of them.

Coverage is measured rather than assumed, by capture-recapture on which half of
the roster surfaced each match, so the budget can be aimed at a coverage target
instead of at a request count someone guessed.

Everything about the pacing is deliberately conservative. This is an undocumented
endpoint behind a WAF with no published rate limit and no allowance for bulk
reads: one request at a time, a fixed gap between them, and a hard stop on the
first sign of being blocked rather than a retry.

    python src/smite2_collector/collect.py --dry-run
    python src/smite2_collector/collect.py --budget 4000 --hours 5
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
import time
from typing import Dict, List, Set, Tuple

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "HirezAPI"
    ),
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths  # noqa: E402
from game import Game  # noqa: E402
from smite2.clearance import (  # noqa: E402
    ClearanceManager,
    ClearanceStore,
    ClearanceUnavailable,
)
from smite2.provider import CLEARANCE_FILE, Smite2Provider  # noqa: E402
from smite2.tracker_client import (  # noqa: E402
    LEADERBOARDS,
    TrackerBlocked,
    TrackerClient,
    TrackerServerError,
    leaderboard_players,
)

import coverage as coverage_module  # noqa: E402
import frontier as frontier_module  # noqa: E402
import rows as rows_module  # noqa: E402
import store as store_module  # noqa: E402


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


async def seed(client: TrackerClient, frontier, today: str, silent: bool) -> int:
    """Top up the roster from the leaderboards.

    Cheap — seven requests for 350 entries — and the only way to reach players
    the snowball has not touched, which matters because a snowball explores one
    connected component and the population has more than one.
    """
    added = 0
    for board in LEADERBOARDS:
        try:
            entries = await client.leaderboard(board)
        except TrackerServerError as error:
            # One board 500s persistently. The others still seed.
            if not silent:
                print(f"  leaderboard {board}: {error}", flush=True)
            continue
        for platform, handle in leaderboard_players(entries):
            before = len(frontier.players)
            frontier.add(platform, handle, today)
            added += len(frontier.players) - before
    return added


async def crawl(args) -> int:
    started = time.time()
    deadline = started + args.hours * 3600
    today = _today()

    corpus_dir = paths.game_match_data_dir(Game.SMITE_2)
    state_dir = paths.game_model_dir(Game.SMITE_2)

    print(f"Smite 2 crawl · budget {args.budget:,} requests · {args.hours}h cap")
    print(f"  corpus  {corpus_dir}")
    print(f"  state   {state_dir}")
    if args.dry_run:
        print("  DRY RUN — nothing will be written")

    # The god index comes from the wiki, and without it every row would have
    # GodId 0 and be dropped by the aggregate. Worth failing loudly for.
    provider = Smite2Provider(silent=True)
    await provider.create()
    if not provider.gods:
        print("Could not load the god catalogue; refusing to crawl.", flush=True)
        return 1
    god_ids = _god_id_lookup(provider)
    print(f"  {len(provider.gods)} gods known")

    frontier = frontier_module.Frontier(state_dir)
    seen = store_module.SeenMatches(state_dir)
    tracker = coverage_module.CoverageTracker()
    buffer = store_module.RowBuffer(corpus_dir, flush_every=args.flush_every)

    print(f"  {frontier.summary()}")
    print(f"  {len(seen):,} matches already collected")

    manager = ClearanceManager(
        ClearanceStore(os.path.join(state_dir, CLEARANCE_FILE))
    )
    if args.reset_clearance:
        manager.reset()

    new_matches = 0
    unknown_items = 0
    item_slots = 0
    blocked = False

    async with TrackerClient(manager, interval=args.interval) as client:
        try:
            added = await seed(client, frontier, today, args.quiet)
            print(f"  seeded {added:,} new players from the leaderboards")

            pending = frontier.select(args.budget, today)
            print(f"  {len(pending):,} players to start with\n")

            visited: Set[str] = set()
            index = 0
            discovered_total = 0

            while pending:
                if time.time() > deadline:
                    print(f"\nWall clock cap reached after {index} players.")
                    break
                if client.requests >= args.budget:
                    print(f"\nRequest budget spent after {index} players.")
                    break

                player = pending.pop(0)
                if player.key in visited:
                    continue
                visited.add(player.key)
                index += 1

                found, fresh, counts, parties, discovered = await _visit(
                    client, player, god_ids, seen, tracker, buffer, args
                )
                frontier.record_visit(player, today, found, fresh)
                frontier.note_parties(parties)
                new_matches += fresh
                unknown_items += counts[0]
                item_slots += counts[1]

                # This is the snowball. Everyone in the matches just read
                # becomes queryable, which is the only way the frontier grows
                # beyond the few hundred players the leaderboards name.
                before = len(frontier.players)
                for platform, handle in discovered:
                    frontier.add(platform, handle, today)
                discovered_total += len(frontier.players) - before

                if not args.dry_run:
                    buffer.maybe_flush()

                if index % 25 == 0:
                    _progress(
                        index, len(pending), client, new_matches, tracker, started
                    )

                if args.coverage_target:
                    estimate = tracker.best_estimate()
                    if estimate is not None and estimate >= args.coverage_target:
                        print(
                            f"\nCoverage target {args.coverage_target:.0%} reached "
                            f"(estimated {estimate:.0%}); stopping early."
                        )
                        break

                # Refill from what this run has discovered, so a night is
                # bounded by its budget rather than by however many players the
                # leaderboards happened to name.
                if not pending:
                    remaining = args.budget - client.requests
                    if remaining > 0:
                        pending = [
                            p
                            for p in frontier.select(remaining, today)
                            if p.key not in visited
                        ]
                        if pending:
                            print(
                                f"  refilled with {len(pending):,} newly "
                                f"discovered players",
                                flush=True,
                            )

            print(f"\n  {discovered_total:,} players discovered this run")

        except ClearanceUnavailable as error:
            print(f"\nSTOPPED — no clearance: {error}")
            blocked = True
        except TrackerBlocked as error:
            print(f"\nSTOPPED — {error}")
            blocked = True

    if not args.dry_run:
        buffer.flush()
        seen.save()
        frontier.save()

    elapsed = time.time() - started
    print(f"\n=== {client.requests:,} requests · {client.bytes / 1e6:,.0f} MB · "
          f"{elapsed / 60:.0f} min ===")
    print(f"  {new_matches:,} new matches · {buffer.written:,} rows written")
    if item_slots:
        share = 100.0 * unknown_items / item_slots
        print(
            f"  {unknown_items:,} of {item_slots:,} item slots unnameable "
            f"({share:.2f}%)"
            + ("  ← check the wiki join" if share > 2 else "")
        )
    print(f"  {frontier.summary()}")
    print("\nCoverage:")
    print(tracker.report())

    return 2 if blocked else 0


async def _visit(client, player, god_ids, seen, tracker, buffer, args):
    """Read one player's most recent page and absorb what is new.

    Returns matches seen, matches new, (unnameable items, item slots), the
    parties observed — which is how premade suppression learns who queues
    together — and every player named in those matches, which is the snowball.
    """
    found = fresh = unknown = slots = 0
    parties: Dict[str, Set[str]] = {}
    discovered: Set[Tuple[str, str]] = set()

    try:
        async for match in client.iter_matches(player.platform, player.handle, 0):
            found += 1
            match_id = (match.get("attributes") or {}).get("id")
            date = rows_module.match_date(match)
            if not match_id or not date:
                continue

            tracker.observe(date, str(match_id), player.key)
            for party, members in frontier_module.parties_in(match).items():
                parties.setdefault(party, set()).update(members)
            # Collected from every match, not only new ones: a match we already
            # have can still name a player we have never queried.
            discovered.update(frontier_module.players_in(match))

            if str(match_id) in seen:
                continue

            fresh += 1
            seen.add(str(match_id), date)
            for record in rows_module.player_rows(match, god_ids):
                unknown += record.pop("UnknownItems", 0)
                slots += record.pop("ItemSlots", 0)
                if not args.dry_run:
                    buffer.add(date, record)
    except (TrackerBlocked, ClearanceUnavailable):
        raise
    except Exception as error:  # noqa: BLE001
        # One unreadable player must not end the night.
        if not args.quiet:
            print(f"  {player.key}: {type(error).__name__}: {error}", flush=True)
        return found, fresh, (unknown, slots), parties, discovered

    return found, fresh, (unknown, slots), parties, discovered


def _progress(index, remaining, client, new_matches, tracker, started) -> None:
    elapsed = (time.time() - started) / 60
    estimate = tracker.best_estimate()
    coverage = f" · ~{estimate:.0%} of recent days" if estimate else ""
    print(
        f"  {index:,} done, {remaining:,} queued · {new_matches:,} new matches · "
        f"{client.bytes / 1e6:,.0f} MB · {elapsed:.0f} min{coverage}",
        flush=True,
    )


def _god_id_lookup(provider) -> Dict[str, int]:
    """Whatever tracker.gg calls a god, onto our synthetic id.

    Built from the same index the wiki side uses, which resolves slugs, display
    names and the engine's `Gods.X` tokens — 100% of the god values observed
    across 26,444 sampled rows.
    """
    lookup: Dict[str, int] = {}
    for god in provider.gods.values():
        resolved = provider.god_by_name(god.name)
        if resolved is not None:
            lookup[god.name] = resolved.id
    return _IndexedLookup(provider, lookup)


class _IndexedLookup(dict):
    """A dict that falls back to the provider's fuzzy name index on a miss."""

    def __init__(self, provider, initial):
        super().__init__(initial)
        self.__provider = provider

    def get(self, key, default=0):  # noqa: A003
        if key in self:
            return self[key]
        god = self.__provider.god_by_name(key)
        value = god.id if god is not None else default
        self[key] = value
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budget", type=int, default=4000, help="maximum requests this run"
    )
    parser.add_argument(
        "--hours", type=float, default=5.0, help="wall-clock cap"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.5,
        help="seconds between requests; the measured-safe figure. Raising it is "
        "safe, lowering it is how one gets blocked.",
    )
    parser.add_argument(
        "--coverage-target",
        type=float,
        default=0.0,
        help="stop once recent days are estimated this covered, e.g. 0.8",
    )
    parser.add_argument("--flush-every", type=int, default=50_000)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="crawl and report, writing nothing",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--reset-clearance",
        action="store_true",
        help="clear a tripped backoff before crawling, for when whatever "
        "caused it has since been fixed",
    )
    args = parser.parse_args()

    if args.dry_run and args.budget > 200:
        # A dry run is for checking the shape of the output, not for pulling a
        # night's data and throwing it away.
        args.budget = min(args.budget, 50)
        print(f"Dry run: capping budget at {args.budget} requests.")

    return asyncio.run(crawl(args))


if __name__ == "__main__":
    sys.exit(main())
