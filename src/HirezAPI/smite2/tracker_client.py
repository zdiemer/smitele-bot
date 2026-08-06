"""The tracker.gg internal API — the only source of per-match Smite 2 builds.

This is an undocumented endpoint behind a WAF, with no published rate limit and
no terms allowance for bulk pulls. Everything about how it is used here follows
from that: requests are paced by a fixed interval rather than issued as fast as
they complete, responses are streamed rather than buffered, and a 403 stops the
caller instead of triggering a retry loop.

The pacing default of 1.5 s is the figure the original probe sustained without
drawing an error. It is not a measured limit — establishing one means being
blocked — so it is a dial with a conservative default rather than something to
tune upward casually.

Match-list responses are ~2.9 MB and carry all ten players of all 25 matches,
which is 250 player-builds per request. That density is what makes a crawl
viable at all, and it is also why `iter_matches` streams: buffering thousands of
them would hold gigabytes.

**This uses `curl_cffi`, not `aiohttp`, and that is load-bearing.** The earlier
probe recorded the clearance cookie as portable across TLS fingerprints, having
replayed it successfully from `urllib`. That generalised too far. Measured
against one cookie, held constant:

    urllib                     200      aiohttp (any header permutation)   403
    curl_cffi impersonate Firefox 200   curl_cffi impersonate Chrome       403
                                        curl (no impersonation)            403

Cloudflare is checking that the TLS handshake is *consistent with the user
agent*. The cookie is minted by a Firefox, so only a Firefox-shaped handshake is
honoured — Chrome impersonation fails precisely because the UA says Firefox.
urllib passes because its generic handshake is not classified as a mismatched
browser, which is luck rather than portability and not something to build on.

So the impersonation target and the minted user agent have to stay in agreement;
they are two halves of one identity.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

import ijson
from curl_cffi import requests as curl_requests

from smite2.clearance import Clearance, ClearanceManager

# Camoufox mints a Firefox user agent, so the handshake has to be Firefox's.
# The unversioned alias tracks curl_cffi's newest Firefox profile, which is what
# we want — the exact version need not match the UA, only the browser family.
IMPERSONATE = "firefox"

API_HOST = "https://api.tracker.gg"

# The site slug. "smite-2" 404s.
GAME_SLUG = "smite2"

PLATFORMS = ("steam", "epic", "psn", "xbl")

# The boards that respond. "SkillRating" 500s on every parameter set tried and
# appears to be reachable server-side only.
LEADERBOARDS = (
    "Wins",
    "Kills",
    "Assists",
    "Damage",
    "GoldEarned",
    "XpEarned",
    "TimePlayed",
)

DEFAULT_INTERVAL_SECONDS = 1.5


class TrackerBlocked(RuntimeError):
    """The WAF refused us. Never retried in a loop — callers should stop."""


class TrackerServerError(RuntimeError):
    """A 5xx that survived a retry. Bad for this route, not for the run."""


class RateLimiter:
    """A minimum gap between request starts, shared across workers.

    A gap rather than a concurrency limit, because what matters to a WAF is the
    rate it observes, and two workers each sleeping 1.5 s between their own
    requests produce twice the rate one does.
    """

    def __init__(self, interval: float = DEFAULT_INTERVAL_SECONDS):
        self.interval = interval
        self.__lock = asyncio.Lock()
        self.__next_at = 0.0

    async def wait(self) -> None:
        async with self.__lock:
            now = time.monotonic()
            if now < self.__next_at:
                await asyncio.sleep(self.__next_at - now)
                now = time.monotonic()
            self.__next_at = now + self.interval


class TrackerClient:
    """Paced, clearance-carrying access to the routes we actually use."""

    def __init__(
        self,
        clearance: ClearanceManager,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        silent: bool = False,
    ):
        self.__clearance = clearance
        self.__limiter = RateLimiter(interval)
        self.__session: Optional[curl_requests.AsyncSession] = None
        self.__current: Optional[Clearance] = None
        self.__silent = silent
        self.requests = 0
        self.bytes = 0

    async def __aenter__(self) -> "TrackerClient":
        self.__session = curl_requests.AsyncSession(impersonate=IMPERSONATE)
        await self.__session.__aenter__()
        return self

    async def __aexit__(self, *args) -> None:
        if self.__session is not None:
            await self.__session.__aexit__(*args)
            self.__session = None

    def __log(self, message: str) -> None:
        if not self.__silent:
            print(f"tracker: {message}", flush=True)

    def url(self, path: str) -> str:
        return f"{API_HOST}{path}"

    async def __headers(self) -> Dict[str, str]:
        self.__current = await self.__clearance.get()
        return self.__current.headers()

    def __check(self, path: str, status: int, attempt: int) -> bool:
        """Whether to retry. Raises if the status means stop.

        A 403 is retried exactly once, after discarding the cookie, because the
        overwhelmingly likely cause is expiry. A second 403 means we are
        actually blocked, and the only correct response to that is to stop —
        never to retry into a WAF.
        """
        if status == 403:
            if attempt == 2:
                raise TrackerBlocked(f"403 twice on {path} — stopping")
            self.__log("403; discarding cookie and minting once")
            if self.__current is not None:
                self.__clearance.invalidate(self.__current)
            return True
        if status == 429:
            raise TrackerBlocked(f"429 on {path} — pacing is too aggressive")
        if status >= 500:
            # A server error is not a block. Some routes 500 persistently — the
            # SkillRating board does on every parameter set — so retry once and
            # then give up on this path rather than on the run.
            if attempt == 1:
                self.__log(f"HTTP {status} on {path}; one retry")
                return True
            raise TrackerServerError(f"HTTP {status} on {path}")
        if self.__current is not None:
            self.__clearance.mark_ok(self.__current)
        return False

    async def get_json(self, path: str, params: Optional[Dict] = None) -> Any:
        """A whole response, for the small ones — profiles and leaderboards."""
        if self.__session is None:
            raise RuntimeError("TrackerClient used outside its context manager")

        for attempt in (1, 2):
            await self.__limiter.wait()
            headers = await self.__headers()
            response = await self.__session.get(
                self.url(path), params=params, headers=headers, timeout=120
            )
            self.requests += 1
            if self.__check(path, response.status_code, attempt):
                continue
            self.bytes += len(response.content)
            return json.loads(response.content)

        raise TrackerBlocked(f"gave up on {path}")

    async def iter_matches(
        self, platform: str, handle: str, page: int = 0
    ) -> AsyncIterator[Dict[str, Any]]:
        """One page of a player's match history, streamed match by match.

        There is no date or time-range parameter on this route — matches are
        reachable only per player, newest first, 25 to a page. That absence is
        the single fact that shapes the whole collector.

        Parsed by pushing chunks into ijson rather than buffering: at ~2.9 MB a
        page and thousands of pages a night, holding whole responses is the
        difference between bounded memory and gigabytes.
        """
        if self.__session is None:
            raise RuntimeError("TrackerClient used outside its context manager")

        path = f"/api/v2/{GAME_SLUG}/standard/matches/{platform}/{handle}"
        params = {"next": page} if page else None

        for attempt in (1, 2):
            await self.__limiter.wait()
            headers = await self.__headers()
            async with self.__session.stream(
                "GET", self.url(path), params=params, headers=headers, timeout=180
            ) as response:
                self.requests += 1
                if self.__check(path, response.status_code, attempt):
                    continue

                matches = ijson.sendable_list()
                # use_float, or every number arrives as a Decimal and poisons
                # everything downstream — Decimals do not survive json.dump and
                # land in pandas as object columns rather than numerics.
                coroutine = ijson.items_coro(
                    matches, "data.matches.item", use_float=True
                )
                try:
                    async for chunk in response.aiter_content():
                        self.bytes += len(chunk)
                        coroutine.send(chunk)
                        for match in matches:
                            yield match
                        del matches[:]
                    coroutine.close()
                    for match in matches:
                        yield match
                except ijson.JSONError as error:
                    raise TrackerBlocked(f"unparseable body on {path}: {error}")
                return

        raise TrackerBlocked(f"gave up on {path}")

    async def leaderboard(
        self, board: str, platform: str = "steam", skip: int = 0, take: int = 50
    ) -> List[Dict[str, Any]]:
        body = await self.get_json(
            f"/api/v1/{GAME_SLUG}/standard/leaderboards",
            {
                "type": "stats",
                "board": board,
                "platform": platform,
                "skip": skip,
                "take": take,
            },
        )
        return (body.get("data") or {}).get("items") or []

    async def profile(self, platform: str, handle: str) -> Dict[str, Any]:
        body = await self.get_json(
            f"/api/v2/{GAME_SLUG}/standard/profile/{platform}/{handle}"
        )
        return body.get("data") or {}

    async def segments(
        self, platform: str, handle: str, kind: str
    ) -> List[Dict[str, Any]]:
        """Per-god, per-mode or per-role aggregates for one player.

        `kind` is one of `god`, `gamemode`, `role`. `overview` and `item` return
        "not implemented", as does every `/api/v1/…/profile/…` route.
        """
        body = await self.get_json(
            f"/api/v2/{GAME_SLUG}/standard/profile/{platform}/{handle}/segments/{kind}"
        )
        return body.get("data") or []

    async def page_count(self, platform: str, handle: str, ceiling: int = 4096) -> int:
        """How many pages of history a player has, found by search rather than walk.

        Verified before relying on it: pages are dense below the end and empty
        above — an active player returned 25 matches at `next=256` and nothing
        at 512 — with timestamps decreasing monotonically and no overlap between
        pages. So the count is findable: double until an empty page brackets it,
        then bisect. That is ~18 requests against the 250+ a sequential walk
        would cost for a player with two years of history.

        Each probe abandons its response after the first match, so it costs a
        fraction of the 2.9 MB a full page transfers.
        """

        async def has_matches(page: int) -> bool:
            async for _ in self.iter_matches(platform, handle, page):
                # Breaking closes the generator, which unwinds the streaming
                # context manager and aborts the rest of the download.
                return True
            return False

        if not await has_matches(0):
            return 0

        low, high = 0, 1
        while high < ceiling and await has_matches(high):
            low, high = high, high * 2

        # low has matches, high does not (or is the ceiling).
        while low + 1 < high:
            middle = (low + high) // 2
            if await has_matches(middle):
                low = middle
            else:
                high = middle
        return low + 1


def leaderboard_players(items: Iterable[Dict[str, Any]]) -> List[tuple]:
    """`(platform, identifier)` pairs out of a leaderboard page.

    The identifier is what the match route wants: a steamid64 for steam, a
    handle elsewhere.
    """
    out = []
    for item in items:
        owner = item.get("owner") or {}
        metadata = owner.get("metadata") or {}
        platform = metadata.get("platformSlug")
        identifier = metadata.get("platformUserIdentifier") or metadata.get(
            "platformUserHandle"
        )
        if platform and identifier:
            out.append((str(platform), str(identifier)))
    return out
