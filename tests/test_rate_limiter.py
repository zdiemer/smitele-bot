"""The gap between requests, which is the only thing pacing the crawl.

Run against a fake clock rather than real sleeps, so the whole file is
instantaneous and the assertions are about the arithmetic rather than about
whether the machine was busy.

The property under all of it: the configured interval is a floor. Jitter only
adds, `widen` only slows, and neither can produce a gap shorter than what was
asked for — because 1.5s is a figure that was observed to work, not one with
measured headroom under it.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "HirezAPI"),
)

pytest.importorskip("curl_cffi")
pytest.importorskip("ijson")
tracker_client = pytest.importorskip("smite2.tracker_client")

import asyncio  # noqa: E402


# Captured before the fixture replaces asyncio.sleep, so the fake can still
# yield to the event loop and the tests can still let another task run.
real_sleep = asyncio.sleep


class Clock:
    """A monotonic clock that only moves when something sleeps.

    The yield happens *before* the clock advances, so that a task parked in
    `sleep` is genuinely parked — otherwise nothing else ever observes the
    in-between state and a concurrency test is not testing concurrency.
    """

    def __init__(self, start: float = 1_000.0):
        self.now = start
        self.slept: list = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await real_sleep(0)
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(tracker_client.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(tracker_client.asyncio, "sleep", fake.sleep)
    return fake


def pin_random(monkeypatch, value: float) -> None:
    monkeypatch.setattr(tracker_client.random, "random", lambda: value)


class TestJitter:
    def test_the_gap_is_never_shorter_than_the_interval(self, clock, monkeypatch):
        """The floor property, at both ends of the jitter range."""
        for draw in (0.0, 1.0):
            pin_random(monkeypatch, draw)
            limiter = tracker_client.RateLimiter(interval=1.5, jitter=0.4)
            asyncio.run(limiter.wait())  # first call never waits
            before = clock.now
            asyncio.run(limiter.wait())
            assert clock.now - before >= 1.5

    def test_jitter_only_ever_widens(self, clock, monkeypatch):
        pin_random(monkeypatch, 1.0)
        limiter = tracker_client.RateLimiter(interval=2.0, jitter=0.5)
        asyncio.run(limiter.wait())
        before = clock.now
        asyncio.run(limiter.wait())
        # 2.0 * (1 + 1.0 * 0.5) == 3.0, the top of the range.
        assert clock.now - before == pytest.approx(3.0)

    def test_the_gap_is_not_always_the_same(self, clock):
        """A perfectly uniform train is a signature; vary it."""
        limiter = tracker_client.RateLimiter(interval=1.5, jitter=0.4)
        gaps = []
        for _ in range(20):
            before = clock.now
            asyncio.run(limiter.wait())
            gaps.append(clock.now - before)
        assert len(set(gaps[1:])) > 1

    def test_zero_jitter_is_the_old_behaviour(self, clock):
        limiter = tracker_client.RateLimiter(interval=1.5, jitter=0.0)
        asyncio.run(limiter.wait())
        before = clock.now
        asyncio.run(limiter.wait())
        assert clock.now - before == pytest.approx(1.5)


class TestPause:
    def test_a_pause_defers_the_next_request(self, clock, monkeypatch):
        pin_random(monkeypatch, 0.0)
        limiter = tracker_client.RateLimiter(interval=1.5, jitter=0.0)
        asyncio.run(limiter.wait())

        limiter.pause(60.0)
        before = clock.now
        asyncio.run(limiter.wait())
        assert clock.now - before == pytest.approx(60.0)

    def test_a_pause_survives_a_waiter_that_is_already_sleeping(
        self, clock, monkeypatch
    ):
        """The reason the cooldown is its own field.

        `wait` overwrites `__next_at` when it completes. If the cooldown lived
        there, a caller that was already sleeping through its ordinary gap would
        wake, stamp `now + interval` over the top, and erase a sixty-second
        stand-down — resuming instantly into the WAF that just refused us.
        """
        pin_random(monkeypatch, 0.0)
        limiter = tracker_client.RateLimiter(interval=1.5, jitter=0.0)
        asyncio.run(limiter.wait())

        async def scenario():
            waiter = asyncio.ensure_future(limiter.wait())
            await real_sleep(0)  # let it park inside its own gap
            assert clock.slept, "the waiter should be sleeping by now"
            limiter.pause(60.0)
            await waiter
            before = clock.now
            await limiter.wait()
            return clock.now - before

        assert asyncio.run(scenario()) >= 55.0

    def test_the_longer_of_two_pauses_wins(self, clock):
        limiter = tracker_client.RateLimiter(interval=1.5, jitter=0.0)
        limiter.pause(60.0)
        limiter.pause(10.0)
        before = clock.now
        asyncio.run(limiter.wait())
        assert clock.now - before == pytest.approx(60.0)


class TestWiden:
    def test_widening_sticks(self, clock, monkeypatch):
        pin_random(monkeypatch, 0.0)
        limiter = tracker_client.RateLimiter(interval=2.0, jitter=0.0)
        assert limiter.widen(factor=1.5) == pytest.approx(3.0)

        asyncio.run(limiter.wait())
        before = clock.now
        asyncio.run(limiter.wait())
        assert clock.now - before == pytest.approx(3.0)

    def test_widening_stops_at_the_ceiling(self):
        limiter = tracker_client.RateLimiter(interval=10.0, jitter=0.0)
        for _ in range(10):
            limiter.widen(factor=2.0, ceiling=15.0)
        assert limiter.interval == pytest.approx(15.0)

    def test_widening_never_speeds_up(self):
        limiter = tracker_client.RateLimiter(interval=1.5, jitter=0.0)
        assert limiter.widen() > 1.5


class TestSharedLimiter:
    """One address, one gap — however many clients are built around it.

    The bot constructs a `TrackerClient` per command. A limiter built inside the
    client has never issued a request, so its first `wait()` returns at once,
    and a burst of commands paced itself at zero however low the interval was
    configured. Sharing one limiter is what makes the gap a property of the
    address rather than of whichever command happened to ask.
    """

    def test_a_supplied_limiter_is_used_instead_of_a_new_one(self):
        limiter = tracker_client.RateLimiter(interval=9.0, jitter=0.0)
        client = tracker_client.TrackerClient(
            clearance=None, interval=1.5, limiter=limiter
        )
        assert client.interval == pytest.approx(9.0)

    def test_without_one_each_client_paces_independently(self, clock, monkeypatch):
        """The behaviour being fixed, pinned so a regression is visible."""
        pin_random(monkeypatch, 0.0)
        first = tracker_client.RateLimiter(interval=5.0, jitter=0.0)
        second = tracker_client.RateLimiter(interval=5.0, jitter=0.0)

        asyncio.run(first.wait())
        before = clock.now
        # A brand-new limiter has no history, so it does not wait at all.
        asyncio.run(second.wait())
        assert clock.now == before

    def test_sharing_one_makes_the_second_caller_wait(self, clock, monkeypatch):
        pin_random(monkeypatch, 0.0)
        limiter = tracker_client.RateLimiter(interval=5.0, jitter=0.0)
        one = tracker_client.TrackerClient(clearance=None, limiter=limiter)
        two = tracker_client.TrackerClient(clearance=None, limiter=limiter)
        assert one.interval == two.interval == pytest.approx(5.0)

        asyncio.run(limiter.wait())
        before = clock.now
        asyncio.run(limiter.wait())
        assert clock.now - before == pytest.approx(5.0)

    def test_a_widen_on_one_client_slows_them_all(self, monkeypatch):
        """A 429 answers the address, not the command that drew it."""
        limiter = tracker_client.RateLimiter(interval=2.0, jitter=0.0)
        one = tracker_client.TrackerClient(clearance=None, limiter=limiter)
        two = tracker_client.TrackerClient(clearance=None, limiter=limiter)

        limiter.widen(factor=2.0)
        assert one.interval == two.interval == pytest.approx(4.0)
