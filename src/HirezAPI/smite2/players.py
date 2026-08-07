"""Per-player Smite 2 lookups, straight from tracker.gg.

These do not read the corpus, and that is the point. The corpus is a snowball
sample and always will be — tracker.gg cannot be enumerated by time — so it is
right for aggregate build win rates and wrong for "how has this player done on
Anubis". Asked per player, the same source answers exactly and completely.

Everything here is cached briefly, because a Discord user pressing a command
twice should not cost two multi-megabyte transfers, and `/first_match` is cached
forever, because the first match two people played together does not change.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from smite2.tracker_client import PLATFORMS

# Long enough that pressing a command twice is free, short enough that a match
# played five minutes ago shows up.
CACHE_SECONDS = 180

FIRST_MATCH_FILE = "first_match.json"

# How deep /first_match will search before giving up. An active player reaches
# ~256 pages over two years; this is generous but bounded, because the cost is
# paid by someone waiting on an interaction.
MAX_PAGES = 4096


def parse_player(value: str, default_platform: str = "steam") -> Tuple[str, str]:
    """`platform:handle`, or a bare handle on the default platform.

    Smite 2 has no global player name — an identity is a platform plus a handle,
    and the same handle can exist on several — so the platform has to be part of
    what the user gives us.
    """
    text = (value or "").strip()
    if ":" in text:
        platform, _, handle = text.partition(":")
        platform = platform.strip().lower()
        if platform in PLATFORMS:
            return platform, handle.strip()
    return default_platform, text


@dataclass
class Segment:
    """One row of a player's aggregate stats — per god, mode or role."""

    key: str
    name: str
    image_url: Optional[str]
    stats: Dict[str, float] = field(default_factory=dict)
    display: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def matches(self) -> int:
        return int(self.stats.get("matchesPlayed", 0))

    @property
    def wins(self) -> int:
        return int(self.stats.get("matchesWon", 0))

    @property
    def losses(self) -> int:
        return int(self.stats.get("matchesLost", 0))

    @property
    def win_rate(self) -> float:
        return float(self.stats.get("matchesWinPct", 0.0)) / 100.0

    @property
    def kda(self) -> float:
        return float(self.stats.get("kdaRatio", 0.0))


def _segment(raw: Dict[str, Any], key_field: str) -> Segment:
    attributes = raw.get("attributes") or {}
    metadata = raw.get("metadata") or {}
    stats_raw = raw.get("stats") or {}
    return Segment(
        key=str(attributes.get(key_field) or ""),
        name=str(metadata.get("name") or attributes.get(key_field) or ""),
        image_url=metadata.get("imageUrl"),
        stats={
            name: value.get("value")
            for name, value in stats_raw.items()
            if isinstance(value, dict) and isinstance(value.get("value"), (int, float))
        },
        display={
            name: str(value.get("displayValue"))
            for name, value in stats_raw.items()
            if isinstance(value, dict)
        },
        metadata=metadata,
    )


@dataclass
class MatchSummary:
    match_id: str
    mode: str
    mode_name: str
    ranked: bool
    timestamp: str
    duration: int
    won: bool
    god: str
    god_name: str
    god_image: Optional[str]
    role: str
    kills: int
    deaths: int
    assists: int
    skill_rating: Optional[float]
    skill_rating_delta: Optional[float]


