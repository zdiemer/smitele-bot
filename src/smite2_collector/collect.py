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
from smite2 import cooldown as cooldown_module  # noqa: E402
from smite2 import egress as egress_module  # noqa: E402
from smite2 import last_run as last_run_module  # noqa: E402
from smite2.provider import CLEARANCE_FILE, Smite2Provider  # noqa: E402
from smite2.tracker_client import (  # noqa: E402
    DEFAULT_JITTER,
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


async def check_egress(state_dir: str) -> int:
    """Prove an egress works before anything depends on it.

    Resolves the address, mints one cookie through it, and issues a single
    request — printing what each stage saw. They must agree: the cookie is bound
    to the address that solved the challenge, so a mint and a crawl leaving from
    different places 403s everything.

    Exists so that evaluating a proxy is one command rather than a nightly that
    fails at 02:40. A pre-flagged address fails here, where it costs nothing,
    instead of costing a solve out of a budget of twelve.
    """
    configured = egress_module.proxy_url()
    identity = egress_module.identity(configured)
    print(f"Egress check · {identity}")

    address = await egress_module.observed_ip(configured)
    if address is None:
        print("  FAILED — no route to the internet through this egress.")
        return 1
    print(f"  address      {address}")

    cooldown = cooldown_module.Cooldown(
        os.path.join(state_dir, cooldown_module.FILE_NAME), egress=identity
    )
    standdown = cooldown.read()
    if standdown.active:
        # Reported rather than enforced: checking is how you find out whether a
        # ban has actually lifted, and one request is not what re-earns it.
        print(
            f"  stand-down   {cooldown_module.describe(standdown.remaining)} "
            f"remaining ({standdown.reason})"
        )

    manager = ClearanceManager(
        ClearanceStore(os.path.join(state_dir, CLEARANCE_FILE), egress=identity)
    )
    try:
        clearance = await manager.get()
    except ClearanceUnavailable as error:
        print(f"  FAILED — no clearance: {error}")
        return 1
    print(f"  minted at    {clearance.observed_ip or 'unknown'}")
    print(f"  user agent   {clearance.user_agent}")

    async with TrackerClient(
        manager, silent=True, proxy=configured, cooldown=cooldown
    ) as client:
        try:
            await client.leaderboard(LEADERBOARDS[0], take=1)
        except Exception as error:  # noqa: BLE001
            print(f"  FAILED — the API refused us: {error}")
            return 1
    print("  request      served")

    if clearance.observed_ip and clearance.observed_ip != address:
        print(
            f"\n  MISMATCH — minted at {clearance.observed_ip} but leaving from "
            f"{address}. Something is proxying one and not the other; check "
            "that HTTPS_PROXY is unset and that only SMITELE_EGRESS_PROXY is "
            "configuring this."
        )
        return 1

    print("\nUsable. Re-run in a few hours to confirm the exit is sticky.")
    return 0


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

    depth = (
        f" · up to {args.pages} pages/player"
        + (f" back to {args.horizon}" if args.horizon else "")
        if args.pages > 1
        else ""
    )
    print(
        f"Smite 2 crawl · budget {args.budget:,} requests · "
        f"{args.hours}h cap{depth}"
    )
    print(f"  corpus  {corpus_dir}")
    print(f"  state   {state_dir}")
    if args.dry_run:
        print("  DRY RUN — nothing will be written")

    cooldown = cooldown_module.Cooldown(
        os.path.join(state_dir, cooldown_module.FILE_NAME)
    )
    if args.reset_cooldown:
        print("  clearing the recorded stand-down before crawling")
        cooldown.clear()

    # Refuse to start inside a stand-down the last run was told to serve. A
    # crawl that fires into a live ban collects nothing and spends reputation
    # doing it, and the whole reason the deadline is on disk is so that this
    # check can exist.
    standdown = cooldown.read()
    if standdown.active:
        print(
            f"\nSTANDING DOWN — {cooldown_module.describe(standdown.remaining)} "
            f"left of a refusal recorded for {egress_module.identity()}.\n"
            f"  because: {standdown.reason}\n"
            "  Crawling now would only confirm it. Wait it out, move to another "
            "egress, or pass --reset-cooldown if the ban is known to be over."
        )
        # The most important run to record. Without this a night that refused
        # to start is indistinguishable from a night that has not come yet —
        # both leave the corpus untouched and say nothing about why.
        if not args.dry_run:
            last_run_module.write(
                state_dir,
                {
                    "started": started,
                    "elapsed_seconds": time.time() - started,
                    "exit_reason": "standdown",
                    "egress": egress_module.identity(),
                    "standdown": {
                        "until": standdown.until,
                        "reason": standdown.reason,
                        "remaining_seconds": standdown.remaining,
                    },
                },
            )
        return 3

    # The god index comes from the wiki, and without it every row would have
    # GodId 0 and be dropped by the aggregate. Worth failing loudly for.
    provider = Smite2Provider(silent=True)
    await provider.create()
    if not provider.gods:
        print("Could not load the god catalogue; refusing to crawl.", flush=True)
        if not args.dry_run:
            last_run_module.write(
                state_dir,
                {
                    "started": started,
                    "elapsed_seconds": time.time() - started,
                    "exit_reason": "no_gods",
                    "egress": egress_module.identity(),
                },
            )
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

    # Sampled once here and once at the end. Two requests across a run of
    # thousands, and the only way a rotating exit announces itself on a run that
    # happened to survive — where it would otherwise show up as a mystery.
    egress_at_start = await egress_module.observed_ip()
    if egress_at_start:
        print(f"  leaving from {egress_at_start} ({egress_module.identity()})")

    new_matches = 0
    unknown_items = 0
    item_slots = 0
    blocked = False
    # Bound out here rather than in the try below, because the run record is
    # written on the way out of a `seed()` that raised too — where the inner
    # bindings would never have happened.
    index = 0
    discovered_total = 0

    async with TrackerClient(
        manager, interval=args.interval, jitter=args.jitter, cooldown=cooldown
    ) as client:
        try:
            added = await seed(client, frontier, today, args.quiet)
            print(f"  seeded {added:,} new players from the leaderboards")

            # Budget is in requests; a player now costs up to --pages of
            # them, so the roster is that much shorter. It refills as the
            # snowball discovers people, so an underestimate costs nothing.
            pending = frontier.select(
                args.budget // args.pages, today, revisit=args.revisit
            )
            print(f"  {len(pending):,} players to start with\n")

            visited: Set[str] = set()

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
                            for p in frontier.select(
                                max(1, remaining // args.pages),
                                today,
                                revisit=args.revisit,
                            )
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
    egress_at_end = await egress_module.observed_ip() if egress_at_start else None
    if egress_at_end and egress_at_end != egress_at_start:
        print(
            f"  ADDRESS CHANGED — started at {egress_at_start}, finished at "
            f"{egress_at_end}. A clearance cookie is bound to the address that "
            "solved it, so a rotating exit cannot work here; use a sticky or "
            "static one."
        )
    if client.rate_limited:
        # Without this a run that was rate limited and recovered reads exactly
        # like a clean one, and the pace it ended on is the number that decides
        # what to configure next time.
        print(
            f"  {client.rate_limited} rate limit(s) · finished pacing at "
            f"{client.interval:.2f}s"
        )
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

    # Everything above, as fields. Written last so a run that died on the way
    # here leaves the previous night's record standing rather than a half one —
    # a stale report that says so beats a fresh report that is wrong.
    if not args.dry_run:
        last_run_module.write(
            state_dir,
            {
                "started": started,
                "elapsed_seconds": elapsed,
                "exit_reason": "blocked" if blocked else "ok",
                "egress": egress_module.identity(),
                "egress_changed": bool(
                    egress_at_end and egress_at_end != egress_at_start
                ),
                "requests": client.requests,
                "bytes": client.bytes,
                "budget": args.budget,
                "new_matches": new_matches,
                "rows_written": buffer.written,
                "matches_known": len(seen),
                "players_visited": index,
                "players_discovered": discovered_total,
                "rate_limited": client.rate_limited,
                "final_interval": client.interval,
                "unknown_items": unknown_items,
                "item_slots": item_slots,
                "frontier": frontier.counts(),
                "coverage": tracker.snapshot(),
                "coverage_estimate": tracker.best_estimate(),
            },
        )

    return 2 if blocked else 0


async def _visit(client, player, god_ids, seen, tracker, buffer, args):
    """Read a player's history and absorb what is new.

    One page by default — 25 matches, roughly the last three days — which is all
    a nightly run needs, since it ran yesterday too. `--pages` walks further
    back for a backfill, and is the only way to reach a day that happened before
    the collector existed.

    Returns matches seen, matches new, (unnameable items, item slots), the
    parties observed — which is how premade suppression learns who queues
    together — and every player named in those matches, which is the snowball.
    """
    found = fresh = unknown = slots = 0
    parties: Dict[str, Set[str]] = {}
    discovered: Set[Tuple[str, str]] = set()
    horizon = args.horizon

    try:
        for page in range(args.pages):
            on_page = 0
            oldest = None

            async for match in client.iter_matches(
                player.platform, player.handle, page
            ):
                on_page += 1
                found += 1
                match_id = (match.get("attributes") or {}).get("id")
                date = rows_module.match_date(match)
                if not match_id or not date:
                    continue
                oldest = min(oldest or date, date)

                tracker.observe(date, str(match_id), player.key)
                for party, members in frontier_module.parties_in(match).items():
                    parties.setdefault(party, set()).update(members)
                # Collected from every match, not only new ones: a match we
                # already have can still name a player we have never queried.
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

            # A page past the end of a history returns nothing rather than
            # erroring, which is what makes walking backwards terminable at all.
            if on_page == 0:
                break
            # Already older than the window being backfilled; deeper pages are
            # only older still.
            if horizon and oldest and oldest < horizon:
                break
            if client.requests >= args.budget:
                break
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
        "--jitter",
        type=float,
        default=DEFAULT_JITTER,
        help="fraction of the interval to add back at random, so requests do "
        "not arrive on a metronome. Only ever widens the gap — the interval "
        "stays a floor.",
    )
    parser.add_argument(
        "--coverage-target",
        type=float,
        default=0.0,
        help="stop once recent days are estimated this covered, e.g. 0.8",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="pages of history per player; 25 matches each, about three days. "
        "One is right for a nightly run, which only has to cover since the last "
        "one. Raise it to backfill days that predate the collector.",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=0,
        help="with --pages, stop walking back once a player's history reaches "
        "this far, so an inactive account is not paged to the beginning of time",
    )
    parser.add_argument(
        "--revisit",
        action="store_true",
        help="re-read players already queried today. Pointless for a nightly "
        "run, which would just refetch the same page, and necessary for a "
        "backfill, whose deeper pages have never been read.",
    )
    parser.add_argument("--flush-every", type=int, default=50_000)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="crawl and report, writing nothing",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--check-egress",
        action="store_true",
        help="resolve the outbound address, mint one cookie and issue one "
        "request through it, then exit. For validating a proxy before anything "
        "depends on it. Writes nothing and does not crawl.",
    )
    parser.add_argument(
        "--reset-cooldown",
        action="store_true",
        help="crawl even though a stand-down is recorded, for when the ban it "
        "describes is known to be over. Deliberately manual: a stand-down that "
        "clears itself early is not a stand-down.",
    )
    parser.add_argument(
        "--reset-clearance",
        action="store_true",
        help="clear a tripped backoff before crawling, for when whatever "
        "caused it has since been fixed",
    )
    args = parser.parse_args()

    if args.check_egress:
        return asyncio.run(check_egress(paths.game_model_dir(Game.SMITE_2)))

    args.pages = max(1, args.pages)
    args.horizon = (
        (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=args.since_days)
        ).strftime("%Y-%m-%d")
        if args.since_days
        else None
    )

    if args.dry_run and args.budget > 200:
        # A dry run is for checking the shape of the output, not for pulling a
        # night's data and throwing it away.
        args.budget = min(args.budget, 50)
        print(f"Dry run: capping budget at {args.budget} requests.")

    return asyncio.run(crawl(args))


if __name__ == "__main__":
    sys.exit(main())
