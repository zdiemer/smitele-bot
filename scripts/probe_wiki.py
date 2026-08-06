#!/usr/bin/env python3
"""Phase 0a: can wiki.smite2.com actually feed the static-data layer?

Everything Smite 2 support needs that tracker.gg does not publish comes from
this wiki, so before any of it is built the question is whether the pages parse
— all of them, not the three that were read by hand. This ingests every god and
every item and reports what fraction yield each field the domain model requires.

The bar is ~99%. Below that the gap is not a handful of stragglers to special
case, it is a parser that does not understand the page format, and the answer is
to fix the parser rather than to build on top of it.

Nothing is written and nothing is cached. Run it directly:

    python scripts/probe_wiki.py [--json report.json]
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "HirezAPI")
)

from smite2 import wikitext  # noqa: E402
from smite2.wiki_client import WikiClient  # noqa: E402

GODS_DATA_PAGE = "Data:Gods.json"

# The stat curves are published per level; a god missing levels is a god whose
# `get_stat_at_level` would read off the end of the array.
EXPECTED_LEVELS = 20

# Basic Attack, Passive, and four abilities — the floor, not the count. Stance
# and transform gods publish a further set per alternate form: Artio and Cu
# Chulainn 10, Mordred 8, Yemoja 7, Merlin 14 across three stances.
MIN_ABILITIES = 6

# The slot vocabulary, which is looser than it first appears. A rigid
# Basic Attack/Passive/1st/2nd/3rd/Ultimate sequence holds for 86 of 88 gods and
# is wrong for the other two: Mordred publishes two Passives and two Ultimates,
# and Yemoja has a "1st Ability (Alt)". So the invariant is that every slot is
# drawn from this set once an optional parenthetical is stripped, the list opens
# with the basic attack, and a passive and an ultimate are present.
KNOWN_SLOTS = (
    "Basic Attack",
    "Passive",
    "1st Ability",
    "2nd Ability",
    "3rd Ability",
    "Ultimate",
)

_SLOT_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def base_slot(slot: Optional[str]) -> str:
    return _SLOT_SUFFIX.sub("", (slot or "").strip())

REQUIRED_GOD_FIELDS = ("slug", "name", "title", "pantheon", "primaryDamageType")

# MaxMana is deliberately absent: manaless gods have no mana curve, and that is
# data rather than a gap. It is reported below instead of scored.
REQUIRED_STATS = (
    "MaxHealth",
    "PhysicalProtection",
    "MagicalProtection",
    "MovementSpeed",
)

# Only these can occupy one of the six core build slots, so only these need a
# tier and stats. Relics, consumables, curios and god-specific items never do.
CORE_ITEM_TYPES = ("Offensive", "Defensive", "Hybrid")


class Tally:
    """Counts with the examples attached, because a bare '3 items failed' sends
    you back to the wiki to find out which."""

    def __init__(self, total: int = 0):
        self.total = total
        self.counts: Dict[str, int] = collections.Counter()
        self.misses: Dict[str, List[str]] = collections.defaultdict(list)
        self.unscored: set = set()

    def check(self, name: str, ok: bool, subject: str, scored: bool = True) -> None:
        """Record one observation. `scored=False` reports the rate without
        holding it to the 99% bar — for fields that are optional by design, like
        a passive on an item that is only stats."""
        if not scored:
            self.unscored.add(name)
        if ok:
            self.counts[name] += 1
        elif len(self.misses[name]) < 8:
            self.misses[name].append(subject)
        else:
            self.misses[name].append("…")

    def report(self, title: str) -> bool:
        print(f"\n{title}  (n={self.total})")
        clean = True
        for name in sorted(self.counts.keys() | self.misses.keys()):
            got = self.counts.get(name, 0)
            pct = 100.0 * got / self.total if self.total else 0.0
            if name in self.unscored:
                flag = "--  "
            elif pct >= 99.0:
                flag = "ok  "
            else:
                flag = "FAIL"
                clean = False
            print(f"  [{flag}] {name:<34} {got:>4}/{self.total:<4} {pct:6.1f}%")
            missed = [m for m in self.misses.get(name, []) if m != "…"]
            if missed and name not in self.unscored:
                suffix = " …" if "…" in self.misses.get(name, []) else ""
                print(f"          missing: {', '.join(missed)}{suffix}")
        return clean


def god_records(raw: str) -> List[Dict[str, Any]]:
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = parsed.get("json", parsed.get("data", []))
    return parsed if isinstance(parsed, list) else []


def completeness(record: Dict[str, Any]) -> Tuple[int, int]:
    """How much of a god record is actually filled in.

    `Data:Gods.json` carries two Bastet objects differing only in `title`, and
    one of them is a stub missing its protection curves. Preferring the fuller
    record makes the dedupe deterministic rather than order-dependent, which
    matters because the id derived from the slug has to be the same in four
    processes.
    """
    stats = record.get("baseStats") or {}
    curves = sum(1 for key in stats if curve_length(stats, key) >= EXPECTED_LEVELS)
    fields = sum(1 for key in REQUIRED_GOD_FIELDS if record.get(key))
    return curves, fields


def dedupe_by_slug(
    records: List[Dict[str, Any]], verbose: bool = False
) -> List[Dict[str, Any]]:
    best: Dict[str, Dict[str, Any]] = {}
    for record in records:
        slug = str(record.get("slug", ""))
        if not slug:
            continue
        incumbent = best.get(slug)
        if incumbent is None or completeness(record) > completeness(incumbent):
            if incumbent is not None and verbose:
                print(
                    f"  deduped {slug}: kept the record with "
                    f"{completeness(record)[0]} full curves over "
                    f"{completeness(incumbent)[0]}"
                )
            best[slug] = record
        elif verbose:
            print(
                f"  deduped {slug}: kept the record with "
                f"{completeness(incumbent)[0]} full curves over "
                f"{completeness(record)[0]}"
            )
    return list(best.values())


def curve_length(stats: Any, key: str) -> int:
    if not isinstance(stats, dict):
        return 0
    value = stats.get(key)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return 0


def ability_section(page: str) -> Optional[str]:
    """The section holding the god's own abilities.

    Aspect gods repeat every `{{Ability}}` in an enhanced form under a separate
    heading, so taking the whole page double-counts. Returning None rather than
    falling back to the page is deliberate: a god whose headings we do not
    recognise should show up as a failure, not as six abilities that might be
    the wrong six.
    """
    found = wikitext.sections(page)
    for heading, body in found.items():
        if heading.strip().lower() in ("abilities", "ability"):
            return body
    return None


def stat_templates_in(value: str) -> List[str]:
    return [t.name for t in wikitext.parse_all(value)]


async def probe_gods(client: WikiClient) -> Tuple[Tally, Dict[str, Any]]:
    pages = await client.query_pages([GODS_DATA_PAGE])
    if GODS_DATA_PAGE not in pages:
        raise SystemExit(f"{GODS_DATA_PAGE} not found — the data page moved")

    raw_records = god_records(pages[GODS_DATA_PAGE]["content"])
    slugs = [str(r.get("slug", "")) for r in raw_records]
    duplicates = [s for s, n in collections.Counter(slugs).items() if n > 1]

    print(
        f"\n{GODS_DATA_PAGE}: {len(raw_records)} records, "
        f"{len(set(slugs))} distinct slugs"
    )
    print(f"  revid {pages[GODS_DATA_PAGE]['revid']}")
    if duplicates:
        print(f"  DUPLICATE SLUGS: {', '.join(sorted(duplicates))}")

    records = dedupe_by_slug(raw_records, verbose=True)

    data = Tally(len(records))
    for record in records:
        name = str(record.get("name") or record.get("slug") or "?")
        for field in REQUIRED_GOD_FIELDS:
            data.check(f"Data:Gods.json {field}", bool(record.get(field)), name)
        stats = record.get("baseStats")
        for stat in REQUIRED_STATS:
            data.check(
                f"baseStats {stat} x{EXPECTED_LEVELS}",
                curve_length(stats, stat) >= EXPECTED_LEVELS,
                name,
            )
        data.check("characterTags non-empty", bool(record.get("characterTags")), name)
        data.check("roleTags non-empty", bool(record.get("roleTags")), name)

    # Not scored — a manaless god legitimately has no mana curve, and the shape
    # of the exception is what the provider needs to know.
    manaless = [
        str(r.get("name"))
        for r in records
        if curve_length(r.get("baseStats"), "MaxMana") < EXPECTED_LEVELS
    ]
    print(f"  no MaxMana curve ({len(manaless)}): {', '.join(manaless) or 'none'}")
    resource_tags = collections.Counter(
        str(tag)
        for r in records
        for tag in (r.get("characterTags") or [])
        if "Resource" in str(tag)
    )
    print(
        "  resource tags: "
        + (", ".join(f"{t}({n})" for t, n in resource_tags.most_common()) or "none")
    )

    # The tracker.gg join key. If this is not in characterTags the whole
    # match-data pipeline needs a different way to identify a god.
    tag_shaped = sum(
        1
        for r in records
        if any(str(t).startswith("Gods.") for t in (r.get("characterTags") or []))
    )
    print(
        f"  gods carrying a 'Gods.X' characterTag (the tracker.gg join key): "
        f"{tag_shaped}/{len(records)}"
    )

    return data, {
        "records": records,
        "duplicates": duplicates,
        "revid": pages[GODS_DATA_PAGE]["revid"],
    }


async def probe_god_pages(client: WikiClient) -> Tally:
    rows = await client.bucket(
        'bucket("god_infoboxs2").select("page_name").limit(1000).run()'
    )
    titles = sorted({str(r["page_name"]) for r in rows if r.get("page_name")})
    print(f"\ngod_infoboxs2 bucket: {len(titles)} pages")

    pages = await client.query_pages(titles)
    tally = Tally(len(titles))
    headings: collections.Counter = collections.Counter()
    ability_counts: collections.Counter = collections.Counter()
    slot_vocabulary: collections.Counter = collections.Counter()
    aspect_bleed = 0

    for title in titles:
        page = pages.get(title, {}).get("content")
        if not page:
            for name in (
                "page fetched",
                "God infoboxS2",
                "infobox image",
                "infobox title",
                "infobox attack type",
                "lore section",
                f">={MIN_ABILITIES} abilities",
                "slots in known vocabulary",
                "opens with basic attack, has passive and ultimate",
                "abilities have names",
                "abilities have rank values",
                "skins",
            ):
                tally.check(name, False, title)
            continue
        tally.check("page fetched", True, title)

        headings.update(h.strip().lower() for h in wikitext.sections(page))

        infoboxes = wikitext.parse_templates(page, "God infoboxS2", top_level=True)
        tally.check("God infoboxS2", bool(infoboxes), title)
        infobox = infoboxes[0] if infoboxes else None
        tally.check("infobox image", bool(infobox and infobox.get("image")), title)
        tally.check("infobox title", bool(infobox and infobox.get("title")), title)
        tally.check(
            "infobox attack type", bool(infobox and infobox.get("attack type")), title
        )

        found = {h.strip().lower(): b for h, b in wikitext.sections(page).items()}
        tally.check("lore section", bool(found.get("lore", "").strip()), title)

        section = ability_section(page)
        abilities = (
            wikitext.parse_templates(section, "Ability") if section is not None else []
        )
        ability_counts[len(abilities)] += 1
        tally.check(
            f">={MIN_ABILITIES} abilities",
            len(abilities) >= MIN_ABILITIES,
            f"{title}({len(abilities)})",
        )
        tally.check(
            "abilities have names",
            bool(abilities) and all(a.get("name") for a in abilities),
            title,
        )

        # Names legitimately repeat across forms — Merlin casts Flicker in all
        # three stances with different numbers each time — so the duplicate-name
        # check that suggests itself here is wrong. The slot pattern is the
        # thing that actually breaks when the aspect section bleeds in.
        slots = [base_slot(a.get("slot")) for a in abilities]
        slot_vocabulary.update(a.get("slot") or "(none)" for a in abilities)
        tally.check(
            "slots in known vocabulary",
            bool(slots) and all(s in KNOWN_SLOTS for s in slots),
            f"{title}({'/'.join(s or '?' for s in slots)})",
        )
        tally.check(
            "opens with basic attack, has passive and ultimate",
            bool(slots)
            and slots[0] == "Basic Attack"
            and "Passive" in slots
            and "Ultimate" in slots,
            f"{title}({'/'.join(s or '?' for s in slots)})",
        )
        if len(wikitext.parse_templates(page, "Ability")) > len(abilities):
            aspect_bleed += 1
        # The passive and basic attack legitimately have no scaling values, so
        # the bar is that the real abilities do.
        scaling = [
            a
            for a in abilities
            if wikitext.parse_stat_block(a.get("stats")) or wikitext.rank_values(a.get("damage"))
        ]
        tally.check("abilities have rank values", len(scaling) >= 3, title)

        skin_viewers = [
            t for t in wikitext.parse_all(page) if t.name.startswith("#invoke:SkinViewer")
        ]
        tally.check("skins", bool(skin_viewers), title)

    print("\n  ability counts per god: " + ", ".join(
        f"{count}×{n}" for n, count in sorted(ability_counts.items())
    ))
    print(
        f"  gods where whole-page parsing would over-count: {aspect_bleed}/{len(titles)}"
        "  (this is what section scoping buys)"
    )
    print("  slot vocabulary: " + ", ".join(
        f"{s}({n})" for s, n in slot_vocabulary.most_common()
    ))
    print("  most common section headings: " + ", ".join(
        f"{h}({n})" for h, n in headings.most_common(12)
    ))
    return tally


async def probe_items(client: WikiClient) -> Tally:
    rows = await client.bucket(
        'bucket("item_infobox").select("page_name","icon","cost","total_cost")'
        ".limit(1000).run()"
    )
    titles = sorted({str(r["page_name"]) for r in rows if r.get("page_name")})
    with_total = sum(1 for r in rows if r.get("total_cost") is not None)
    print(f"\nitem_infobox bucket: {len(titles)} items, {with_total} with total_cost")

    pages = await client.query_pages(titles)
    every = Tally(len(titles))
    core = Tally(0)
    xtab: collections.Counter = collections.Counter()
    stat_templates: collections.Counter = collections.Counter()
    unclassified: List[str] = []

    for title in titles:
        page = pages.get(title, {}).get("content")
        if not page:
            every.check("page fetched", False, title)
            continue
        every.check("page fetched", True, title)

        infoboxes = wikitext.parse_templates(page, "Item infobox", top_level=True)
        every.check("Item infobox", bool(infoboxes), title)
        if not infoboxes:
            continue
        infobox = infoboxes[0]

        tier = infobox.get("tier")
        kind = infobox.get("type")
        xtab[(kind or "-", tier or "-")] += 1

        every.check("cost", bool(infobox.get("cost")), title)
        every.check("image", bool(infobox.get("image")), title)
        every.check("name", bool(infobox.get("name")), title)

        stats = [v for k, v in infobox.params.items() if re.fullmatch(r"stat\d+", k)]
        for value in stats:
            stat_templates.update(stat_templates_in(value))

        if not kind and not tier:
            unclassified.append(title)

        # Only items that can fill a core build slot need a tier and stats;
        # a Blink Rune has neither and never will. Scoring the whole catalogue
        # against a core-item bar just reports the catalogue's composition.
        if kind in CORE_ITEM_TYPES:
            core.total += 1
            core.check("tier", bool(tier), title)
            core.check("tier is 3", tier == "3", f"{title}(tier={tier or '-'})")
            core.check(">=1 stat", bool(stats), title)
            # Plenty of tier-3 items are stats only.
            core.check("passive", bool(infobox.get("passive")), title, scored=False)
            core.check(
                "recipe",
                bool(wikitext.parse_templates(page, "Recipe", top_level=True)),
                title,
            )

    print(f"\n  core items (type in {'/'.join(CORE_ITEM_TYPES)}): {core.total}")
    print(f"  {'type':<16}{'tier':>6}  count")
    for (kind, tier), n in sorted(xtab.items()):
        print(f"  {kind:<16}{tier:>6}  {n}")
    if unclassified:
        print(f"  no type AND no tier ({len(unclassified)}): {', '.join(unclassified)}")

    print(f"\n  stat templates in use ({len(stat_templates)}): " + ", ".join(
        f"{t}({n})" for t, n in stat_templates.most_common()
    ))

    known = {
        t.split(":", 1)[-1]
        for t in await client.category_members("Stat templates")
    }
    unknown = sorted(set(stat_templates) - known)
    print(f"  Category:Stat templates has {len(known)} members")
    if unknown:
        print(f"  NOT IN THAT CATEGORY: {', '.join(unknown)}")
    else:
        print("  every stat template in use is in that category")

    return every, core


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="also write the raw report here")
    args = parser.parse_args()

    async with WikiClient() as client:
        gods_tally, gods_raw = await probe_gods(client)
        pages_tally = await probe_god_pages(client)
        items_tally, core_tally = await probe_items(client)

    clean = True
    clean &= gods_tally.report("Data:Gods.json field coverage")
    clean &= pages_tally.report("God page parse coverage")
    clean &= items_tally.report("Item page parse coverage — all items")
    clean &= core_tally.report("Item page parse coverage — core build items")

    print(
        "\n"
        + ("PASS — the wiki can feed the static-data layer." if clean else
           "FAIL — something below 99%. Fix the parser before building on it.")
    )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as out:
            json.dump(
                {
                    "gods": gods_raw["records"],
                    "duplicate_slugs": gods_raw["duplicates"],
                    "gods_revid": gods_raw["revid"],
                },
                out,
            )
        print(f"wrote {args.json}")

    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
