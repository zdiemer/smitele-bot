import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import time

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/smite2_collector'))
from collect_sources import collect_match
from collector_sources import Sources,NAMES
from source_rows import normalize_source
from prime_rows import InvalidMatch
from test_prime_collector import Catalogue

FIXTURES=Path(__file__).parent/'fixtures/smite2_sources'

def test_source_recorded_scoreboard():
    match=json.loads((FIXTURES/'smitesource_full.json').read_text())
    assets=json.loads((FIXTURES/'smitesource_assets.json').read_text())
    date,rows=normalize_source(match,assets,Catalogue())
    assert date=='2026-10-18'
    assert len(rows)==6
    assert all(r['Source']=='SmiteSource' for r in rows)
    assert sum(r['Win_Status']=='Winner' for r in rows)==3
    assert all(r['UnknownItems']==0 for r in rows)
    match['players'][0]['items'].append(copy.deepcopy(next(i for i in match['players'][0]['items'] if i['c']=='I')))
    with pytest.raises(InvalidMatch):normalize_source(match,assets,Catalogue())

@pytest.mark.asyncio
async def test_bad_scoreboard_falls_back_without_mixing_rows():
    good=json.loads((FIXTURES/'smiteprime_match.json').read_text())
    class Client:
        assets={}
        stats={n:{'rejected':0} for n in NAMES}
        calls=[]
        async def match(self,name,mid):
            self.calls.append(name)
            return good if name=='SmitePrime' else {'players':[]}
    client=Client()
    result,refs=await collect_match(client,good['match_id'],['SmiteSource'],Catalogue())
    assert client.calls==['SmiteSource','SmitePrime']
    assert result[1]=='SmitePrime'
    assert client.stats['SmiteSource']['rejected']==1

@pytest.mark.asyncio
async def test_history_queries_all_three_and_unions_duplicates():
    client=Sources.__new__(Sources)
    client.stats={n:{'discovered':0} for n in NAMES};client.assets={}
    calls=[]
    async def get(name,path,payload=None):
        calls.append((name,path))
        if 'search' in path:return {'results':[{'person_id':'prime-local','players':[{'portal_id':10,'portal_user_id':'123','display_name':'name'}]}]}
        if name=='SmitePrime':return {'results':[{'match':{'match_id':'same'}}]}
        if path=='getPlayerByPlatform':
            assert payload['platformId']=='123'
            return {'playerUuid':'source-local'}
        if name=='SmiteSource':return {'matches':[{'hirezMatchId':'same'},{'hirezMatchId':'source-only'}]}
        return {'matches':[{'attributes':{'id':'same'}}]}
    client.get=get
    found=await client.histories(('xbl','name'))
    assert found['same']==list(NAMES)
    assert 'source-only' in found
    assert {n for n,p in calls}==set(NAMES)

@pytest.mark.asyncio
async def test_persisted_pause_makes_no_network_request(tmp_path):
    client=Sources.__new__(Sources)
    client.pauses={'tracker.gg':time.time()+3600}
    client.stats={'tracker.gg':{'status':'ready'}}
    assert await client.get('tracker.gg','matches/test') is None
    assert client.stats['tracker.gg']['status']=='paused'

@pytest.mark.asyncio
async def test_one_history_outage_does_not_stop_other_sources():
    client=Sources.__new__(Sources)
    client.stats={n:{'discovered':0} for n in NAMES};client.assets={}
    calls=[]
    async def get(name,path,payload=None):
        calls.append(name)
        if name=='tracker.gg':return {'matches':[{'attributes':{'id':'tracker-only'}}]}
        return None
    client.get=get
    assert await client.histories(('steam','123'))=={'tracker-only':['tracker.gg']}
    assert set(calls)==set(NAMES)

@pytest.mark.asyncio
async def test_refusal_is_durable_and_never_retried(tmp_path):
    client=Sources.__new__(Sources)
    client.path=tmp_path/'access.json';client.pauses={};client.clearance=None
    client.stats={n:{'status':'ready','requests':0} for n in NAMES}
    client.requests=client.previous=0;client.interval=0;client.budget=10;client.deadline=time.monotonic()+100
    class Transport:
        calls=0
        async def get(self,*args,**kwargs):
            self.calls+=1
            return SimpleNamespace(status_code=403,headers={})
    client.tracker=Transport()
    await client.get('tracker.gg','matches/test')
    await client.get('tracker.gg','matches/test')
    assert client.tracker.calls==1
    assert json.loads(client.path.read_text())['tracker.gg']>time.time()


