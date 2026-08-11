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
# played five minutes ago shows up. This one is about *recency*, so it stays
# short and applies only to the match list.
CACHE_SECONDS = 180

# Aggregates — profiles, per-god segments, how deep a history goes — do not move
# in three minutes, and every request the bot spends is one the nightly crawl
# does not get. Held far longer on purpose.
PROFILE_CACHE_SECONDS = 900

# A lobby's roster is fixed the moment the match starts, so the only thing this
# staleness can cost is answering for a match that has just ended — a minute of
# that is worth far more than re-fetching for every command. It also bounds what
# a linked player can spend: `/build` asks on every invocation, and without a
# cache a chatty channel would put the crawl's request budget on the floor.
LIVE_MATCH_CACHE_SECONDS = 60

FIRST_MATCH_FILE = "first_match.json"
PAGE_COUNT_FILE = "page_count.json"

# How deep /first_match will search before giving up. An active player reaches
# ~256 pages over two years, so 512 is already generous; the old 4096 bought
# nothing and cost three extra probes per player on every single call, because
# the doubling search walks to the ceiling before it can bisect.
MAX_PAGES = 512


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


@dataclass
class LivePlayer:
    god: str
    team: str
    handle: str


@dataclass
class LiveMatch:
    """A lobby in progress, as much of it as tracker.gg will say.

    `team` is `order` or `chaos` rather than a number, and `own_team` is what
    turns the ten players into allies and enemies — which is the one thing the
    Smite 1 path cannot do without guessing, since `getmatchplayerdetails`
    labels no lanes.
    """

    match_id: str
    mode: str
    mode_name: str
    ranked: bool
    own_god: str
    own_team: str
    players: List[LivePlayer]
    # When tracker.gg last refreshed this lobby (its `snapshotTimestamp`),
    # as epoch seconds, or 0 when the payload did not carry one. Observed
    # cadence is roughly ten minutes, which is the whole of the "live status
    # lags" complaint; showing the age is the honest thing a display can do.
    snapshot_at: float = 0.0

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.snapshot_at) if self.snapshot_at else 0.0

    @property
    def allies(self) -> List[str]:
        """Team-mates' gods, excluding the player themselves."""
        found = [p.god for p in self.players if p.team == self.own_team]
        try:
            found.remove(self.own_god)
        except ValueError:
            pass
        return found

    @property
    def enemies(self) -> List[str]:
        return [p.god for p in self.players if p.team and p.team != self.own_team]


def _live_player(segment: Dict[str, Any]) -> Optional[LivePlayer]:
    """One player out of a match segment, or None if it is not a player row.

    A match carries twelve segments and only ten of them are players; the god
    name is what distinguishes them, not the segment type, which is `overview`
    for both the per-player rows and the lobby's own summary.
    """
    metadata = segment.get("metadata") or {}
    god = metadata.get("godName")
    if not god:
        return None
    attributes = segment.get("attributes") or {}
    platform_info = metadata.get("platformInfo")
    handle = ""
    if isinstance(platform_info, dict):
        handle = platform_info.get("platformUserHandle") or ""
    if not handle:
        # The live route carries the handle directly on the segment metadata
        # rather than nested under platformInfo; without this, every player in
        # a live lobby rendered as "Hidden Player".
        handle = metadata.get("platformUserHandle") or ""
    return LivePlayer(
        god=str(god),
        team=str(attributes.get("teamId") or metadata.get("teamId") or ""),
        handle=str(handle),
    )


