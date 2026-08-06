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
import random
import time
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

import ijson
from curl_cffi import requests as curl_requests

from smite2 import egress
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

# How much of the interval to add back at random. One-sided on purpose: 1.5s is
# the pace a probe sustained without drawing an error and it has no measured
# headroom, so symmetric jitter would put half the gaps under a figure nothing
# has ever cleared. The cost is a slightly slower run, which --budget and
# --hours already express; the gain is that our request train stops arriving on
# a metronome, which is itself a thing worth not looking like.
DEFAULT_JITTER = 0.4

# What a 429 does to the pace for the rest of the run, and where widening stops.
# Resuming at the rate that just earned a 429 only earns another.
BACKOFF_FACTOR = 1.5
MAX_INTERVAL_SECONDS = 15.0

# How many times a single request may be reissued. Unchanged: one retry, for a
# cookie refresh or a flaky 5xx. Cooldowns deliberately do not count against it.
MAX_ATTEMPTS = 2

# A Retry-After longer than this is a ban notice rather than a pause, and a job
# with a wall-clock cap has nothing useful to do while it waits one out.
RETRY_AFTER_CAP_SECONDS = 300
# What to wait when the header is absent, which is the common case.
DEFAULT_COOLDOWN_SECONDS = 60
# 429s tolerated in one run. The first is information — we widen and carry on.
# The fourth means the widened pace is still wrong and the night should end.
MAX_RATE_LIMITS = 3

# __check's verdicts. Deliberately not a bool: a cooldown is neither "retry"
# nor "this response is usable", and collapsing it into either was the bug.
OK: Optional[str] = None
RETRY: str = "retry"
COOLDOWN: str = "cooldown"


class TrackerBlocked(RuntimeError):
    """The WAF refused us. Never retried in a loop — callers should stop."""


class TrackerServerError(RuntimeError):
    """A 5xx that survived a retry. Bad for this route, not for the run."""


class EgressChanged(TrackerBlocked):
    """Our address moved out from under a cookie bound to the old one.

    A rotating proxy, almost always. Kept distinct from TrackerBlocked because
    the remedy is different — the WAF has not refused us, the configuration is
    wrong — and because the alternative to stopping is minting once per request
    until the daily cap trips and arms a four-hour breaker.

    A subclass rather than a sibling so that every existing caller already
    treats it as a stop, flushing what it collected on the way out.
    """


def _cooldown_seconds(retry_after: Optional[str]) -> float:
    """`Retry-After` as seconds.

    Mirrors `wiki_client`, which reads the same header and doubles a local delay
    on each refusal; here the fallback is a constant because there is no
    per-request delay to double. Only the seconds form is parsed — tracker.gg
    sends that, and the HTTP-date form falling through to the default is safe.
    """
    try:
        return max(float(retry_after), 1.0)
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SECONDS


def _retry_after(response: Any) -> Optional[str]:
    try:
        return response.headers.get("Retry-After")
    except AttributeError:
        return None


class RateLimiter:
    """A minimum gap between request starts, shared across workers.

    A gap rather than a concurrency limit, because what matters to a WAF is the
    rate it observes, and two workers each sleeping 1.5 s between their own
    requests produce twice the rate one does.

    The gap is the floor, never the ceiling: jitter only ever adds to it, and a
    429 widens it permanently. Nothing here can make the crawl go faster than it
    was configured to.
    """

    def __init__(
        self,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        jitter: float = DEFAULT_JITTER,
    ):
        self.interval = interval
        self.jitter = jitter
        self.__lock = asyncio.Lock()
        self.__next_at = 0.0
        self.__paused_until = 0.0

    def _gap(self) -> float:
        return self.interval * (1.0 + random.random() * self.jitter)

    async def wait(self) -> None:
        async with self.__lock:
            now = time.monotonic()
            target = max(self.__next_at, self.__paused_until)
            if now < target:
                await asyncio.sleep(target - now)
                now = time.monotonic()
            self.__next_at = now + self._gap()

    def pause(self, seconds: float) -> None:
        """Hold every caller off for `seconds`, without sleeping here.

        Kept in its own field rather than pushed into `__next_at`, because
        `wait` overwrites `__next_at` when it finishes — a caller already
        sleeping through a short gap would stamp the cooldown out of existence
        the moment its own sleep ended.

        No lock: one assignment with no await in it, so nothing can interleave.
        Expressing the cooldown as state rather than sleeping at the call site
        also means it applies to every worker, not just the one that was told.
        """
        self.__paused_until = max(self.__paused_until, time.monotonic() + seconds)

    def widen(
        self,
        factor: float = BACKOFF_FACTOR,
        ceiling: float = MAX_INTERVAL_SECONDS,
    ) -> float:
        """Slow the rest of the run down. Returns the new interval."""
        self.interval = min(self.interval * factor, ceiling)
        return self.interval


