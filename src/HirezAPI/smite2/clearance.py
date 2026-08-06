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
IP with occasional traffic. So:

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

SITE_URL = "https://tracker.gg/smite2"
COOKIE_NAME = "cf_clearance"

# How long a mint is given to get through the challenge before we give up. The
# observed time is ~10s; this is slack for a slow challenge, not a target.
MINT_TIMEOUT_SECONDS = 120

# Solves per rolling day across every process sharing the store. Well above
# what the no-timer policy should ever need, so tripping it means something is
# wrong — an expired-cookie loop, or Cloudflare refusing to issue.
MAX_MINTS_PER_DAY = 12

# How long to stand down after the breaker trips or a mint fails outright.
BACKOFF_SECONDS = 4 * 60 * 60

DAY_SECONDS = 24 * 60 * 60


class ClearanceUnavailable(RuntimeError):
    """No usable cookie, and minting one is not currently allowed."""


@dataclass
class Clearance:
    """A cookie and the user agent it is bound to.

    Both must be replayed together — Cloudflare ties clearance to the user agent
    (and the address) that solved the challenge, so sending the cookie with a
    different UA is worse than sending no cookie at all.
    """

    value: str
    user_agent: str
    issued_at: float
    last_ok: float = 0.0

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
    clearance: Optional[Clearance] = None
    mints: List[float] = field(default_factory=list)
    blocked_until: float = 0.0


class ClearanceStore:
    """The shared JSON file holding the current clearance and the mint log.

    Read before every decision and written after every mint, because the point
    is that separate processes — the bot pod and the nightly collector — see
    each other's work. Losing a race here costs one extra solve, so the file is
    written atomically but not locked.
    """

    def __init__(self, path: str):
        self.path = path

    def load(self) -> _State:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return _State()

        clearance = raw.get("clearance")
        cutoff = time.time() - DAY_SECONDS
        return _State(
            clearance=Clearance(**clearance) if clearance else None,
            mints=[t for t in raw.get("mints", []) if t > cutoff],
            blocked_until=float(raw.get("blocked_until", 0.0)),
        )

    def save(self, state: _State) -> None:
        payload = {
            "clearance": asdict(state.clearance) if state.clearance else None,
            "mints": state.mints,
            "blocked_until": state.blocked_until,
        }
        partial = f"{self.path}.partial"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(partial, self.path)
        except OSError as error:
            # A read-only mount is not a reason to fail a lookup that already
            # has a working cookie in hand.
            print(f"clearance: could not persist to {self.path}: {error}", flush=True)


async def mint(headless: bool = True, timeout: int = MINT_TIMEOUT_SECONDS) -> Clearance:
    """Drive a browser through the challenge and return what it earned.

    Imported lazily so that anything not minting — the aggregate job, a test —
    does not need Camoufox installed to import this module.
    """
    from camoufox.async_api import AsyncCamoufox  # noqa: PLC0415

    deadline = time.time() + timeout
    async with AsyncCamoufox(headless=headless, geoip=True, humanize=True) as browser:
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
                    )
            await asyncio.sleep(2)

    raise ClearanceUnavailable(f"no {COOKIE_NAME} after {timeout}s")


class ClearanceManager:
    """Hands out a clearance, minting only when there is no working one.

    `get()` is what callers use. `invalidate()` is what they call on a 403 — it
    is the *only* thing that causes a refresh, which is the whole policy.
    """

    def __init__(self, store: ClearanceStore, silent: bool = False):
        self.__store = store
        self.__lock = asyncio.Lock()
        self.__silent = silent

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

            self.__log(f"minting (mint {len(recent) + 1} of {MAX_MINTS_PER_DAY} today)")
            try:
                clearance = await mint()
            except Exception as error:  # noqa: BLE001
                state.mints = recent + [now]
                state.blocked_until = now + BACKOFF_SECONDS
                self.__store.save(state)
                raise ClearanceUnavailable(f"mint failed: {error}") from error

            self.__log(f"minted, bound to {clearance.user_agent}")
            state.clearance = clearance
            state.mints = recent + [now]
            state.blocked_until = 0.0
            self.__store.save(state)
            return clearance

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
