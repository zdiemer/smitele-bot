"""Finding how deep a player's history goes, without walking it.

Pages are dense below the end and empty above, so the count is found by
doubling until an empty page brackets it and then bisecting. The cost of
getting this wrong is paid by whoever is waiting on a Discord command, and it
is paid in requests out of an allowance the nightly crawl also draws on.

The property these pin down: a history only ever *grows*. That makes a
previously returned count a correct lower bound rather than a cache that can go
stale, which is what lets the search start near the answer instead of at one.
"""

from __future__ import annotations

import asyncio
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


class Histories:
    """A player with `depth` pages, counting every page actually asked for."""

    def __init__(self, depth: int):
        self.depth = depth
        self.asked: list = []

    def iter_matches(self, _platform, _handle, page=0):
        self.asked.append(page)
        has = page < self.depth

        async def gen():
            if has:
                yield {"attributes": {"id": f"m{page}"}}

        return gen()


def count(depth: int, known: int = 0, ceiling: int = 512):
    client = tracker_client.TrackerClient.__new__(tracker_client.TrackerClient)
    history = Histories(depth)
    client.iter_matches = history.iter_matches
    result = asyncio.run(
        tracker_client.TrackerClient.page_count(
            client, "steam", "someone", ceiling=ceiling, known=known
        )
    )
    return result, history


class TestFromScratch:
    def test_a_player_with_no_history_costs_one_request(self):
        result, history = count(depth=0)
        assert result == 0
        assert history.asked == [0]

    @pytest.mark.parametrize("depth", [1, 2, 7, 25, 100, 257])
    def test_the_count_is_exact(self, depth):
        result, _ = count(depth=depth)
        assert result == depth

    def test_the_ceiling_bounds_a_bottomless_history(self):
        result, history = count(depth=10_000, ceiling=64)
        assert result <= 64
        assert max(history.asked) <= 64


class TestSeeded:
    def test_yesterdays_answer_costs_a_fraction_of_the_probes(self):
        cold, _ = count(depth=260)
        warm, warm_history = count(depth=260, known=260)
        assert cold == warm == 260
        # The whole point: three probes rather than twenty-odd.
        assert len(warm_history.asked) <= 4

    def test_a_history_that_grew_since_is_still_found(self):
        result, _ = count(depth=300, known=260)
        assert result == 300

    def test_a_reset_account_falls_back_rather_than_lying(self):
        """`known` is verified, not trusted — a handle can be reused."""
        result, history = count(depth=3, known=260)
        assert result == 3
        # It asked about the stale lower bound, found nothing, and restarted.
        assert history.asked[0] == 259
        assert 0 in history.asked

    def test_a_reset_to_nothing_returns_zero(self):
        result, _ = count(depth=0, known=100)
        assert result == 0

    def test_an_unchanged_history_confirms_without_bisecting(self):
        result, history = count(depth=42, known=42)
        assert result == 42
        # Page 41 has matches, page 42 does not: the bracket is already tight,
        # so there is nothing left to bisect.
        assert sorted(history.asked) == [41, 42]
