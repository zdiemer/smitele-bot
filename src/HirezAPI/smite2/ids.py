"""Identity for Smite 2 gods and items.

Two problems, one module.

**Stable integer ids.** Smite 1 hands out integers, and everything downstream
assumes them: `items` is `Dict[int, Item]`, `build_features` builds an integer
matrix, the corpus stores `ItemId1..6`. Smite 2 has no integer ids at all —
`Data:Gods.json` publishes `id: ""` for every god, and tracker.gg identifies
items by slug. So they are derived, by hash, from the slug.

The derivation has to be a pure function rather than a persisted registry
because four processes on three schedules — the bot, the collector, the
aggregate job and the trainer — must agree without coordinating, and a corpus
written last month must still read correctly today. A registry would need
locking and would invalidate the corpus whenever it was rebuilt.

Ids land above `1 << 31`, which is a safety property rather than cosmetics:
every Smite 1 id is below 25,000, so a frame that accidentally mixes games
produces "unknown id" — which `build_features.annotate` already handles by
marking the row unusable — instead of a silently wrong join.

**Joining tracker.gg to the wiki.** The two sources spell the same thing
differently, and the differences were measured rather than guessed. Over 26,758
observed player rows, matching tracker.gg's god field against wiki slugs alone
covers 96.09%. The residue is entirely spelling:

    jingwei      the wiki slug is jing-wei
    morrigan     the wiki calls it The Morrigan
    xing-tian    absent from Data:Gods.json altogether, though the page exists
    Gods.X       tracker.gg sometimes emits the raw unnormalized token

Collapsing to alphanumerics, stripping a leading "the", stripping the `Gods.`
prefix, and indexing the *page list* rather than just `Data:Gods.json` takes
that to 100.00%. Items reach 99.59% by occurrence the same way, the remainder
being one item the wiki does not document.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, Iterable, Optional, Set

# Every Smite 1 god and item id is below 25,000, so nothing above this can
# collide with one and a mixed frame fails loudly rather than quietly.
#
# 2^30 rather than 2^31 because the aggregate stores GodId as int32 — it exists
# to fit a 132-million-row corpus in memory, so widening a column there to suit
# this would be the tail wagging the dog. With a 30-bit hash the largest id is
# exactly int32's maximum, and the range is still four orders of magnitude clear
# of anything Smite 1 issues.
S2_ID_BASE = 1 << 30
_HASH_MASK = (1 << 30) - 1

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# tracker.gg occasionally emits the engine's own token instead of a slug.
_ENGINE_PREFIX = "Gods."


def squash(value: object) -> str:
    """Collapse a name to alphanumerics.

    `Jing Wei`, `jing-wei` and `jingwei` are one key; so are `Chronos' Pendant`
    and `chronos-pendant`.
    """
    return _NON_ALNUM.sub("", str(value).lower())


def slugify(name: str) -> str:
    """A wiki page name in tracker.gg's slug spelling.

    `Chronos' Pendant` → `chronos-pendant`. Apostrophes are dropped rather than
    turned into separators, which is why they go before the substitution.
    """
    lowered = name.lower().replace("'", "").replace("’", "")
    return _NON_ALNUM.sub("-", lowered).strip("-")


def join_keys(value: object) -> Set[str]:
    """Every spelling one name should answer to.

    The leading-article rule earns its place: tracker.gg says `morrigan` where
    the wiki says `The Morrigan`, and handling it generally beats carrying an
    alias entry per god that happens to have an article.
    """
    text = str(value)
    if text.startswith(_ENGINE_PREFIX):
        text = text[len(_ENGINE_PREFIX) :]
    base = squash(text)
    keys = {base}
    if base.startswith("the"):
        keys.add(base[3:])
    return {key for key in keys if key}


def lookup_key(value: object) -> str:
    """The single key to look a tracker.gg value up by."""
    text = str(value)
    if text.startswith(_ENGINE_PREFIX):
        text = text[len(_ENGINE_PREFIX) :]
    return squash(text)


class NameIndex:
    """Maps however a source spells a name onto one canonical name."""

    def __init__(self) -> None:
        self.__keys: Dict[str, str] = {}

    def add(self, canonical: str, *aliases: object) -> None:
        for source in (canonical, *aliases):
            if not source:
                continue
            for key in join_keys(source):
                # First writer wins, so the canonical page list is not
                # overwritten by a stub record that happens to share a key.
                self.__keys.setdefault(key, canonical)

    def get(self, value: object) -> Optional[str]:
        return self.__keys.get(lookup_key(value))

    def __contains__(self, value: object) -> bool:
        return lookup_key(value) in self.__keys

    def __len__(self) -> int:
        return len(set(self.__keys.values()))


def s2_id(kind: str, slug: str) -> int:
    """A stable integer id for a Smite 2 god or item.

    blake2b rather than the builtin `hash`, which is salted per process by
    PYTHONHASHSEED and so would differ between the bot and the collector — the
    exact failure this function exists to avoid.
    """
    digest = hashlib.blake2b(f"{kind}:{slug}".encode(), digest_size=4).digest()
    return S2_ID_BASE | (int.from_bytes(digest, "big") & _HASH_MASK)


def god_id(slug: str) -> int:
    return s2_id("god", slug)


def item_id(slug: str) -> int:
    return s2_id("item", slug)


def assert_no_collisions(kind: str, slugs: Iterable[str]) -> Dict[int, str]:
    """Ids for every slug, raising rather than trusting a 32-bit hash.

    The probability over a few hundred slugs is ~3e-5, which is small enough to
    ignore and far too large to discover in production as two items sharing a
    build hash.
    """
    out: Dict[int, str] = {}
    for slug in slugs:
        identifier = s2_id(kind, slug)
        existing = out.get(identifier)
        if existing is not None and existing != slug:
            raise ValueError(
                f"{kind} id collision: {slug!r} and {existing!r} both hash to {identifier}"
            )
        out[identifier] = slug
    return out