def test_tracker_complete_roster_and_missing_identity():
    from test_smite2_rows import match
    from source_rows import normalize_tracker
    body=match([],mode='joust')
    body['attributes']['id']='3fec05f5-ae61-4b54-bade-a8acb87e1914'
    body['segments']=[copy.deepcopy(body['segments'][0]) for _ in range(6)]
    for i,row in enumerate(body['segments']):
        row['attributes']['platformUserIdentifier']=str(i+1)
        row['metadata']['teamId']='order' if i<3 else 'chaos'
    _,rows=normalize_tracker(body,Catalogue())
    assert len(rows)==6 and rows[0]['Source']=='tracker.gg'
    body['segments'][0]['attributes']['platformUserIdentifier']=None
    with pytest.raises(InvalidMatch):normalize_tracker(body,Catalogue())

@pytest.fixture
def retry_client(tmp_path, monkeypatch):
    import collector_sources as module
    clock = SimpleNamespace(now=1000., sleeps=[])
    async def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(module.time, 'time', lambda: clock.now)
    monkeypatch.setattr(module.random, 'uniform', lambda low, high: (low+high)/2)
    client = Sources.__new__(Sources)
    client.path = tmp_path/'access.json'
    client.pauses = {}; client.clearance = None
    client.stats = {n:dict(status='ready', requests=0) for n in NAMES}
    client.requests = client.previous = 0
    client.interval = 12; client.budget = 20; client.deadline = 5000
    class Transport:
        outcomes = []
        calls = 0
        async def get(self, *args, **kwargs):
            self.calls += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException): raise outcome
            return outcome
    client.public = client.tracker = Transport()
    return client, clock


def response(code=200, headers=None, body=None):
    return SimpleNamespace(status_code=code, headers=headers or {},
                           json=lambda: {'json': {'players': []}} if body is None else body)

@pytest.mark.asyncio
async def test_timeout_and_503_recover_with_exponential_jitter(retry_client):
    client, clock = retry_client
    client.public.outcomes = [TimeoutError(), response(503), response()]
    assert await client.get('SmiteSource', 'getMatch') == {'players': []}
    assert clock.sleeps == [0, 18, 0, 36, 0]
    assert client.requests == 3
    assert client.stats['SmiteSource']['retries'] == 2
    assert client.stats['SmiteSource']['status'] == 'active'

@pytest.mark.asyncio
async def test_exhaustion_temporarily_cools_only_one_source(retry_client):
    client, clock = retry_client
    client.public.outcomes = [TimeoutError() for _ in range(4)]
    assert await client.get('SmiteSource', 'getMatch') is None
    assert client.requests == 4
    assert await client.get('SmiteSource', 'getMatch') is None
    assert client.requests == 4
    client.public.outcomes = [response(body={'match_id': 'prime'}), response()]
    assert await client.get('SmitePrime', 'matches-data/matches/id/') == {'match_id': 'prime'}
    clock.now = client.stats['SmiteSource']['resume_at']+1
    assert await client.get('SmiteSource', 'getMatch') == {'players': []}

@pytest.mark.asyncio
async def test_retry_obeys_budget_and_deadline(retry_client):
    from collect_prime import BudgetSpent
    client, clock = retry_client
    client.budget = 1
    client.public.outcomes = [TimeoutError()]
    with pytest.raises(BudgetSpent): await client.get('SmiteSource', 'getMatch')
    assert client.requests == 1
    client.budget = 20; client.deadline = clock.now+5
    client.public.outcomes = [TimeoutError()]
    with pytest.raises(BudgetSpent): await client.get('SmiteSource', 'getMatch')
    assert client.requests == 1  # Even normal pacing cannot cross the deadline.

@pytest.mark.asyncio
async def test_rate_limit_http_date_and_later_recovery(retry_client):
    from email.utils import formatdate
    client, clock = retry_client
    client.public.outcomes = [response(429, {'Retry-After': formatdate(clock.now+120, usegmt=True)}), response()]
    assert await client.get('SmiteSource', 'getMatch') is None
    assert client.pauses['SmiteSource'] >= 1120
    assert await client.get('SmiteSource', 'getMatch') is None
    assert client.requests == 1
    clock.now = client.pauses['SmiteSource']+1
    assert await client.get('SmiteSource', 'getMatch') == {'players': []}

@pytest.mark.asyncio
async def test_permanent_error_and_cancellation_are_not_retried(retry_client):
    client, clock = retry_client
    client.public.outcomes = [response(400), asyncio.CancelledError()]
    assert await client.get('SmiteSource', 'getMatch') is None
    assert client.requests == 1
    with pytest.raises(asyncio.CancelledError): await client.get('SmiteSource', 'getMatch')
    assert client.requests == 2

@pytest.mark.asyncio
async def test_server_retry_after_is_a_minimum(retry_client):
    client, clock = retry_client
    client.public.outcomes = [response(503, {'Retry-After': '90'}), response()]
    await client.get('SmiteSource', 'getMatch')
    assert 90 in clock.sleeps
