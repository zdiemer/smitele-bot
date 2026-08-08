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
import signal
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

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
    API_HOST,
    DEFAULT_JITTER,
    GAME_SLUG,
    IMPERSONATE,
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
    """Prove the configured egresses work before anything depends on them.

    Walks every exit in `SMITELE_EGRESS_PROXY`, in the preference order it was
    written in, and reports one verdict each. Screening a list is worth doing
    precisely because the state is bucketed per egress: the clearance cookie,
    the twelve-solves-a-day breaker and the WAF stand-down are all keyed on
    `identity()`, so a candidate that fails burns its own budget and arms its
    own backoff. Trying a bad one cannot cost the address you depend on.

    A failure never stops the walk — the point is to find which of several work,
    and the first entry being dead is the ordinary case this exists to catch.
    """
    exits = egress_module.proxy_urls() or [None]
    if len(exits) > 1:
        print(f"Screening {len(exits)} exits, in preference order.\n")

    usable: List[str] = []
    for candidate in exits:
        if await _check_one(state_dir, candidate) == 0:
            usable.append(candidate or "direct")

    if len(exits) > 1:
        print(
            f"\n{len(usable)} of {len(exits)} usable"
            + (f": {', '.join(usable)}" if usable else "")
        )
        if usable:
            print(
                "  Re-run in a few hours: an exit that works once still has to "
                "hold one address for the life of a cookie."
            )
    return 0 if usable else 1


async def _serves_unauthenticated(configured: Optional[str]) -> bool:
    """Whether this exit can read the API carrying no cookie at all.

    Cheap, and it answers the only question screening really asks. Kept
    deliberately narrow: one small leaderboard call, no retries, every failure
    read as "no" — a false negative costs a mint, a false positive would report
    a broken exit as usable.
    """
    try:
        from curl_cffi import requests as curl_requests  # noqa: PLC0415
    except ImportError:
        return False

    kwargs = {"impersonate": IMPERSONATE, "trust_env": False}
    if configured:
        kwargs["proxy"] = configured
    try:
        async with curl_requests.AsyncSession(**kwargs) as session:
            response = await session.get(
                f"{API_HOST}/api/v1/{GAME_SLUG}/standard/leaderboards",
                params={
                    "type": "stats",
                    "board": LEADERBOARDS[0],
                    "platform": "steam",
                    # Not optional — omitting `skip` 404s.
                    "skip": 0,
                    "take": 1,
                },
                timeout=30,
            )
            return response.status_code == 200 and b'"data"' in (response.content or b"")
    except Exception:  # noqa: BLE001
        return False


