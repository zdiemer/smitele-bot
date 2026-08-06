"""What survives when the crawl is stopped part-way through.

This is the property that makes a mid-run refusal cheap. The 429 handling in
`tracker_client` only pays off if the rows collected before it are already safe
— otherwise a stop still costs the night, which was exactly the outcome of a
backfill that died eight minutes in.

Nothing pinned this before, and it is easy to break: the persistence block sits
*outside* the `async with TrackerClient(...)`, and moving it inside would look
tidier and silently discard every row on any abort.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "HirezAPI"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "smite2_collector"))

pytest.importorskip("pandas")
pytest.importorskip("curl_cffi")
pytest.importorskip("ijson")
collect = pytest.importorskip("collect")

from smite2.tracker_client import TrackerBlocked  # noqa: E402


class God:
    def __init__(self, identifier: int, name: str):
        self.id = identifier
        self.name = name


class Provider:
    """Just enough of Smite2Provider for `crawl` to get past its god check."""

    def __init__(self, *args, **kwargs):
        self.gods = {1: God(1, "Anubis"), 2: God(2, "Ymir")}

    async def create(self):
        return None

    def god_by_name(self, name):
        for god in self.gods.values():
            if god.name == name:
                return god
        return None


def match(match_id: str, date: str = "2026-08-05T12:00:00Z"):
    """One match in the shape `rows.player_rows` expects.

    Segments are `overview`, the god and team live in segment metadata, and the
    winner is named on the match — the same shape the real route returns.
    """
    return {
        "attributes": {"id": match_id, "gamemode": "Conquest"},
        "metadata": {
            "timestamp": date,
            "winningTeamId": "order",
            "isRanked": False,
        },
        "segments": [
            {
                "type": "overview",
                "attributes": {
                    "platformSlug": "steam",
                    "platformUserIdentifier": handle,
                },
                "metadata": {
                    "god": "Anubis",
                    "teamId": "order" if index < 5 else "chaos",
                    "playedRole": {"key": "mid"},
                    "items": [],
                },
                "stats": {},
            }
            for index, handle in enumerate(
                f"7656119800000{index:04d}" for index in range(10)
            )
        ],
    }


class Client:
    """Serves one good page, then refuses the way a 429 now does."""

    def __init__(self, *args, **kwargs):
        self.requests = 0
        self.bytes = 0
        self.rate_limited = 1
        self.interval = 2.25
        self.pages_served = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_json(self, path, params=None):
        return {"data": {"items": []}}

    async def iter_matches(self, platform, handle, page=0):
        self.requests += 1
        if self.pages_served >= 1:
            raise TrackerBlocked("429 on /matches — stopping")
        self.pages_served += 1
        for index in range(3):
            yield match(f"{handle}-{page}-{index}")


@pytest.fixture
def crawling(tmp_path, monkeypatch):
    corpus = tmp_path / "output"
    state = tmp_path / "state"
    corpus.mkdir()
    state.mkdir()

    monkeypatch.setattr(collect, "Smite2Provider", Provider)
    monkeypatch.setattr(collect, "TrackerClient", Client)
    # No network from the test suite: the start/end address sample would
    # otherwise reach an IP echo, and a run with no address to report is the
    # ordinary unproxied case anyway.
    monkeypatch.setattr(collect.egress_module, "observed_ip", _no_address)
    monkeypatch.setattr(collect.paths, "game_match_data_dir", lambda game: str(corpus))
    monkeypatch.setattr(collect.paths, "game_model_dir", lambda game: str(state))
    # Seeding hits the leaderboards; the frontier is populated directly instead.
    monkeypatch.setattr(collect, "seed", _seed_two_players)
    return corpus, state


async def _no_address(url=None, timeout=10.0):
    return None


async def _seed_two_players(client, frontier, today, silent):
    for handle in ("76561198000000001", "76561198000000002"):
        frontier.add("steam", handle, today)
    return 2


def args(**overrides):
    base = dict(
        budget=100,
        hours=1.0,
        interval=1.5,
        jitter=0.0,
        coverage_target=0.0,
        pages=1,
        since_days=0,
        horizon=None,
        revisit=True,
        flush_every=50_000,
        dry_run=False,
        quiet=True,
        reset_clearance=False,
        reset_cooldown=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class TestAbortPersists:
    def test_a_block_still_writes_the_rows_it_collected(self, crawling):
        """The whole point: a refusal costs a pause, not the night."""
        corpus, _ = crawling

        code = asyncio.run(collect.crawl(args()))

        assert code == 2, "a blocked run reports failure"
        written = [name for name in os.listdir(str(corpus)) if name.endswith(".parquet")]
        assert written, "rows collected before the block were dropped"

    def test_a_block_still_records_what_it_saw(self, crawling):
        """`seen` and the frontier must persist too.

        If the rows land but `seen` does not, the next run refetches them and
        counts them as new; if the frontier does not, the snowball forgets every
        player the aborted run discovered.
        """
        _, state = crawling

        asyncio.run(collect.crawl(args()))

        saved = set(os.listdir(str(state)))
        assert "seen_matches.parquet" in saved
        assert "frontier.parquet" in saved

    def test_a_clean_run_reports_success(self, crawling, monkeypatch):
        """The control: exit 0 when nothing refused us."""
        monkeypatch.setattr(Client, "iter_matches", _never_blocks)

        assert asyncio.run(collect.crawl(args())) == 0


async def _never_blocks(self, platform, handle, page=0):
    self.requests += 1
    for index in range(3):
        yield match(f"{handle}-{page}-{index}")


class TestStandDown:
    """A run must not fire into a ban the last one was told to serve."""

    def standdown_at(self, state, seconds, reason="429 asking for 3600s"):
        from smite2 import cooldown as cooldown_module

        cooldown_module.Cooldown(
            os.path.join(str(state), cooldown_module.FILE_NAME)
        ).arm(seconds, reason)

    def test_a_live_standdown_stops_the_run_before_any_work(
        self, crawling, monkeypatch
    ):
        """Refused before the session *and* before the catalogue load.

        The check sits ahead of the wiki fetch on purpose: nothing about a
        stand-down needs the god index, and a run that is not going to happen
        should cost nothing at all.
        """
        corpus, state = crawling
        self.standdown_at(state, 3600)

        touched = []
        monkeypatch.setattr(
            collect, "TrackerClient", lambda *a, **k: touched.append("client")
        )
        monkeypatch.setattr(
            collect, "Smite2Provider", lambda *a, **k: touched.append("provider")
        )

        code = asyncio.run(collect.crawl(args()))

        assert code == 3, "a stand-down is its own outcome, not a crawl failure"
        assert touched == [], f"a banned run did work anyway: {touched}"
        assert not [n for n in os.listdir(str(corpus)) if n.endswith(".parquet")]

    def test_an_elapsed_standdown_does_not_stop_anything(self, crawling):
        _, state = crawling
        self.standdown_at(state, 0)
        assert asyncio.run(collect.crawl(args())) == 2, "the crawl ran and blocked"

    def test_reset_cooldown_overrides_it(self, crawling):
        _, state = crawling
        self.standdown_at(state, 3600)
        assert asyncio.run(collect.crawl(args(reset_cooldown=True))) == 2
