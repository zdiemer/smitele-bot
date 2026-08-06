#!/usr/bin/env python3
"""Phase 0b: measure tracker.gg before committing to a collector design.

The feasibility record in the README rests on an *estimated* concurrency figure
and says so — it recommends measuring the real match production rate before
anyone commits to its tables. This does that, and settles the other unknowns the
collector design depends on:

  * how many matches a day the game actually produces, per mode;
  * the mode and platform vocabulary, since all of them are meant to be supported;
  * the item-slug to wiki-page join rate, weighted by occurrence — this is the
    go/no-go, because a build made of items we cannot name is not a build;
  * whether any per-match rating exists, which decides `/rank` and `high_mmr`;
  * how items are really laid out, since a mis-slotted build is silently wrong;
  * how much of the player base is premades, which is both the largest source of
    wasted crawl budget and what breaks the coverage estimator;
  * how long a `cf_clearance` cookie lasts, which decides the refresh policy.

Two modes, because one of them is a long wait:

    python scripts/probe_tracker.py --players 200
    python scripts/probe_tracker.py --lifetime      # poll until the cookie dies

Nothing is written to the corpus. Pacing defaults to the 1.5 s the original
probe sustained without drawing an error; this is an undocumented endpoint
behind a WAF, so do not raise it to see what happens.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "HirezAPI"
    ),
)

from smite2.clearance import ClearanceManager, ClearanceStore  # noqa: E402
from smite2.ids import NameIndex, assert_no_collisions  # noqa: E402
from smite2.tracker_client import (  # noqa: E402
    LEADERBOARDS,
    TrackerBlocked,
    TrackerClient,
    TrackerServerError,
    leaderboard_players,
)
from smite2.wiki_client import WikiClient  # noqa: E402

# Items that fill one of the six core build slots. Selecting on this rather than
# on position is not a preference: talents are interleaved *into* positions 3-8,
# so a position-based mapping silently mis-slots them as items.
CORE_EQUIPMENT = ("item-passive", "item-active")

def overview_segments(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [s for s in (match.get("segments") or []) if s.get("type") == "overview"]


def core_items(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = [
        i
        for i in (metadata.get("items") or [])
        if i.get("equipmentType") in CORE_EQUIPMENT
    ]
    return sorted(items, key=lambda i: int(i.get("position", 0)))


def of_type(metadata: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    for item in metadata.get("items") or []:
        if item.get("equipmentType") == kind:
            return item
    return None


class Findings:
    def __init__(self) -> None:
        self.matches: Dict[str, Dict[str, Any]] = {}
        self.modes: collections.Counter = collections.Counter()
        self.platforms: collections.Counter = collections.Counter()
        self.regions: collections.Counter = collections.Counter()
        self.roles: collections.Counter = collections.Counter()
        self.equipment: collections.Counter = collections.Counter()
        self.position_equipment: collections.Counter = collections.Counter()
        self.core_counts: collections.Counter = collections.Counter()
        self.core_position_sets: collections.Counter = collections.Counter()
        self.item_uses: collections.Counter = collections.Counter()
        self.god_uses: collections.Counter = collections.Counter()
        self.stat_keys: collections.Counter = collections.Counter()
        self.talents_in_core_range = 0
        self.players_seen: Set[Tuple[str, str]] = set()
        self.pages = 0
        # match id -> the set of player keys we found it through, for
        # capture-recapture and for measuring how often a match repeats.
        self.discovered_by: Dict[str, Set[str]] = collections.defaultdict(set)

    def absorb(self, match: Dict[str, Any], found_via: str) -> None:
        attributes = match.get("attributes") or {}
        metadata = match.get("metadata") or {}
        match_id = attributes.get("id")
        if not match_id:
            return

        self.discovered_by[match_id].add(found_via)
        if match_id in self.matches:
            return

        self.matches[match_id] = {
            "timestamp": metadata.get("timestamp"),
            "mode": attributes.get("gamemode"),
            "ranked": metadata.get("isRanked"),
            "duration": metadata.get("duration"),
            "parties": self.__party_units(match),
        }
        self.modes[
            f"{attributes.get('gamemode')}{' (ranked)' if metadata.get('isRanked') else ''}"
        ] += 1
        self.regions[attributes.get("region")] += 1

        for segment in overview_segments(match):
            attrs = segment.get("attributes") or {}
            meta = segment.get("metadata") or {}
            platform = attrs.get("platformSlug")
            identifier = attrs.get("platformUserIdentifier")
            if platform and identifier:
                self.platforms[platform] += 1
                self.players_seen.add((str(platform), str(identifier)))

            self.roles[(meta.get("assignedRole") or {}).get("key")] += 1
            self.god_uses[meta.get("god")] += 1
            self.stat_keys.update((segment.get("stats") or {}).keys())

            entries = meta.get("items") or []
            for entry in entries:
                kind = entry.get("equipmentType")
                position = int(entry.get("position", 0))
                self.equipment[kind] += 1
                self.position_equipment[(position, kind)] += 1
                if kind == "talent" and 3 <= position <= 8:
                    self.talents_in_core_range += 1

            core = core_items(meta)
            self.core_counts[len(core)] += 1
            self.core_position_sets[
                tuple(int(i.get("position", 0)) for i in core)
            ] += 1
            for entry in core:
                self.item_uses[entry.get("id")] += 1
            for kind in ("starter", "relic"):
                entry = of_type(meta, kind)
                if entry is not None:
                    self.item_uses[entry.get("id")] += 1

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as out:
            json.dump(
                {
                    "matches": self.matches,
                    "discovered_by": {k: sorted(v) for k, v in self.discovered_by.items()},
                    "item_uses": dict(self.item_uses),
                    "god_uses": {str(k): v for k, v in self.god_uses.items()},
                    "modes": dict(self.modes),
                    "platforms": dict(self.platforms),
                    "regions": dict(self.regions),
                    "roles": {str(k): v for k, v in self.roles.items()},
                    "equipment": {str(k): v for k, v in self.equipment.items()},
                    "position_equipment": {
                        f"{p}|{e}": n for (p, e), n in self.position_equipment.items()
                    },
                    "core_counts": {str(k): v for k, v in self.core_counts.items()},
                    "core_position_sets": {
                        ",".join(str(p) for p in k): v
                        for k, v in self.core_position_sets.items()
                    },
                    "stat_keys": sorted(self.stat_keys),
                    "talents_in_core_range": self.talents_in_core_range,
                    "pages": self.pages,
                },
                out,
            )

    @classmethod
    def load(cls, path: str) -> "Findings":
        """Rebuild from a dump, so the reporting can be reworked without
        re-crawling. Every number below came from requests already made."""
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        findings = cls()
        findings.matches = raw["matches"]
        findings.discovered_by = collections.defaultdict(
            set, {k: set(v) for k, v in raw.get("discovered_by", {}).items()}
        )
        findings.item_uses = collections.Counter(raw["item_uses"])
        findings.god_uses = collections.Counter(raw["god_uses"])
        findings.modes = collections.Counter(raw["modes"])
        findings.platforms = collections.Counter(raw["platforms"])
        findings.regions = collections.Counter(raw.get("regions", {}))
        findings.roles = collections.Counter(raw.get("roles", {}))
        findings.equipment = collections.Counter(raw.get("equipment", {}))
        findings.position_equipment = collections.Counter(
            {
                (int(k.split("|")[0]), k.split("|")[1]): v
                for k, v in raw.get("position_equipment", {}).items()
            }
        )
        findings.core_counts = collections.Counter(
            {int(k): v for k, v in raw.get("core_counts", {}).items()}
        )
        findings.core_position_sets = collections.Counter(
            {
                tuple(int(p) for p in k.split(",") if p): v
                for k, v in raw.get("core_position_sets", {}).items()
            }
        )
        findings.stat_keys = collections.Counter(raw.get("stat_keys", []))
        findings.talents_in_core_range = raw.get("talents_in_core_range", 0)
        findings.pages = raw.get("pages", 0)
        return findings

    @staticmethod
    def __party_units(match: Dict[str, Any]) -> int:
        """Distinct *queryable* units in a match.

        Ten players are not ten independent draws: querying either half of a duo
        returns the same matches, so the crawl's effective yield is set by the
        number of parties, not the number of players. `partyId` measures that
        directly, where the README could only estimate it.
        """
        solos = 0
        parties: Set[str] = set()
        for segment in overview_segments(match):
            party = (segment.get("metadata") or {}).get("partyId")
            if party:
                parties.add(str(party))
            else:
                solos += 1
        return solos + len(parties)


async def crawl(
    client: TrackerClient, findings: Findings, budget: int, seeds: List[Tuple[str, str]]
) -> None:
    """Leaderboard seeds first, then snowball into players seen in their matches."""
    queried: Set[Tuple[str, str]] = set()
    frontier: List[Tuple[str, str]] = list(seeds)
    started = time.time()

    while frontier and findings.pages < budget:
        player = frontier.pop(0)
        if player in queried:
            continue
        queried.add(player)
        key = f"{player[0]}:{player[1]}"

        try:
            count = 0
            async for match in client.iter_matches(player[0], player[1], 0):
                findings.absorb(match, key)
                count += 1
        except TrackerBlocked:
            raise
        except Exception as error:  # noqa: BLE001
            print(f"  {key}: {type(error).__name__}: {error}", flush=True)
            continue

        findings.pages += 1
        if findings.pages % 10 == 0:
            elapsed = time.time() - started
            print(
                f"  {findings.pages}/{budget} pages · {len(findings.matches)} matches"
                f" · {client.bytes / 1e6:.0f} MB · {elapsed / 60:.1f} min",
                flush=True,
            )

        # Snowball: everyone seen in those matches becomes queryable. Shuffling
        # is unnecessary — the frontier is already ordered by discovery, which
        # spreads across matches rather than concentrating on one lobby.
        for candidate in findings.players_seen:
            if candidate not in queried and len(frontier) < budget * 4:
                frontier.append(candidate)


def report_production_rate(findings: Findings) -> None:
    by_day: Dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    for record in findings.matches.values():
        day = (record.get("timestamp") or "")[:10]
        if day:
            by_day[day][record.get("mode") or "?"] += 1

    print("\n=== matches observed per day (sampled, not total) ===")
    for day in sorted(by_day):
        total = sum(by_day[day].values())
        modes = ", ".join(f"{m}:{n}" for m, n in by_day[day].most_common(6))
        print(f"  {day}  {total:>5}   {modes}")

    spans = [r["timestamp"][:10] for r in findings.matches.values() if r.get("timestamp")]
    if spans:
        print(f"  span: {min(spans)} … {max(spans)}  ({len(set(spans))} distinct days)")

    repeats = collections.Counter(len(v) for v in findings.discovered_by.values())
    print("\n=== how many queried players surfaced each match ===")
    for times, count in sorted(repeats.items()):
        print(f"  seen via {times:>2} player(s): {count} matches")
    total_sightings = sum(t * c for t, c in repeats.items())
    distinct = sum(repeats.values())
    if distinct:
        print(
            f"  redundancy: {total_sightings / distinct:.2f} sightings per distinct match"
        )

    parties = [r["parties"] for r in findings.matches.values() if r.get("parties")]
    if parties:
        print(
            f"\n=== premades ===\n  mean distinct query-units per match: "
            f"{sum(parties) / len(parties):.2f} of 10 players"
        )
        print(
            "  (the README estimated ~6; this is measured from partyId rather "
            "than inferred from match overlap)"
        )


async def build_indexes() -> Tuple[NameIndex, NameIndex]:
    """Wiki-side name indexes for gods and items.

    Gods are indexed from the *page list* first, not `Data:Gods.json`. That is
    not a preference: the JSON has 88 records but is missing Xing Tian entirely
    while carrying Bastet twice, so it is not authoritative for the roster.
    """
    async with WikiClient(silent=True) as wiki:
        item_rows = await wiki.bucket(
            'bucket("item_infobox").select("page_name").limit(1000).run()'
        )
        god_rows = await wiki.bucket(
            'bucket("god_infoboxs2").select("page_name").limit(1000).run()'
        )
        gods_page = await wiki.query_pages(["Data:Gods.json"])

    items = NameIndex()
    for row in item_rows:
        if row.get("page_name"):
            items.add(str(row["page_name"]))

    gods = NameIndex()
    for row in god_rows:
        if row.get("page_name"):
            gods.add(str(row["page_name"]))

    records = json.loads(gods_page["Data:Gods.json"]["content"])
    if isinstance(records, dict):
        records = records.get("json") or records.get("data") or []
    for record in records:
        name = str(record.get("name") or "")
        if not name:
            continue
        tags = [t for t in (record.get("characterTags") or []) if str(t).startswith("Gods.")]
        gods.add(name, record.get("slug"), *tags)

    return gods, items


async def report_join_rate(findings: Findings) -> bool:
    """Item and god slugs against the wiki. This is the go/no-go."""
    gods, items = await build_indexes()

    print("\n=== item slug → wiki page ===")
    matched = sum(n for slug, n in findings.item_uses.items() if slug in items)
    total = sum(findings.item_uses.values())
    missing = [s for s in findings.item_uses if s not in items]
    rate = 100.0 * matched / total if total else 0.0
    print(f"  by occurrence: {matched}/{total} = {rate:.2f}%")
    print(
        f"  by distinct slug: "
        f"{len(findings.item_uses) - len(missing)}/{len(findings.item_uses)}"
    )
    if missing:
        print("  unmatched, by frequency:")
        for slug in sorted(missing, key=lambda s: -findings.item_uses[s])[:15]:
            share = 100.0 * findings.item_uses[slug] / total if total else 0.0
            print(f"    {findings.item_uses[slug]:>6}  {share:5.2f}%  {slug}")

    print("\n=== god → wiki ===")
    god_matched = sum(n for slug, n in findings.god_uses.items() if slug in gods)
    god_total = sum(findings.god_uses.values())
    god_missing = [s for s in findings.god_uses if s not in gods]
    god_rate = 100.0 * god_matched / god_total if god_total else 0.0
    print(f"  by occurrence: {god_matched}/{god_total} = {god_rate:.2f}%")
    print(f"  by distinct: {len(findings.god_uses) - len(god_missing)}/{len(findings.god_uses)}")
    if god_missing:
        print(f"  unmatched: {sorted(str(s) for s in god_missing)[:20]}")

    # A 32-bit hash over a few hundred slugs is safe, but verified rather than
    # assumed — a collision would surface as two items sharing a build hash.
    try:
        assert_no_collisions("item", findings.item_uses.keys())
        assert_no_collisions("god", (str(g) for g in findings.god_uses))
        print("\n  no id collisions over every observed slug")
    except ValueError as error:
        print(f"\n  ID COLLISION: {error}")
        return False

    ok = rate >= 98.0 and god_rate >= 99.0
    print(
        "\n  GO — the corpus can be named against the wiki."
        if ok
        else "\n  NO-GO — too much of the catalogue is unnameable; fix the join first."
    )
    return ok


def report_coverage(findings: Findings) -> None:
    """Capture-recapture on the sample, split by which player surfaced a match.

    Chapman-corrected Lincoln-Petersen, because the recapture count is small.
    The independence assumption is violated by premades — which is exactly what
    partyId lets us see — and where it fails the estimate is biased low, so the
    resulting coverage figure is an upper bound.
    """
    print("\n=== coverage, by capture-recapture ===")
    by_day: Dict[str, Dict[str, Set[str]]] = collections.defaultdict(
        lambda: {"a": set(), "b": set()}
    )
    for match_id, finders in findings.discovered_by.items():
        record = findings.matches.get(match_id)
        day = (record or {}).get("timestamp", "")[:10]
        if not day:
            continue
        for finder in finders:
            half = "a" if hash_half(finder) else "b"
            by_day[day][half].add(match_id)

    rows = []
    for day, halves in sorted(by_day.items()):
        first, second = halves["a"], halves["b"]
        both = first & second
        n1, n2, m = len(first), len(second), len(both)
        if n1 < 5 or n2 < 5:
            continue
        estimate = (n1 + 1) * (n2 + 1) / (m + 1) - 1
        observed = len(first | second)
        rows.append((day, observed, n1, n2, m, estimate, 100.0 * observed / estimate))

    if not rows:
        print("  too few recaptures to estimate — the sample is far from saturation")
        return

    print(f"  {'day':<12}{'seen':>7}{'halfA':>7}{'halfB':>7}{'both':>6}{'est. total':>12}{'coverage':>10}")
    for day, observed, n1, n2, m, estimate, coverage in rows[-10:]:
        print(
            f"  {day:<12}{observed:>7}{n1:>7}{n2:>7}{m:>6}{estimate:>12.0f}{coverage:>9.1f}%"
        )
    print(
        "  Estimates are an upper bound on coverage: premades break the\n"
        "  independence the estimator assumes, which biases the total low."
    )


def hash_half(value: str) -> bool:
    import hashlib  # noqa: PLC0415

    return hashlib.blake2b(value.encode(), digest_size=2).digest()[0] % 2 == 0


def report_layout(findings: Findings) -> None:
    print("\n=== item layout ===")
    print(f"  equipmentType: {dict(findings.equipment)}")
    print("  position → equipmentType:")
    for (position, kind), count in sorted(findings.position_equipment.items()):
        print(f"    {position:>2}  {kind:<14} {count}")
    print(
        f"\n  talents at positions 3-8: {findings.talents_in_core_range}"
        "  ← each one a slot a position-based mapping would corrupt"
    )
    print("  core items per player (selected by equipmentType):")
    for count, players in sorted(findings.core_counts.items()):
        print(f"    {count} items  {players}")
    full = findings.core_counts.get(6, 0)
    everyone = sum(findings.core_counts.values())
    if everyone:
        print(f"  full six-item builds: {full}/{everyone} = {100.0 * full / everyone:.1f}%")

    print("\n  core position sets, top 10:")
    for combo, count in findings.core_position_sets.most_common(10):
        print(f"    {list(combo)}  {count}")


def report_rank(findings: Findings) -> None:
    rankish = sorted(
        k
        for k in findings.stat_keys
        if any(w in k.lower() for w in ("rank", "rating", "mmr", "elo", "tier", "skill"))
    )
    print("\n=== rating ===")
    print(f"  {len(findings.stat_keys)} distinct per-player stats")
    print(f"  rank/rating-like: {rankish or 'NONE'}")
    if not rankish:
        print(
            "  → no per-match rating. /build's high_mmr must be disabled for Smite 2,\n"
            "    and HighMmr is constant False in the aggregate."
        )


async def measure_lifetime(manager: ClearanceManager, interval: int) -> None:
    """Poll a cheap endpoint until the cookie stops working.

    The ~30 minute figure everyone quotes is a Cloudflare default, not an
    observation of this site. Whether any pre-warming is worth doing depends
    entirely on the real number.
    """
    async with TrackerClient(manager, interval=1.5) as client:
        clearance = await manager.get()
        minted = clearance.issued_at
        print(f"cookie minted, polling every {interval}s until it 403s")
        while True:
            age = (time.time() - minted) / 60
            try:
                await client.leaderboard("Wins", take=1)
                print(f"  {age:6.1f} min  ok", flush=True)
            except TrackerBlocked as error:
                print(f"  {age:6.1f} min  DEAD — {error}")
                print(f"\ncf_clearance lifetime: ~{age:.0f} minutes")
                return
            await asyncio.sleep(interval)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--players", type=int, default=200, help="pages to fetch")
    parser.add_argument("--interval", type=float, default=1.5, help="seconds between requests")
    parser.add_argument("--store", default=os.path.expanduser("~/.cache/smitele/clearance.json"))
    parser.add_argument("--lifetime", action="store_true", help="measure cookie lifetime instead")
    parser.add_argument("--poll", type=int, default=120, help="lifetime poll interval")
    parser.add_argument("--json", help="write raw findings here")
    parser.add_argument(
        "--from-json", help="re-report an earlier run without crawling again"
    )
    args = parser.parse_args()

    if args.from_json:
        return await report(Findings.load(args.from_json))

    manager = ClearanceManager(ClearanceStore(args.store))

    if args.lifetime:
        await measure_lifetime(manager, args.poll)
        return 0

    findings = Findings()
    started = time.time()

    async with TrackerClient(manager, interval=args.interval) as client:
        seeds: List[Tuple[str, str]] = []
        for board in LEADERBOARDS:
            try:
                entries = await client.leaderboard(board)
            except TrackerServerError as error:
                # One dead board is not a dead run; the others still seed.
                print(f"  board {board}: {error}")
                continue
            seeds.extend(leaderboard_players(entries))
            print(f"  board {board}: {len(entries)} entries")
        unique_seeds = list(dict.fromkeys(seeds))
        print(
            f"seeded {len(unique_seeds)} distinct players from "
            f"{len(LEADERBOARDS)} leaderboards ({len(seeds)} entries)"
        )

        try:
            await crawl(client, findings, args.players, unique_seeds)
        except TrackerBlocked as error:
            print(f"\nSTOPPED: {error}")

    elapsed = time.time() - started
    print(
        f"\n=== {findings.pages} pages · {client.requests} requests · "
        f"{client.bytes / 1e6:.0f} MB · {elapsed / 60:.1f} min ==="
    )
    if findings.pages:
        print(
            f"  {client.bytes / 1e6 / findings.pages:.2f} MB and "
            f"{elapsed / findings.pages:.2f} s per page"
        )
    print(f"  {len(findings.matches)} distinct matches, {len(findings.players_seen)} players seen")

    print("\n=== vocabulary ===")
    print(f"  modes:     {dict(findings.modes)}")
    print(f"  platforms: {dict(findings.platforms)}")
    print(f"  regions:   {dict(findings.regions)}")
    print(f"  roles:     {dict(findings.roles)}")

    if args.json:
        findings.dump(args.json)
        print(f"wrote {args.json}")

    return await report(findings)


async def report(findings: Findings) -> int:
    report_production_rate(findings)
    report_coverage(findings)
    report_layout(findings)
    report_rank(findings)
    ok = await report_join_rate(findings)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