async def _check_one(state_dir: str, configured: Optional[str]) -> int:
    """One exit, end to end.

    Resolves the address, mints one cookie through it, and issues a single
    request — printing what each stage saw. They must agree: the cookie is bound
    to the address that solved the challenge, so a mint and a crawl leaving from
    different places 403s everything.

    Exists so that evaluating a proxy is one command rather than a nightly that
    fails at 02:40. A pre-flagged address fails here, where it costs a solve out
    of *its own* budget of twelve rather than out of the one the crawl depends on.
    """
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

    # Ask without a cookie before spending one. Measured 2026-08-07: the API
    # answers 200 to an unauthenticated request as long as the TLS handshake is
    # Firefox's — it is the *fingerprint* that is checked on these routes, not
    # clearance. So a mint is a fallback for exits that need it, not the price
    # of finding out, and screening a list of candidates costs seconds rather
    # than two minutes each.
    if await _serves_unauthenticated(configured):
        print("  request      served without a cookie")
        print("  USABLE")
        return 0

    manager = ClearanceManager(
        ClearanceStore(os.path.join(state_dir, CLEARANCE_FILE), egress=identity)
    )
    print("  clearance    needed here; minting")
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

    print("  USABLE")
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

    # Which stop condition fired. "ok" used to conflate spending the budget,
    # running out of wall clock and hitting the coverage target; under a model
    # where the run is *meant* to last until the deadline, those three want
    # telling apart — finishing on "budget" now means the ceiling was too low,
    # not that the night went well.
    exit_reason = "ok"
    terminated = False
    new_matches = 0
    unknown_items = 0
    item_slots = 0
    blocked = False
    # Bound out here rather than in the try below, because the run record is
    # written on the way out of a `seed()` that raised too — where the inner
    # bindings would never have happened.
    index = 0
    discovered_total = 0
    last_checkpoint = time.time()

    def checkpoint() -> None:
        """Put everything collected so far beyond the reach of a kill.

        A run used to write its indexes once, at the end, which was survivable
        when it lasted an hour. It now waits out quota resets and can run most
        of a day, so an eviction or an `activeDeadlineSeconds` kill would throw
        away a whole night of `seen_matches` and `frontier` updates — and the
        next run would refetch every one of those matches and count them new.

        Also files an interim run record, so a crawl that is still going says so
        instead of leaving yesterday's report standing all day.
        """
        nonlocal last_checkpoint
        last_checkpoint = time.time()
        if args.dry_run:
            return
        buffer.flush()
        seen.save()
        frontier.save()
        last_run_module.write(
            state_dir,
            {
                "started": started,
                "elapsed_seconds": time.time() - started,
                "exit_reason": "running",
                "egress": egress_module.identity(),
                "requests": client.requests,
                "bytes": client.bytes,
                "budget": args.budget,
                "new_matches": new_matches,
                "rows_written": buffer.written,
                "players_visited": index,
                "rate_limited": client.rate_limited,
                # getattr: the record is a report, and a missing field must not
                # be able to fail the run that was trying to describe itself.
                "rate_limits": getattr(client, "rate_limit_events", []),
            },
        )

    # players/found/fresh per selection tier. The question it exists to settle:
    # `select` puts never-visited players first, and those were discovered from
    # the matches just read, so their pages should overlap what we already have.
    # If `fresh` yields materially worse than `stale`, the ordering is wrong —
    # but that is a claim about this corpus, not one to guess at.
    by_tier: Dict[str, Dict[str, int]] = {}

    async with TrackerClient(
        manager,
        interval=args.interval,
        jitter=args.jitter,
        cooldown=cooldown,
        # What turns a refusal from the end of the night into a pause. The
        # client cannot know how long the run has; giving it a way to ask is
        # what lets it decide that an hour's wait is affordable.
        time_left=lambda: deadline - time.time(),
        checkpoint=checkpoint,
        max_wait=args.max_wait_minutes * 60,
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
                    exit_reason = "deadline"
                    break
                if client.requests >= args.budget:
                    print(f"\nRequest budget spent after {index} players.")
                    exit_reason = "budget"
                    break

                player = pending.pop(0)
                if player.key in visited:
                    continue
                visited.add(player.key)
                index += 1

                found, fresh, counts, parties, discovered = await _visit(
                    client, player, god_ids, seen, tracker, buffer, args
                )
                tier = by_tier.setdefault(
                    player.tier or "unknown",
                    {"players": 0, "found": 0, "fresh": 0},
                )
                tier["players"] += 1
                tier["found"] += found
                tier["fresh"] += fresh

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

                if time.time() - last_checkpoint > args.checkpoint_minutes * 60:
                    checkpoint()

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
                        exit_reason = "coverage"
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
        except asyncio.CancelledError:
            # SIGTERM, almost always: the kubelet reclaiming the pod. Caught so
            # that everything below still runs, because the alternative is
            # losing a whole night's indexes to a signal we were told about.
            print("\nSTOPPED — asked to shut down; saving what we have.")
            exit_reason = "terminated"
            terminated = True

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
    if by_tier:
        # The number that decides whether `select`'s ordering is right. Printed
        # rather than only filed, because it is read by whoever is deciding.
        parts = []
        for name in ("fresh", "stale", "revivable", "unknown"):
            counts = by_tier.get(name)
            if not counts or not counts["found"]:
                continue
            share = counts["fresh"] / counts["found"]
            parts.append(
                f"{name} {counts['players']:,}p {share:.0%} new"
            )
        if parts:
            print(f"  yield by tier · {' · '.join(parts)}")
    if frontier.suppressed:
        print(f"  {frontier.suppressed:,} players held back as premades")
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
                "exit_reason": "blocked" if blocked else exit_reason,
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
                # One record per refusal: how much was served before it, over
                # how long, and what was asked for. This is what distinguishes
                # a quota from a rate from a volume cap, and the answer decides
                # how the next round of pacing work is spent.
                "rate_limits": getattr(client, "rate_limit_events", []),
                "final_interval": client.interval,
                "unknown_items": unknown_items,
                "item_slots": item_slots,
                "frontier": frontier.counts(),
                "yield_by_tier": by_tier,
                "party_suppressed": frontier.suppressed,
                "coverage": tracker.snapshot(),
                "coverage_estimate": tracker.best_estimate(),
            },
        )

    if blocked:
        return 2
    # A run we asked to stop did not fail, but it did not finish either, and a 0
    # would tell the CronJob's history it had.
    return 4 if terminated else 0


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
        "--max-wait-minutes",
        type=float,
        default=120.0,
        help="longest single pause to serve when the site asks for one. The "
        "measured quota resets after an hour, so anything under this is a "
        "wait; beyond it, treat the refusal as a stop.",
    )
    parser.add_argument(
        "--checkpoint-minutes",
        type=float,
        default=10.0,
        help="how often to put the indexes on disk. A long run that is killed "
        "between checkpoints refetches everything since the last one.",
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

    return asyncio.run(_run(args))


async def _run(args) -> int:
    """`crawl`, with SIGTERM turned into something the crawl can act on.

    Without this the kubelet's kill lands as an unhandled signal and the process
    dies where it stands — skipping the flush, the index writes and the run
    record. That was survivable when a run lasted an hour and wrote everything
    at the end anyway; it is not when a run waits out quota resets for most of a
    day. Cancelling the task instead unwinds it through the same path a block
    takes, which already knows how to save on the way out.

    Needs `terminationGracePeriodSeconds` on the pod to be worth anything: the
    handler only gets as long as the kubelet waits before SIGKILL.
    """
    task = asyncio.ensure_future(crawl(args))
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, task.cancel)
        except (NotImplementedError, RuntimeError):
            # Windows, or a loop that will not take handlers. The crawl still
            # runs; it just dies abruptly, which is where we started.
            pass
    try:
        return await task
    except asyncio.CancelledError:
        # `crawl` catches this and saves. Reaching here means it was cancelled
        # somewhere that could not, so there is nothing left to write.
        print("Shut down before the crawl could save.", flush=True)
        return 4


if __name__ == "__main__":
    sys.exit(main())
