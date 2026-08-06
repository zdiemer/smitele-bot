"""Getting and holding a Cloudflare clearance cookie for tracker.gg.

Both tracker.gg and its API refuse plain HTTP clients — the site serves a
challenge, the API a WAF block — so a real browser has to solve the challenge
once. The cookie it mints is portable: replaying it from `urllib`, with an
entirely different TLS fingerprint, returns 200. So the browser is needed to
*get* clearance, never to use it, which keeps multi-megabyte crawl payloads out
of a Firefox process.

The policy here is deliberately the opposite of the obvious one. Refreshing on a
timer — every fifteen minutes, say — means ~96 challenge solves a day from one
address, which is a solver-farm signature and a worse signal to Cloudflare than
holding a single session. Cloudflare rewards a consistent cookie, user agent and
IP with occasional traffic.

Measurement settles it. One cookie, pinned and polled, served requests
continuously for **6.7 hours** and was still working when the probe was stopped
— the 30 minutes everyone quotes is a Cloudflare default, not this site's
setting. A whole nightly crawl fits inside one clearance with hours to spare, so
there is nothing to pre-warm and the only sane trigger is an observed failure.

So:

  * one clearance is shared by every process, through a file on the shared
    volume, so the nightly crawl and the bot do not each solve their own;
  * nothing refreshes on a schedule — a cookie is replaced only after it is
    observed to have stopped working;
  * a cookie is only ever *replaced* on a successful mint, so a failed refresh
    degrades nothing;
  * concurrent callers that find it stale mint once between them, not once
    each;
  * and a circuit breaker caps solves per day and backs off for hours, not
    seconds, so a block is never hammered.

The cost of all that is latency, not availability: the first lookup after an
expiry waits for a mint. At ~10 seconds, behind a deferred interaction, that is
a slow response rather than an outage.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from smite2 import egress as egress_module

SITE_URL = "https://tracker.gg/smite2"
COOKIE_NAME = "cf_clearance"

# The state file's shape. v1 was a single flat cookie with no notion of where it
# was solved; v2 buckets by egress. See `_migrate`.
SCHEMA_VERSION = 2

# How long a mint is given to get through the challenge before we give up. The
# observed time is ~10s; this is slack for a slow challenge, not a target.
MINT_TIMEOUT_SECONDS = 120

# Solves per rolling day, per egress, across every process sharing the store. A
# measured cookie lifetime of 6.7+ hours means a healthy day needs one or two,
# so this is an order of magnitude of headroom and tripping it means something
# is wrong — an expired-cookie loop, or Cloudflare refusing to issue.
#
# Per egress rather than global because it is a per-address budget in reality:
# solving twelve times from one address is the thing that looks like a solver
# farm, and two addresses each solving twice does not become worse for being
# counted together.
MAX_MINTS_PER_DAY = 12

# How long to stand down after the breaker trips or a mint fails outright.
BACKOFF_SECONDS = 4 * 60 * 60

DAY_SECONDS = 24 * 60 * 60


class ClearanceUnavailable(RuntimeError):
    """No usable cookie, and minting one is not currently allowed."""


class BrowserUnavailable(ClearanceUnavailable):
    """The browser could not be started, so no challenge was ever attempted.

    Kept distinct because the circuit breaker exists to stop us hammering
    Cloudflare, and this failure never reached Cloudflare. Counting a missing
    browser as a refusal put a four-hour backoff on a misconfigured image and
    made a one-line fix look like a rate limit.
    """


@dataclass
class Clearance:
    """A cookie, the user agent it is bound to, and where it was solved.

    All three must line up — Cloudflare ties clearance to the user agent *and*
    the address that solved the challenge, so sending the cookie with a
    different UA, or from a different address, is worse than sending no cookie
    at all.

    `egress` is the bucket this belongs to and is what the store keys on.
    `observed_ip` is the address actually seen at mint time; it is never keyed
    on — a real exit address wobbles, and keying on it would throw away good
    cookies — but recording it turns "every request 403s" into one line naming
    the two addresses that disagreed. Both default, so a payload written before
    they existed still constructs.
    """

    value: str
    user_agent: str
    issued_at: float
    last_ok: float = 0.0
    egress: str = ""
    observed_ip: str = ""

    @property
    def age_seconds(self) -> float:
        return max(time.time() - self.issued_at, 0.0)

    def headers(self) -> dict:
        return {
            "User-Agent": self.user_agent,
            "Cookie": f"{COOKIE_NAME}={self.value}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://tracker.gg",
            "Referer": "https://tracker.gg/",
        }


@dataclass
class _State:
    """One egress's cookie and its budget.

    Unchanged by the move to per-egress buckets, because it was already the
    per-egress unit — which is also how `MAX_MINTS_PER_DAY` and the breaker
    became per-address for free. Both are per-IP budgets in reality and were
    only ever tracked globally by accident.
    """

    clearance: Optional[Clearance] = None
    mints: List[float] = field(default_factory=list)
    blocked_until: float = 0.0


def _clearance_from(payload) -> Optional[Clearance]:
    """A stored cookie, or None if it is not one we recognise.

    Guarded because `load` is on the path of every lookup: an unexpected shape
    should cost one extra solve, not raise out of every command the bot has.
    """
    if not isinstance(payload, dict):
        return None
    try:
        return Clearance(**payload)
    except TypeError:
        return None


def _migrate(raw: dict) -> dict:
    """A v1 document, filed under the egress it must have been solved on.

    v1 recorded one clearance and knew nothing about addresses. It predates any
    proxy here, so it was necessarily minted from the host's own address —
    "direct". Filing it there keeps a working cookie working for an unproxied
    bot and correctly withholds it from a proxied collector, which is the exact
    mistake this schema exists to prevent.

    Rolling back is safe too: v1 code reading a v2 document finds no "clearance"
    key, mints once, and carries on.
    """
    return {
        "version": SCHEMA_VERSION,
        "egress": {
            egress_module.DIRECT: {
                "clearance": raw.get("clearance"),
                "mints": raw.get("mints", []),
                "blocked_until": raw.get("blocked_until", 0.0),
            }
        },
    }


class ClearanceStore:
    """The shared JSON file holding each egress's clearance and mint log.

    Bucketed by egress, because a cookie is bound to the address that solved it:
    an unproxied bot and a proxied collector sharing one file must not hand each
    other a cookie neither can use. Within a bucket this behaves exactly as it
    always did.

    Read before every decision and written after every mint, because the point
    is that separate processes — the bot pod and the nightly collector — see
    each other's work. Losing a race here costs one extra solve, so the file is
    written atomically but not locked.
    """

    def __init__(self, path: str, egress: Optional[str] = None):
        self.path = path
        # Resolved once, here, so every caller in a process agrees on which
        # bucket it is talking about even if the environment changes under it.
        self.egress = egress_module.identity() if egress is None else egress

    def __document(self) -> dict:
        """The whole file, migrated forward, or an empty v2 document."""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return {"version": SCHEMA_VERSION, "egress": {}}
        if not isinstance(raw, dict):
            return {"version": SCHEMA_VERSION, "egress": {}}
        if "egress" not in raw:
            return _migrate(raw)
        return raw

    def load(self) -> _State:
        bucket = (self.__document().get("egress") or {}).get(self.egress) or {}

        clearance = bucket.get("clearance")
        cutoff = time.time() - DAY_SECONDS
        return _State(
            clearance=_clearance_from(clearance),
            mints=[t for t in bucket.get("mints", []) if t > cutoff],
            blocked_until=float(bucket.get("blocked_until", 0.0)),
        )

    def save(self, state: _State) -> None:
        """Replace this egress's bucket, leaving every other one alone.

        Read-modify-write rather than a whole-file overwrite, because the bot
        pod and the collector share this file and may be on different egresses;
        a blind write would drop the other's cookie and cost it a solve. Still
        unlocked, and the race is bounded exactly as it was before — losing one
        costs one extra solve, now possibly in the other bucket.
        """
        document = self.__document()
        document.setdefault("egress", {})[self.egress] = {
            "clearance": asdict(state.clearance) if state.clearance else None,
            "mints": state.mints,
            "blocked_until": state.blocked_until,
        }
        document["version"] = SCHEMA_VERSION

        partial = f"{self.path}.partial"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            os.replace(partial, self.path)
        except OSError as error:
            # A read-only mount is not a reason to fail a lookup that already
            # has a working cookie in hand.
            print(f"clearance: could not persist to {self.path}: {error}", flush=True)


async def mint(
    headless: bool = True,
    timeout: int = MINT_TIMEOUT_SECONDS,
    proxy_url: Optional[str] = None,
) -> Clearance:
    """Drive a browser through the challenge and return what it earned.

    Imported lazily so that anything not minting — the aggregate job, a test —
    does not need Camoufox installed to import this module.

    The browser goes through the same proxy the crawl will, because the cookie
    is bound to the solving address: solving here and replaying from somewhere
    else 403s every request. These are two halves of one identity, exactly like
    the impersonation target and the user agent.
    """
    try:
        from camoufox.async_api import AsyncCamoufox  # noqa: PLC0415
        from camoufox.pkgman import installed_verstr  # noqa: PLC0415

        # An explicit precondition rather than a surprise mid-launch. camoufox
        # locates the browser through XDG_CACHE_HOME, so an image that fetched
        # it under one HOME and runs under another fails here — and that is a
        # deployment mistake, not Cloudflare turning us away.
        installed_verstr()
    except ClearanceUnavailable:
        raise
    except Exception as error:  # noqa: BLE001
        raise BrowserUnavailable(f"no usable browser: {error}") from error

    proxy_url = egress_module.proxy_url() if proxy_url is None else proxy_url
    proxy = egress_module.proxy_dict(proxy_url)
    identity = egress_module.identity(proxy_url)

    # Resolved here rather than left to camoufox's `geoip=True`. Camoufox does
    # geolocate the *proxy* when one is passed, which is the behaviour we want —
    # a fingerprint claiming one country while the address is in another is
    # worse than no proxy — but it does the lookup with a synchronous `requests`
    # walk over six URLs at five seconds each. On a dead proxy that is half a
    # minute of blocked event loop inside the browser launch, which in the bot
    # pod stalls the Discord client. It is also the value we want recorded.
    observed = await egress_module.observed_ip(proxy_url)
    if observed is None and proxy is not None:
        # Cloudflare was never reached, so the breaker must not arm — the same
        # reasoning as BrowserUnavailable, and the same class so that it does.
        raise BrowserUnavailable(f"no route to the internet through {identity}")

    launch = {"headless": headless, "humanize": True, "geoip": observed or True}
    if proxy is not None:
        launch["proxy"] = proxy

    deadline = time.time() + timeout
    async with AsyncCamoufox(**launch) as browser:
        page = await browser.new_page()
        await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
        user_agent = await page.evaluate("navigator.userAgent")

        while time.time() < deadline:
            for cookie in await page.context.cookies():
                if cookie["name"] == COOKIE_NAME:
                    return Clearance(
                        value=cookie["value"],
                        user_agent=user_agent,
                        issued_at=time.time(),
                        egress=identity,
                        observed_ip=observed or "",
                    )
            await asyncio.sleep(2)

    raise ClearanceUnavailable(f"no {COOKIE_NAME} after {timeout}s")


class ClearanceManager:
    """Hands out a clearance, minting only when there is no working one.

    `get()` is what callers use. `invalidate()` is what they call on a 403 — it
    is the *only* thing that causes a refresh, which is the whole policy.
    """

    def __init__(
        self,
        store: ClearanceStore,
        silent: bool = False,
        proxy_url: Optional[str] = None,
    ):
        self.__store = store
        self.__lock = asyncio.Lock()
        self.__silent = silent
        self.__proxy_url = (
            egress_module.proxy_url() if proxy_url is None else proxy_url
        )

    def __log(self, message: str) -> None:
        if not self.__silent:
            print(f"clearance: {message}", flush=True)

    async def get(self, force: bool = False) -> Clearance:
        state = self.__store.load()
        if not force and state.clearance is not None:
            return state.clearance

        # Single-flight: ten commands arriving at once mint one cookie between
        # them. The second waiter re-reads the store and finds the first's work.
        async with self.__lock:
            state = self.__store.load()
            if not force and state.clearance is not None:
                return state.clearance

            now = time.time()
            if now < state.blocked_until:
                remaining = (state.blocked_until - now) / 60
                raise ClearanceUnavailable(
                    f"backing off for another {remaining:.0f} min"
                )

            recent = [t for t in state.mints if t > now - DAY_SECONDS]
            if len(recent) >= MAX_MINTS_PER_DAY:
                state.mints = recent
                state.blocked_until = now + BACKOFF_SECONDS
                self.__store.save(state)
                raise ClearanceUnavailable(
                    f"{len(recent)} mints in 24h hit the cap; standing down for "
                    f"{BACKOFF_SECONDS // 3600}h"
                )

            self.__log(
                f"minting for {self.__store.egress} "
                f"(mint {len(recent) + 1} of {MAX_MINTS_PER_DAY} today)"
            )
            try:
                clearance = await mint(proxy_url=self.__proxy_url)
            except BrowserUnavailable:
                # Never reached Cloudflare, so there is nothing to back off
                # from. Arming the breaker here would turn a broken image into
                # a four-hour outage that looks like a rate limit.
                raise
            except Exception as error:  # noqa: BLE001
                state.mints = recent + [now]
                state.blocked_until = now + BACKOFF_SECONDS
                self.__store.save(state)
                raise ClearanceUnavailable(f"mint failed: {error}") from error

            self.__log(
                f"minted, bound to {clearance.user_agent}"
                + (f" at {clearance.observed_ip}" if clearance.observed_ip else "")
            )
            state.clearance = clearance
            state.mints = recent + [now]
            state.blocked_until = 0.0
            self.__store.save(state)
            return clearance

    def reset(self) -> None:
        """Clear the backoff and the mint log, keeping any working cookie.

        For when the breaker tripped on something that has since been fixed —
        a broken image, a bad deploy — and waiting out four hours would only
        punish the operator. Deliberately manual: nothing calls this on its
        own, because a breaker that resets itself is not a breaker.
        """
        state = self.__store.load()
        self.__log(
            f"reset: clearing {len(state.mints)} recorded mint(s) and any backoff"
        )
        state.mints = []
        state.blocked_until = 0.0
        self.__store.save(state)

    def invalidate(self, used: Clearance) -> None:
        """Discard a cookie that has stopped working.

        No-ops if the store already holds a different cookie, so one worker's
        403 does not throw away the replacement another worker just minted.
        """
        state = self.__store.load()
        if state.clearance is None or state.clearance.value != used.value:
            return
        self.__log(f"discarding cookie after {state.clearance.age_seconds / 60:.0f} min")
        state.clearance = None
        self.__store.save(state)

    def mark_ok(self, used: Clearance) -> None:
        """Record that a cookie just served a request.

        Only useful as an observation — how long a cookie survives in practice
        is the number that decides whether pre-warming is worth it at all.
        """
        state = self.__store.load()
        if state.clearance is None or state.clearance.value != used.value:
            return
        state.clearance.last_ok = time.time()
        self.__store.save(state)