def new_session(proxy: Optional[str] = None) -> curl_requests.AsyncSession:
    """The one place an HTTP session is built.

    A factory rather than a constructor call inline, so that everything about
    our network identity is decided in a single place — the impersonation target
    has to stay in agreement with the minted user agent, and the egress has to
    stay in agreement with the address the cookie was solved from.

    `trust_env` is off deliberately. curl_cffi reads HTTPS_PROXY on its own and
    Camoufox does not, so an environment-set proxy would send the crawl through
    one address while the cookie was minted at another and 403 every request —
    the precise failure this module's identity discipline exists to prevent.
    Egress is configured in exactly one way, SMITELE_EGRESS_PROXY, and read in
    exactly one place.
    """
    kwargs = {"impersonate": IMPERSONATE, "trust_env": False}
    if proxy:
        kwargs["proxy"] = proxy
    return curl_requests.AsyncSession(**kwargs)


class TrackerClient:
    """Paced, clearance-carrying access to the routes we actually use."""

    def __init__(
        self,
        clearance: ClearanceManager,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        silent: bool = False,
        jitter: float = DEFAULT_JITTER,
        proxy: Optional[str] = None,
    ):
        self.__clearance = clearance
        self.__limiter = RateLimiter(interval, jitter)
        self.__proxy = egress.proxy_url() if proxy is None else proxy
        self.__session: Optional[curl_requests.AsyncSession] = None
        self.__current: Optional[Clearance] = None
        self.__silent = silent
        self.requests = 0
        self.bytes = 0
        # 429s seen this run. Counted rather than fatal on sight, so one of them
        # costs a cooldown instead of the rest of the night.
        self.rate_limited = 0

    async def __aenter__(self) -> "TrackerClient":
        self.__session = new_session(self.__proxy)
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

    def __check(
        self,
        path: str,
        status: int,
        attempt: int,
        retry_after: Optional[str] = None,
    ) -> Optional[str]:
        """What to do about a response. Raises if the status means stop.

        A 403 is retried exactly once, after discarding the cookie, because the
        overwhelmingly likely cause is expiry. A second 403 means we are
        actually blocked, and the only correct response to that is to stop —
        never to retry into a WAF.

        A 429 is different in kind and used to be treated the same. It is the
        site telling us a rate, not refusing us: the request was answered. So it
        costs a cooldown and a permanently wider pace, and returns COOLDOWN
        rather than RETRY — spending the 403 budget on it would end a run that
        had already recovered, which is precisely what killed a backfill eight
        minutes into six and a half hours.
        """
        if status == 403:
            if attempt == 2:
                raise TrackerBlocked(f"403 twice on {path} — stopping")
            self.__log("403; discarding cookie and minting once")
            if self.__current is not None:
                self.__clearance.invalidate(self.__current)
            return RETRY
        if status == 429:
            return self.__cool_down(path, retry_after)
        if status >= 500:
            # A server error is not a block. Some routes 500 persistently — the
            # SkillRating board does on every parameter set — so retry once and
            # then give up on this path rather than on the run.
            if attempt == 1:
                self.__log(f"HTTP {status} on {path}; one retry")
                return RETRY
            raise TrackerServerError(f"HTTP {status} on {path}")
        if self.__current is not None:
            self.__clearance.mark_ok(self.__current)
        return OK

    async def __verdict(
        self, path: str, status: int, attempt: int, retry_after: Optional[str]
    ) -> Optional[str]:
        """`__check`, with a stop diagnosed before it is reported.

        Split out because `__check` is synchronous and naming a moved address
        costs a round trip. Only ever runs on a path that was going to raise
        anyway, so the extra request is free in every normal run.
        """
        try:
            return self.__check(path, status, attempt, retry_after)
        except TrackerBlocked as blocked:
            raise await self.__diagnose(blocked) from None

    async def __diagnose(self, blocked: TrackerBlocked) -> TrackerBlocked:
        """Tell a rotating proxy apart from a WAF that actually refused us.

        Both look like a wall of 403s from in here, and the remedies are
        opposite: one is "stop crawling", the other is "your proxy tier is
        wrong". Worth one request to say which.
        """
        bound = getattr(self.__current, "observed_ip", "") or ""
        if not bound:
            return blocked
        current = await egress.observed_ip(self.__proxy)
        if current and current != bound:
            return EgressChanged(
                f"our address changed from {bound} to {current} mid-run; the "
                "cookie was bound to the old one. A rotating proxy cannot work "
                "here — the exit has to hold for the life of the cookie."
            )
        return blocked

    def __cool_down(self, path: str, retry_after: Optional[str]) -> str:
        """Stand down for a 429, or decide it is not a pause at all."""
        self.rate_limited += 1
        cooldown = _cooldown_seconds(retry_after)
        if cooldown > RETRY_AFTER_CAP_SECONDS:
            raise TrackerBlocked(
                f"429 on {path} asking for {cooldown:.0f}s — that is a block, "
                "not a pause"
            )
        if self.rate_limited > MAX_RATE_LIMITS:
            raise TrackerBlocked(
                f"429 on {path} for the {self.rate_limited}th time this run — "
                "widening the pace did not help; stopping"
            )
        widened = self.__limiter.widen()
        self.__limiter.pause(cooldown)
        self.__log(
            f"429 on {path} ({self.rate_limited}/{MAX_RATE_LIMITS}); waiting "
            f"{cooldown:.0f}s and pacing at {widened:.2f}s for the rest of the "
            "run"
        )
        return COOLDOWN

    @property
    def interval(self) -> float:
        """The pace in force now, which a 429 may have widened."""
        return self.__limiter.interval

    async def get_json(self, path: str, params: Optional[Dict] = None) -> Any:
        """A whole response, for the small ones — profiles and leaderboards."""
        if self.__session is None:
            raise RuntimeError("TrackerClient used outside its context manager")

        attempt = 1
        while attempt <= MAX_ATTEMPTS:
            await self.__limiter.wait()
            headers = await self.__headers()
            response = await self.__session.get(
                self.url(path), params=params, headers=headers, timeout=120
            )
            self.requests += 1
            verdict = await self.__verdict(
                path, response.status_code, attempt, _retry_after(response)
            )
            if verdict is COOLDOWN:
                # Not a failed attempt — the cooldown is already served by the
                # limiter on the way back round. Bounded by MAX_RATE_LIMITS.
                continue
            if verdict is RETRY:
                attempt += 1
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

        attempt = 1
        while attempt <= MAX_ATTEMPTS:
            await self.__limiter.wait()
            headers = await self.__headers()
            async with self.__session.stream(
                "GET", self.url(path), params=params, headers=headers, timeout=180
            ) as response:
                self.requests += 1
                verdict = await self.__verdict(
                    path, response.status_code, attempt, _retry_after(response)
                )
                if verdict is COOLDOWN:
                    continue
                if verdict is RETRY:
                    attempt += 1
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
