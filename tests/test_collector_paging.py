"""Walking a player's history backwards, and knowing when to stop.

A nightly run reads one page — 25 matches, about three days — because it ran
yesterday too. Backfilling a month means paging past that, and every way of
getting the stop condition wrong is expensive rather than loud: pages past the
end return an empty list rather than an error, so a missing check spends the
whole budget fetching nothing, and a missing horizon pages a retired account
back to the beginning of time.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "src", "smite2_collector"),
)

pytest.importorskip("pandas", reason="pandas not installed")
collect = pytest.importorskip("collect", reason="collector deps not installed")


def match(match_id, date):
    """The two fields the crawl reads before deciding anything."""
    return {
        "attributes": {"id": str(match_id)},
        "metadata": {"timestamp": f"{date}T12:00:00+00:00"},
        "segments": [],
    }


class Client:
    """Serves canned pages and counts what was asked for."""

    def __init__(self, pages):
        self.pages = pages
        self.asked = []
        self.requests = 0

    def iter_matches(self, _platform, _handle, page):
        self.asked.append(page)
        self.requests += 1
        entries = self.pages[page] if page < len(self.pages) else []

        async def gen():
            for entry in entries:
                yield entry

        return gen()


class Seen:
    def __init__(self):
        self.ids = set()

    def __contains__(self, key):
        return key in self.ids

    def add(self, key, _date):
        self.ids.add(key)


def visit(client, *, pages, horizon=None, budget=10_000):
    args = types.SimpleNamespace(
        pages=pages, horizon=horizon, budget=budget, dry_run=True, quiet=True
    )
    tracker = types.SimpleNamespace(observe=lambda *a: None)
    buffer = types.SimpleNamespace(add=lambda *a: None)
    player = types.SimpleNamespace(platform="steam", handle="someone", key="steam:x")
    return asyncio.run(
        collect._visit(client, player, {}, Seen(), tracker, buffer, args)
    )


class TestPageDepth:
    def test_one_page_is_the_default_shape(self):
        """The nightly run must be untouched by any of this."""
        client = Client([[match(1, "2026-08-06")], [match(2, "2026-08-01")]])
        found, fresh, _counts, _parties, _found_players = visit(client, pages=1)

        assert client.asked == [0]
        assert (found, fresh) == (1, 1)

    def test_it_walks_back_through_the_requested_pages(self):
        client = Client([
            [match(1, "2026-08-06")],
            [match(2, "2026-08-03")],
            [match(3, "2026-07-31")],
        ])
        found, fresh, _c, _p, _d = visit(client, pages=3)

        assert client.asked == [0, 1, 2]
        assert (found, fresh) == (3, 3)

    def test_it_stops_at_the_end_of_a_short_history(self):
        """Past the last page tracker.gg returns zero results rather than an
        error. Without this check a backfill spends its whole budget on
        players who have barely played."""
        client = Client([[match(1, "2026-08-06")]])
        visit(client, pages=10)

        assert client.asked == [0, 1], "should stop after the first empty page"

    def test_it_stops_once_the_history_predates_the_window(self):
        """A retired account would otherwise be paged back for years."""
        client = Client([
            [match(1, "2026-08-06")],
            [match(2, "2026-07-01")],
            [match(3, "2026-06-01")],
        ])
        visit(client, pages=10, horizon="2026-07-06")

        assert client.asked == [0, 1], "page 1 is already past the horizon"

    def test_the_page_that_crosses_the_horizon_is_still_kept(self):
        """It is half inside the window; dropping it would lose those days."""
        client = Client([
            [match(1, "2026-08-06"), match(2, "2026-07-01")],
        ])
        found, fresh, _c, _p, _d = visit(client, pages=10, horizon="2026-07-06")

        assert (found, fresh) == (2, 2)

    def test_it_respects_the_request_budget_mid_player(self):
        """The budget is a hard ceiling, not a per-player one."""
        client = Client([[match(i, "2026-08-06")] for i in range(10)])
        visit(client, pages=10, budget=2)

        assert client.asked == [0, 1]

    def test_a_duplicate_match_is_counted_but_not_rewritten(self):
        """Deep pages of two players in the same match overlap heavily."""
        client = Client([[match(1, "2026-08-06"), match(1, "2026-08-06")]])
        found, fresh, _c, _p, _d = visit(client, pages=1)

        assert (found, fresh) == (2, 1)


class TestRevisit:
    """A nightly run skips anyone read today — a second read of their most
    recent page returns what the first one did. A backfill reads pages nobody
    has, so the guard would hand it an empty roster and it would do nothing.
    """

    @staticmethod
    def frontier_with_one_player_read_today(tmp_path):
        import frontier as frontier_module

        frontier = frontier_module.Frontier(str(tmp_path))
        frontier.add("steam", "someone", "2026-08-06")
        player = next(iter(frontier.players.values()))
        frontier.record_visit(player, "2026-08-06", 25, 25)
        return frontier

    def test_a_nightly_run_skips_them(self, tmp_path):
        frontier = self.frontier_with_one_player_read_today(tmp_path)
        assert frontier.select(100, "2026-08-06") == []

    def test_a_backfill_reaches_them(self, tmp_path):
        frontier = self.frontier_with_one_player_read_today(tmp_path)
        assert len(frontier.select(100, "2026-08-06", revisit=True)) == 1

    def test_revisit_still_takes_one_player_per_party(self, tmp_path):
        """Premade suppression is not a recency rule and must survive."""
        import frontier as frontier_module

        frontier = frontier_module.Frontier(str(tmp_path))
        for handle in ("a", "b"):
            frontier.add("steam", handle, "2026-08-06")
        for player in frontier.players.values():
            frontier.record_visit(player, "2026-08-06", 25, 25)
        frontier.note_parties({"party-1": {"steam:a", "steam:b"}})

        assert len(frontier.select(100, "2026-08-06", revisit=True)) == 1