class PlayerLookups:
    """tracker.gg per-player reads, with the caching the bot needs."""

    def __init__(self, client_factory, cache_dir: Optional[str] = None, silent=False):
        self.__client_factory = client_factory
        self.__cache: Dict[str, Tuple[float, Any]] = {}
        self.__silent = silent
        self.__first_match_path = (
            os.path.join(cache_dir, FIRST_MATCH_FILE) if cache_dir else None
        )
        self.__first_match: Dict[str, Any] = {}
        if self.__first_match_path:
            try:
                with open(self.__first_match_path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                self.__first_match = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self.__first_match = {}

    def __log(self, message: str) -> None:
        if not self.__silent:
            print(f"smite2 players: {message}", flush=True)

    def __cached(self, key: str):
        entry = self.__cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.time() - stored_at > CACHE_SECONDS:
            del self.__cache[key]
            return None
        return value

    def __store(self, key: str, value: Any) -> Any:
        self.__cache[key] = (time.time(), value)
        return value

    async def profile(self, platform: str, handle: str) -> Optional[Dict[str, Any]]:
        key = f"profile:{platform}:{handle}"
        cached = self.__cached(key)
        if cached is not None:
            return cached
        async with self.__client_factory() as client:
            data = await client.profile(platform, handle)
        if not data:
            return None
        return self.__store(key, data)

    async def overview(
        self, platform: str, handle: str
    ) -> Optional[Tuple[Dict[str, Any], List[Segment]]]:
        """Who a player is *and* their per-mode stats, from one request.

        The profile response already carries every gamemode segment, so asking
        `/profile` and then `/segments/gamemode` fetches the same rows twice.
        That matters more here than it looks: tracker.gg refused this address
        after ~300 requests in one run, and every request the site's player
        refresh spends is one the nightly crawl does not get.

        Returns `(platform_info, segments)` — the first carrying the display
        handle and avatar, which is the only place either is published. Hi-Rez
        has no equivalent worth reading: its `Avatar_URL` is a vestige of the
        old web profile and the one populated value in this roster 403s.
        """
        raw = await self.profile(platform, handle)
        if not raw:
            return None
        return (
            raw.get("platformInfo") or {},
            [_segment(row, "gamemode") for row in (raw.get("segments") or [])],
        )

    async def segments(
        self, platform: str, handle: str, kind: str
    ) -> List[Segment]:
        key = f"segments:{kind}:{platform}:{handle}"
        cached = self.__cached(key)
        if cached is not None:
            return cached
        async with self.__client_factory() as client:
            raw = await client.segments(platform, handle, kind)
        field_name = {"god": "god", "gamemode": "gamemode", "role": "role"}[kind]
        parsed = [_segment(row, field_name) for row in raw]
        # Only rows the player has actually played. tracker.gg returns a segment
        # for every god in the game, most of them empty.
        parsed = [s for s in parsed if s.matches > 0]
        return self.__store(key, parsed)

    async def recent_matches(
        self, platform: str, handle: str, limit: int = 10
    ) -> List[MatchSummary]:
        """The player's most recent matches, from page one only.

        A page is 25 matches and ~2.9 MB, so this streams and stops as soon as
        it has enough rather than buffering the response.
        """
        key = f"matches:{platform}:{handle}:{limit}"
        cached = self.__cached(key)
        if cached is not None:
            return cached

        out: List[MatchSummary] = []
        async with self.__client_factory() as client:
            async for match in client.iter_matches(platform, handle, 0):
                summary = _summarise(match, platform, handle)
                if summary is not None:
                    out.append(summary)
                if len(out) >= limit:
                    break
        return self.__store(key, out)

    async def page_count(self, platform: str, handle: str) -> int:
        key = f"pages:{platform}:{handle}"
        cached = self.__cached(key)
        if cached is not None:
            return cached
        async with self.__client_factory() as client:
            count = await client.page_count(platform, handle, ceiling=MAX_PAGES)
        return self.__store(key, count)

    # --- /first_match ----------------------------------------------------

    @staticmethod
    def __pair_key(one: Tuple[str, str], two: Tuple[str, str]) -> str:
        return "|".join(sorted((f"{one[0]}:{one[1]}", f"{two[0]}:{two[1]}")))

    def cached_first_match(
        self, one: Tuple[str, str], two: Tuple[str, str]
    ) -> Optional[Dict[str, Any]]:
        return self.__first_match.get(self.__pair_key(one, two))

    def __remember_first_match(
        self, one: Tuple[str, str], two: Tuple[str, str], value: Dict[str, Any]
    ) -> None:
        self.__first_match[self.__pair_key(one, two)] = value
        if not self.__first_match_path:
            return
        partial = f"{self.__first_match_path}.partial"
        try:
            os.makedirs(os.path.dirname(self.__first_match_path) or ".", exist_ok=True)
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump(self.__first_match, handle)
            os.replace(partial, self.__first_match_path)
        except OSError as error:
            self.__log(f"could not persist first_match: {error}")

    async def first_shared_match(
        self, one: Tuple[str, str], two: Tuple[str, str], budget: int = 40
    ) -> Optional[Dict[str, Any]]:
        """The earliest match these two players appear in together.

        Two properties make this affordable. History is ordered — page N is
        strictly older than page N-1, with no overlap — and a page past the end
        is empty, so the oldest page is found by doubling then bisecting rather
        than by walking every page. And the answer never changes, so it is
        written to disk and never recomputed.

        The search then walks *backwards from the oldest page*, because two
        people who have played together for a while first did so near the start
        of the shorter history. `budget` bounds the pages read so one
        interaction cannot run away.
        """
        remembered = self.cached_first_match(one, two)
        if remembered is not None:
            return remembered

        async with self.__client_factory() as client:
            deepest = min(
                await client.page_count(*one, ceiling=MAX_PAGES),
                await client.page_count(*two, ceiling=MAX_PAGES),
            )
            if deepest == 0:
                return None

            found = None
            for offset in range(min(budget, deepest)):
                page = deepest - 1 - offset
                if page < 0:
                    break
                theirs = {}
                async for match in client.iter_matches(*one, page):
                    theirs[match["attributes"]["id"]] = match
                if not theirs:
                    continue
                shared = []
                async for match in client.iter_matches(*two, page):
                    if match["attributes"]["id"] in theirs:
                        shared.append(match)
                if shared:
                    # Oldest on the page, since a page runs newest-first.
                    found = min(
                        shared, key=lambda m: m["metadata"].get("timestamp") or ""
                    )
                    break

            if found is None:
                return None

        result = {
            "match_id": found["attributes"]["id"],
            "timestamp": found["metadata"].get("timestamp"),
            "mode": found["metadata"].get("gamemodeName")
            or found["attributes"].get("gamemode"),
            "duration": found["metadata"].get("duration"),
        }
        self.__remember_first_match(one, two, result)
        return result


def _summarise(
    match: Dict[str, Any], platform: str, handle: str
) -> Optional[MatchSummary]:
    """One match from the queried player's point of view."""
    attributes = match.get("attributes") or {}
    metadata = match.get("metadata") or {}

    wanted = str(handle).lower()
    mine = None
    for segment in match.get("segments") or []:
        if segment.get("type") != "overview":
            continue
        attrs = segment.get("attributes") or {}
        if str(attrs.get("platformUserIdentifier") or "").lower() == wanted:
            mine = segment
            break
    if mine is None:
        return None

    meta = mine.get("metadata") or {}
    stats = mine.get("stats") or {}

    def value(name: str, default=0):
        entry = stats.get(name)
        return entry.get("value", default) if isinstance(entry, dict) else default

    rating = value("skillRating", None)
    delta = value("skillRatingDelta", None)

    return MatchSummary(
        match_id=str(attributes.get("id") or ""),
        mode=str(attributes.get("gamemode") or ""),
        mode_name=str(metadata.get("gamemodeName") or attributes.get("gamemode") or ""),
        ranked=bool(metadata.get("isRanked")),
        timestamp=str(metadata.get("timestamp") or ""),
        duration=int(metadata.get("duration") or 0),
        won=meta.get("teamId") == metadata.get("winningTeamId"),
        god=str(meta.get("god") or ""),
        god_name=str(meta.get("godName") or meta.get("god") or ""),
        god_image=meta.get("godImageUrl"),
        role=str((meta.get("playedRole") or meta.get("assignedRole") or {}).get("key") or ""),
        kills=int(value("kills")),
        deaths=int(value("deaths")),
        assists=int(value("assists")),
        skill_rating=rating,
        skill_rating_delta=delta,
    )


def best_and_worst(
    segments: Iterable[Segment], minimum: int = 10
) -> Tuple[Optional[Segment], Optional[Segment]]:
    """Highest and lowest win rate among segments with enough games.

    The threshold matters: a single win on a god is a 100% win rate, and the
    Smite 1 command uses the same ten-match floor for the same reason.
    """
    eligible = [s for s in segments if s.matches >= minimum]
    if not eligible:
        return None, None
    return (
        max(eligible, key=lambda s: (s.win_rate, s.matches)),
        min(eligible, key=lambda s: (s.win_rate, -s.matches)),
    )
