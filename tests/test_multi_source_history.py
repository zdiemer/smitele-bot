"""Recorded public response shapes and orchestration failure boundaries."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from smite2 import history, smiteprime, smitesource

FIXTURES = Path(__file__).parent / "fixtures" / "smite2_sources"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def match(match_id="3fec05f5-ae61-4b54-bade-a8acb87e1914", source="SmiteSource", **kwargs):
    row = history.summary(
        match_id=match_id, source=source, mode="casual_joust",
        timestamp="2026-10-18T21:17:42.847Z", duration=1204, won=True,
        god="ratatoskr", god_name="Ratatoskr", role="Solo", kills=8, deaths=2,
        assists=10, rating=None, starting_rating=None,
    )
    return replace(row, **kwargs)


def test_merging_is_by_game_uuid_and_never_adds_stats():
    first = match()
    second = match(source="SmitePrime", kills=99, skill_rating=0, skill_rating_delta=0)
    newer = match("37e7026c-4e13-4a1f-a12b-26d4975276d6", "SmitePrime",
                  timestamp="2026-10-18T21:39:29.110000Z")
    rows = history.merge_histories([[first], [second, newer]], 10)
    assert len(rows) == 2
    assert rows[0].match_id == newer.match_id
    assert rows[1].kills == 8
    assert rows[1].skill_rating == 0
    assert rows[1].sources == ("SmiteSource", "SmitePrime")
    assert first.skill_rating is None  # Cached source objects were not mutated.


def test_same_timestamp_does_not_mean_same_match():
    assert len(history.merge_histories([[match(), match("37e7026c-4e13-4a1f-a12b-26d4975276d6")]], 10)) == 2


def test_prime_identity_is_platform_scoped_and_not_a_shared_uuid():
    body = fixture("smiteprime_search.json")
    identity = smiteprime.exact_person(body, "xbl", "juicytot6298")
    assert identity == {"person_id": "a8b158a1-5872-4b2c-aa39-7c9064728c0a", "platform_id": "2535431543554562"}
    with pytest.raises(smiteprime.SmitePrimeUnavailable):
        smiteprime.exact_person(body, "steam", "juicytot6298")
    body["results"].append(dict(body["results"][0], person_id="different-person"))
    with pytest.raises(smiteprime.SmitePrimeUnavailable):
        smiteprime.exact_person(body, "xbl", "juicytot6298")


def test_truncated_name_search_is_not_unique():
    body = fixture("smiteprime_search.json")
    body["next"] = "page2"
    with pytest.raises(smiteprime.SmitePrimeUnavailable):
        smiteprime.exact_person(body, "xbl", "juicytot6298")
    assert smiteprime.exact_person(body, "xbl", "2535431543554562")


async def test_prime_recorded_history_uses_own_row_and_match_duration(monkeypatch):
    client = smiteprime.SmitePrimeClient()
    async def get(path):
        return fixture("smiteprime_history.json")
    monkeypatch.setattr(client, "get", get)
    rows = await client.recent_matches({"person_id": "a8b158a1-5872-4b2c-aa39-7c9064728c0a"}, 10)
    assert rows[0].kills == 5
    assert rows[0].duration == 1424
    assert rows[0].won is False
    assert rows[0].sources == ("SmitePrime",)
    with pytest.raises(smiteprime.SmitePrimeUnavailable):
        await client.recent_matches({"person_id": "wrong-person"}, 10)


async def test_source_recorded_history_and_current_split(monkeypatch):
    async def rpc(self, procedure, payload):
        assert procedure == "matches.getPlayerMatches"
        assert "split" not in payload  # Follow the current split, never pin an old season.
        return fixture("smitesource_history.json")
    monkeypatch.setattr(smitesource.SmiteSourceClient, "rpc", rpc)
    rows = await history.source_history("xbl", "247b8c65-8ec4-4f01-bfac-2a3007056bcd", 10)
    assert rows[0].kills == 8
    assert rows[0].won is True
    assert rows[0].sources == ("SmiteSource",)


class Tracker:
    def __init__(self):
        self.calls = 0
    def __call__(self):
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def iter_matches(self, *args):
        self.calls += 1
        if False:
            yield


async def test_partial_failure_and_simultaneous_requests(monkeypatch):
    tracker = Tracker()
    lookups = history.MultiSourcePlayerLookups(tracker, silent=True)
    calls = []
    async def source(*args):
        calls.append("source")
        await asyncio.sleep(.01)
        return [match()]
    async def prime(*args):
        raise RuntimeError("outage")
    monkeypatch.setattr(lookups, "_source_history", source)
    monkeypatch.setattr(lookups, "_prime_history", prime)
    a, b = await asyncio.gather(*[lookups.recent_matches("xbl", "player", 10) for _ in range(2)])
    assert a == b == [match()]
    assert calls == ["source"]
    assert tracker.calls == 1
    assert await lookups.recent_matches("xbl", "player", 10) == a
    assert tracker.calls == 1


async def test_enough_records_avoid_tracker_and_zero_limit_is_free(monkeypatch):
    tracker = Tracker()
    lookups = history.MultiSourcePlayerLookups(tracker, silent=True)
    async def source(*args):
        return [match()]
    monkeypatch.setattr(lookups, "_source_history", source)
    monkeypatch.setattr(lookups, "_prime_history", source)
    assert len(await lookups.recent_matches("xbl", "player", 1)) == 1
    assert await lookups.recent_matches("xbl", "player", 0) == []
    assert tracker.calls == 0


async def test_all_failures_are_not_cached_as_no_matches(monkeypatch):
    lookups = history.MultiSourcePlayerLookups(None, silent=True)
    async def fail(*args):
        raise TimeoutError()
    monkeypatch.setattr(lookups, "_source_history", fail)
    monkeypatch.setattr(lookups, "_prime_history", fail)
    with pytest.raises(smitesource.SmiteSourceUnavailable):
        await lookups.recent_matches("xbl", "player", 10)
    assert not lookups._history_cache
    assert not lookups._pending


async def test_name_bridge_passes_platform_id_not_prime_person_id(monkeypatch):
    lookups = history.MultiSourcePlayerLookups(None, silent=True)
    calls = []
    async def source(platform, handle, limit):
        calls.append(handle)
        if handle == "player":
            raise smitesource.SmiteSourceUnavailable()
        return [match()]
    async def identity(*args):
        return {"platform_id": "2535431543554562", "person_id": "not-a-source-uuid"}
    monkeypatch.setattr(history, "source_history", source)
    monkeypatch.setattr(lookups, "_prime_identity", identity)
    assert await lookups._source_history("xbl", "player", 10)
    assert calls == ["player", "2535431543554562"]


def test_conflicting_ratings_do_not_produce_a_fabricated_delta():
    rows = history.merge_histories([
        [match(skill_rating=100)],
        [match(source="SmitePrime", skill_rating=200, skill_rating_delta=20)],
    ], 10)
    assert rows[0].skill_rating == 100
    assert rows[0].skill_rating_delta is None


async def test_timeout_keeps_other_sources_usable(monkeypatch):
    tracker = Tracker()
    lookups = history.MultiSourcePlayerLookups(tracker, silent=True)
    async def slow(*args):
        await asyncio.sleep(10)
    async def good(*args):
        return [match()]
    monkeypatch.setattr(history, "SOURCE_TIMEOUT", .01)
    monkeypatch.setattr(lookups, "_source_history", slow)
    monkeypatch.setattr(lookups, "_prime_history", good)
    assert await lookups.recent_matches("xbl", "player", 1) == [match()]
    assert tracker.calls == 0


async def test_required_source_header_and_new_identity_route(monkeypatch):
    client = smitesource.SmiteSourceClient()
    class Session:
        async def get(self, url, **kwargs):
            assert url.endswith('/rpc/matches/getPlayerByPlatform')
            assert kwargs['headers']['x-smitesource-client'] == '1'
            assert json.loads(kwargs['params']['data']) == {
                'json': {'platform': 'xbox', 'platformId': '2535431543554562'}
            }
            class Response:
                status_code = 200
                content = b'{"json":{"playerUuid":"247b8c65-8ec4-4f01-bfac-2a3007056bcd"}}'
            return Response()
    client._SmiteSourceClient__session = Session()
    assert await client.player_uuid('xbl', '2535431543554562') == '247b8c65-8ec4-4f01-bfac-2a3007056bcd'


@pytest.mark.parametrize('body', [{}, [], {'error': 'changed schema'}])
async def test_malformed_live_answer_is_not_a_fresh_no(monkeypatch, body):
    client = smitesource.SmiteSourceClient()
    async def rpc(*args):
        return body
    monkeypatch.setattr(client, 'rpc', rpc)
    with pytest.raises(smitesource.SmiteSourceUnavailable):
        await client.live_match_id('player')
