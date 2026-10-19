import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from smite2.multi_source import MultiSourceLookups, segment
from smite2.transport import NAMES

FIX=Path(__file__).parent/'fixtures/smite2_sources'

class FakeTransport:
    def __init__(self, replies):
        self.replies=replies
        self.stats={n:{'status':'active'} for n in NAMES}
        self.calls=[]
    async def get(self,name,path,payload=None):
        self.calls.append((name,path,payload))
        value=self.replies.get((name,path))
        if isinstance(value,Exception):raise value
        return value


def test_prime_aggregate_preserves_mode_and_scope():
    rows=json.loads((FIX/'prime_career.json').read_text())
    row=segment('casual_joust','Joust',rows[0],'SmitePrime','Recorded career matches',prime=True)
    assert row.key=='casual_joust'
    assert row.matches==743 and row.wins==422
    assert row.kda==pytest.approx((4229+4870/2)/2624)
    assert row.metadata['scope']=='Recorded career matches'

@pytest.mark.asyncio
async def test_source_god_variants_reconcile_without_cross_source_sum():
    overview=json.loads((FIX/'source_overview.json').read_text())
    gods=json.loads((FIX/'source_gods.json').read_text())
    api=MultiSourceLookups(transport=FakeTransport({('SmiteSource','getPlayerOverview'):overview,
        ('SmiteSource','getPlayerByPlatform'):{'playerUuid':'source'},('SmiteSource','getPlayerGodStats'):gods}))
    rows=await api.source_segments('xbl','123','god')
    assert len(rows)>50
    assert sum(r.matches for r in rows)==overview['totals']['totalMatches']
    assert len({r.key for r in rows})==len(rows)
    assert all(r.metadata['source']=='SmiteSource' for r in rows)

@pytest.mark.asyncio
async def test_fast_source_does_not_wait_for_other_sources_retry():
    api=MultiSourceLookups()
    started=asyncio.Event(); cancelled=asyncio.Event()
    async def slow():
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    async def fast():
        await started.wait()
        return ['Prime answer']
    assert await asyncio.wait_for(api.choose([slow(),fast()]),1)==['Prime answer']
    assert cancelled.is_set()

@pytest.mark.asyncio
async def test_numeric_prime_identity_uses_verified_account_not_fuzzy_name():
    search=json.loads((FIX/'smiteprime_search.json').read_text())
    api=MultiSourceLookups(transport=FakeTransport({
        ('SmitePrime','player-data/players/search/2535431543554562/?page=1'):{'results':[]},
        ('SmiteSource','getPlayerByPlatform'):{'playerUuid':'source'},
        ('SmiteSource','getPlayerOverview'):{'displayName':'juicytot6298'},
        ('SmitePrime','player-data/players/search/juicytot6298/?page=1'):search}))
    identity=await api.identity('xbl','2535431543554562')
    assert str(identity['platform_id'])=='2535431543554562'

@pytest.mark.asyncio
async def test_recent_history_reads_all_three_and_deduplicates(monkeypatch):
    from test_multi_source_history import match
    api=MultiSourceLookups();calls=[]
    async def history(name,*args):
        calls.append(name)
        return [match(source=name)]
    monkeypatch.setattr(api,'history',history)
    rows=await api.recent_matches('xbl','player')
    assert set(calls)==set(NAMES)
    assert len(rows)==1 and set(rows[0].sources)==set(NAMES)

@pytest.mark.asyncio
async def test_live_negative_does_not_hide_other_sources_positive(monkeypatch):
    api=MultiSourceLookups()
    async def live(name,*args):
        if name=='SmiteSource':return None
        if name=='tracker.gg':raise RuntimeError('blocked')
        return 'Prime live lobby'
    monkeypatch.setattr(api,'live',live)
    assert await api.live_match('xbl','player')=='Prime live lobby'

@pytest.mark.asyncio
async def test_failed_live_lookup_is_not_cached_as_not_live(monkeypatch):
    api=MultiSourceLookups()
    async def live(*args):raise RuntimeError('unavailable')
    monkeypatch.setattr(api,'live',live)
    assert await api.live_match('xbl','player') is None
    assert ('live','xbl','player') not in api.cache