class PlayerLookups:
    """tracker.gg per-player reads, with the caching the bot needs."""

    def __init__(self, client_factory, cache_dir: Optional[str] = None, silent=False):
        self.__client_factory = client_factory
        self.__cache: Dict[str, Tuple[float, Any]] = {}
        self.__silent = silent
        self.__first_match_path = (
            os.path.join(cache_dir, FIRST_MATCH_FILE) if cache_dir else None
        )
        self.__page_count_path = (
            os.path.join(cache_dir, PAGE_COUNT_FILE) if cache_dir else None
        )
        self.__first_match: Dict[str, Any] = self.__restore(self.__first_match_path)
        self.__page_counts: Dict[str, Any] = self.__restore(self.__page_count_path)

    @staticmethod
    def __restore(path: Optional[str]) -> Dict[str, Any]:
        """A remembered mapping, or an empty one. Never raises: both of these
        are optimisations, and a bad file should cost requests, not the bot."""
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def __persist(self, path: Optional[str], data: Dict[str, Any]) -> None:
        """Write-to-partial then rename, because the bot and the web pods read
        these files without coordinating and a torn read is worse than a miss."""
        if not path:
            return
        partial = f"{path}.partial"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(partial, path)
        except OSError as error:
            self.__log(f"could not persist {os.path.basename(path)}: {error}")

    def __log(self, message: str) -> None:
        if not self.__silent:
            print(f"smite2 players: {message}", flush=True)

    def __cached(self, key: str, ttl: float = CACHE_SECONDS):
        entry = self.__cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.time() - stored_at > ttl:
            del self.__cache[key]
            return None
        return value

    def __store(self, key: str, value: Any) -> Any:
        self.__cache[key] = (time.time(), value)
        return value

    async def profile(self, platform: str, handle: str) -> Optional[Dict[str, Any]]:
        key = f"profile:{platform}:{handle}"
        cached = self.__cached(key, PROFILE_CACHE_SECONDS)
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

        Returns `(platform_info, gamemode_segments)` — the first carrying the
        display handle and avatar, which is the only place either is published.
        Hi-Rez has no equivalent worth reading: its `Avatar_URL` is a vestige of
        the old web profile and the one populated value in this roster 403s.

        **Filtered by type, and that is load-bearing.** The profile mixes three
        kinds of segment in one list — for one account it was 9 god, 4 gamemode
        and 3 role — so taking them all sums a player's matches roughly twice
        and lets "best mode" come back as *Jungle*, which is a lane. The god
        rows here are also only a recent slice; `segments(kind="god")` is the
        full list and is what callers should use for that.
        """
        raw = await self.profile(platform, handle)
        if not raw:
            return None
        return (
            raw.get("platformInfo") or {},
            [
                _segment(row, "gamemode")
                for row in (raw.get("segments") or [])
                if row.get("type") == "gamemode"
            ],
        )

    async def segments(
        self, platform: str, handle: str, kind: str
    ) -> List[Segment]:
        key = f"segments:{kind}:{platform}:{handle}"
        cached = self.__cached(key, PROFILE_CACHE_SECONDS)
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

    async def live_match(
        self, platform: str, handle: str
    ) -> Optional["LiveMatch"]:
        """The lobby this player is in, or None if they are not in one.

        Two requests: one to learn the match id and which side the player is
        on, one to read the other nine. Cached briefly because a lobby changes
        only when the match ends, and because `/build` will ask for this on
        every invocation from a linked player — the same reason the rate
        limiter is held by the provider rather than per command.

        Not-in-a-match is the common answer and costs exactly one request, and
        the negative is cached too. Any failure returns None: a build should
        lose its matchup, never its response.
        """
        key = f"live:{platform}:{handle}"
        cached = self.__cached(key, LIVE_MATCH_CACHE_SECONDS)
        if cached is not None:
            return cached or None

        try:
            async with self.__client_factory() as client:
                own = await client.live_match(platform, handle)
                match_id = ((own.get("attributes") or {}).get("id")) if own else None
                if not match_id:
                    # Cache the negative as a falsy sentinel; None would mean
                    # "not asked yet" and re-request on every command.
                    self.__store(key, False)
                    return None
                # `/live` answers with a whole match object whose `segments`
                # hold the caller's own row — not with a bare segment, which
                # is what the original probe recorded. Passing the match dict
                # to `_live_player` found no godName in the match's own
                # metadata and returned None, so the command answered "isn't
                # in a match" for players standing in the fountain. Both
                # shapes are accepted: a bare segment first, then the rows.
                mine = _live_player(own)
                if mine is None:
                    mine = next(
                        (
                            player
                            for player in (
                                _live_player(segment)
                                for segment in (own.get("segments") or [])
                            )
                            if player is not None
                        ),
                        None,
                    )
                match = await client.match(str(match_id))
        except Exception as error:  # noqa: BLE001 — never fatal to a command
            self.__log(f"live match lookup failed: {error}")
            return None

        players = [
            player
            for player in (
                _live_player(segment) for segment in (match.get("segments") or [])
            )
            if player is not None
        ]
        if mine is None or not players:
            self.__store(key, False)
            return None

        metadata = match.get("metadata") or {}
        attributes = match.get("attributes") or {}
        snapshot_at = 0.0
        stamp = metadata.get("snapshotTimestamp")
        if stamp:
            try:
                from datetime import datetime  # noqa: PLC0415

                snapshot_at = datetime.fromisoformat(str(stamp)).timestamp()
            except ValueError:
                snapshot_at = 0.0
        return self.__store(
            key,
            LiveMatch(
                match_id=str(match_id),
                mode=str(attributes.get("gamemode") or ""),
                mode_name=str(metadata.get("gamemodeName") or ""),
                ranked=bool(metadata.get("isRanked")),
                own_god=mine.god,
                own_team=mine.team,
                players=players,
                snapshot_at=snapshot_at,
            ),
        )

    async def page_count(self, platform: str, handle: str) -> int:
        """How many pages of history a player has.

        Remembered on disk as well as in memory, because the answer is a
        *monotonic* one: a history only grows, so yesterday's count is still a
        correct lower bound today. Handing it back as the search's starting
        point turns roughly twenty-four probes into three, which is the
        difference between `/first_match` costing a third of an hour's
        allowance and costing almost none of it.
        """
        key = f"pages:{platform}:{handle}"
        cached = self.__cached(key, PROFILE_CACHE_SECONDS)
        if cached is not None:
            return cached
        remembered = int(self.__page_counts.get(key) or 0)
        async with self.__client_factory() as client:
            count = await client.page_count(
                platform, handle, ceiling=MAX_PAGES, known=remembered
            )
        if count and count != remembered:
            self.__page_counts[key] = count
            self.__persist(self.__page_count_path, self.__page_counts)
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
        self.__persist(self.__first_match_path, self.__first_match)

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
