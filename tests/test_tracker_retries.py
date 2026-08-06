"""What the client does about a refusal, and what it costs.

The behaviour these pin down is the difference between a 429 costing a minute
and a 429 costing the rest of a six-and-a-half-hour backfill. A real run died
eight minutes in because a single 429 raised straight out of the crawl; a 429
now widens the pace, stands down, and carries on.

The rule underneath: a 429 is the site telling us a rate, and the request was
answered. A 403 is the site refusing us. Only the second may spend the one retry
a request gets.
"""

from __future__ import annotations

import asyncio
import json
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

BODY = json.dumps({"data": {"items": [{"hello": "world"}]}}).encode()


class Response:
    def __init__(self, status: int, retry_after=None, content: bytes = BODY):
        self.status_code = status
        self.content = content
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


class Session:
    """Serves a scripted sequence of responses and records what it was asked."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.asked = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, params=None, headers=None, timeout=None):
        self.asked += 1
        if not self.responses:
            raise AssertionError(f"unscripted request {self.asked} to {url}")
        return self.responses.pop(0)


class Clearance:
    value = "cookie"
    user_agent = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"

    def headers(self):
        return {"Cookie": f"cf_clearance={self.value}"}


class Manager:
    def __init__(self):
        self.invalidated = 0
        self.ok = 0

    async def get(self, force: bool = False):
        return Clearance()

    def invalidate(self, used):
        self.invalidated += 1

    def mark_ok(self, used):
        self.ok += 1


@pytest.fixture
def instant(monkeypatch):
    """No real sleeping, and a limiter whose waits are free."""

    async def sleep(seconds):
        return None

    monkeypatch.setattr(tracker_client.asyncio, "sleep", sleep)
    return None


def client_for(session, monkeypatch, **kwargs):
    monkeypatch.setattr(tracker_client, "new_session", lambda *a, **k: session)
    return tracker_client.TrackerClient(Manager(), silent=True, **kwargs)


def run(session, monkeypatch, **kwargs):
    client = client_for(session, monkeypatch, **kwargs)

    async def scenario():
        async with client:
            return await client.get_json("/api/v1/whatever")

    return client, asyncio.run(scenario())


class TestRateLimit:
    def test_a_429_with_retry_after_cools_down_and_resumes(
        self, instant, monkeypatch
    ):
        session = Session(Response(429, retry_after="30"), Response(200))
        client, body = run(session, monkeypatch)

        assert body == json.loads(BODY)
        assert session.asked == 2
        assert client.rate_limited == 1
        # And the pace it resumed at is slower than the one that earned the 429.
        assert client.interval > tracker_client.DEFAULT_INTERVAL_SECONDS

    def test_a_429_without_the_header_uses_the_default_cooldown(
        self, instant, monkeypatch
    ):
        session = Session(Response(429), Response(200))
        client, body = run(session, monkeypatch)
        assert body == json.loads(BODY)
        assert client.rate_limited == 1

    def test_a_retry_after_beyond_the_cap_is_fatal(self, instant, monkeypatch):
        """An hour is not a pause, it is a ban notice."""
        session = Session(Response(429, retry_after="3600"))
        with pytest.raises(tracker_client.TrackerBlocked, match="that is a block"):
            run(session, monkeypatch)

    def test_the_fourth_429_stops_the_run(self, instant, monkeypatch):
        session = Session(*[Response(429) for _ in range(6)])
        with pytest.raises(tracker_client.TrackerBlocked, match="did not help"):
            run(session, monkeypatch)
        assert session.asked == tracker_client.MAX_RATE_LIMITS + 1

    def test_repeated_cooldowns_keep_widening_up_to_the_ceiling(
        self, instant, monkeypatch
    ):
        session = Session(Response(429), Response(429), Response(200))
        client, _ = run(session, monkeypatch)
        assert client.rate_limited == 2
        assert client.interval == pytest.approx(
            tracker_client.DEFAULT_INTERVAL_SECONDS
            * tracker_client.BACKOFF_FACTOR**2
        )


class TestAttemptAccounting:
    def test_a_429_does_not_spend_the_403_retry(self, instant, monkeypatch):
        """The regression test for what actually killed the backfill.

        A cooldown is not a failed attempt. When it counted as one, a 429
        followed by an ordinary cookie expiry exhausted the loop and raised
        "gave up" — ending a run that had already recovered from both.
        """
        session = Session(Response(429), Response(403), Response(200))
        client, body = run(session, monkeypatch)

        assert body == json.loads(BODY)
        assert session.asked == 3
        assert client.rate_limited == 1

    def test_two_403s_still_stop(self, instant, monkeypatch):
        session = Session(Response(403), Response(403))
        with pytest.raises(tracker_client.TrackerBlocked, match="403 twice"):
            run(session, monkeypatch)

    def test_one_403_still_refreshes_the_cookie_and_recovers(
        self, instant, monkeypatch
    ):
        session = Session(Response(403), Response(200))
        client = client_for(session, monkeypatch)

        async def scenario():
            async with client:
                return await client.get_json("/api/v1/whatever")

        assert asyncio.run(scenario()) == json.loads(BODY)

    def test_a_5xx_gives_up_on_the_path_not_the_run(self, instant, monkeypatch):
        session = Session(Response(500), Response(500))
        with pytest.raises(tracker_client.TrackerServerError):
            run(session, monkeypatch)

    def test_a_5xx_still_retries_once(self, instant, monkeypatch):
        session = Session(Response(500), Response(200))
        _, body = run(session, monkeypatch)
        assert body == json.loads(BODY)


class TestSession:
    def test_the_session_ignores_the_environment(self, monkeypatch):
        """`trust_env=False` is what stops HTTPS_PROXY splitting our identity.

        curl_cffi reads HTTPS_PROXY on its own and Camoufox does not, so an
        environment-set proxy would crawl through one address while the cookie
        was minted at another — 403ing every request for a reason that looks
        nothing like its cause.
        """
        captured = {}

        class Session:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(
            tracker_client.curl_requests, "AsyncSession", Session
        )
        monkeypatch.setenv("HTTPS_PROXY", "http://not-ours.example:3128")

        tracker_client.new_session()
        assert captured["trust_env"] is False
        assert "proxy" not in captured

    def test_the_session_carries_the_configured_proxy(self, monkeypatch):
        captured = {}

        class Session:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(
            tracker_client.curl_requests, "AsyncSession", Session
        )
        tracker_client.new_session("http://bot:hunter2@gate.example.net:8080")
        assert captured["proxy"] == "http://bot:hunter2@gate.example.net:8080"
        assert captured["impersonate"] == tracker_client.IMPERSONATE


class TestBudget:
    def test_every_attempt_counts_as_a_request(self, instant, monkeypatch):
        """A 429 was a real request and must come out of --budget.

        Otherwise a run that keeps getting rate limited never notices it is
        making no progress.
        """
        session = Session(Response(429), Response(403), Response(200))
        client, _ = run(session, monkeypatch)
        assert client.requests == 3
