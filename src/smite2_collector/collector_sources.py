"""Three public sources with independent durable refusal handling, no solves."""
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import quote
from curl_cffi import requests
from smite2 import egress, cooldown
from smite2.clearance import ClearanceStore
from smite2.provider import CLEARANCE_FILE
from smite2.smiteprime import exact_person
from smite2.smitesource import PLATFORM_SLUGS
from smite2.tracker_client import new_session
from collect_prime import BudgetSpent
from prime_rows import PORTALS

NAMES = ('SmitePrime', 'SmiteSource', 'tracker.gg')

class Sources:
    def __init__(self, state, budget, interval, deadline):
        self.path = Path(state)/'collector_source_access.json'
        self.pauses = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.budget, self.interval, self.deadline = budget, interval, deadline
        self.requests, self.previous = 0, 0
        self.stats = {n:dict(requests=0, discovered=0, persisted=0, rejected=0, status='ready') for n in NAMES}
        self.assets = {}
        self.refs = set()
        self.clearance = ClearanceStore(str(Path(state)/CLEARANCE_FILE)).load().clearance
        standdown = cooldown.Cooldown(str(Path(state)/cooldown.FILE_NAME)).read()
        if standdown.active:
            self.pauses['tracker.gg'] = max(self.pauses.get('tracker.gg',0),time.time()+standdown.remaining)

    async def __aenter__(self):
        self.public = requests.AsyncSession(impersonate='firefox', trust_env=False)
        self.tracker = new_session(egress.proxy_url())
        await self.public.__aenter__()
        await self.tracker.__aenter__()
        return self

    async def __aexit__(self,*args):
        await self.public.__aexit__(*args)
        await self.tracker.__aexit__(*args)

    async def get(self, name, path, payload=None):
        stat = self.stats[name]
        if self.pauses.get(name,0)>time.time():
            stat['status']='paused'
            return None
        if stat['status']=='unavailable':return None
        if self.requests>=self.budget:raise BudgetSpent()
        await asyncio.sleep(max(0,self.previous+self.interval-time.monotonic()))
        if time.monotonic()>=self.deadline:raise BudgetSpent()
        self.requests+=1; stat['requests']+=1; self.previous=time.monotonic()
        headers={'Accept':'application/json'}
        params=None
        if name=='SmitePrime':url='https://smiteprime.com/api/'+path
        elif name=='SmiteSource':
            url='https://smitesource.com/rpc/matches/'+path
            headers['x-smitesource-client']='1'
            params={'data':json.dumps({'json':payload})}
        else:
            url='https://api.tracker.gg/api/v2/smite2/standard/'+path
            if self.clearance:headers=self.clearance.headers()
        try:
            response=await (self.tracker if name=='tracker.gg' else self.public).get(url,params=params,headers=headers,timeout=25)
            if response.status_code in (403,429):
                try:delay=max(86400,float(response.headers.get('Retry-After',0)))
                except ValueError:delay=86400
                self.pauses[name]=time.time()+delay
                temp=self.path.with_suffix('.partial'); temp.write_text(json.dumps(self.pauses)); temp.replace(self.path)
                stat['status']='paused'; stat['http_status']=response.status_code
                return None
            if response.status_code==404:return None
            if response.status_code!=200:raise ValueError('HTTP failure')
            body=response.json()
            body=body['json'] if name=='SmiteSource' else body.get('data') if name=='tracker.gg' else body
            if body is None:return None
            if not isinstance(body,dict):raise ValueError('unexpected envelope')
            expected=('results' if name=='SmitePrime' and path.startswith('player-data/') else
                      'matches' if path=='getPlayerMatches' or (name=='tracker.gg' and path.count('/')==2) else None)
            if expected and not isinstance(body.get(expected),list):raise ValueError('unexpected history')
            stat['status']='active'
            return body
        except Exception as error:
            stat['status']='unavailable'; stat['error']=type(error).__name__
            return None

    async def histories(self, ref):
        """Query every source before detail collection; bridge only verified accounts."""
        platform, account = ref
        found={}
        self.refs=set()
        prime=await self.get('SmitePrime',f'player-data/players/search/{quote(account,safe="")}/?page=1')
        if prime:
            try:identity=exact_person(prime,platform,account)
            except RuntimeError:identity=None
            if identity:
                account=str(identity['platform_id'])
                body=await self.get('SmitePrime',f'player-data/players/{identity["person_id"]}/matches/?page=1')
                for row in (body or {}).get('results',[]):
                    mid=(row.get('match') or {}).get('match_id')
                    if mid:found.setdefault(mid,[]).append('SmitePrime')
        resolved=await self.get('SmiteSource','getPlayerByPlatform',{'platform':PLATFORM_SLUGS[platform],'platformId':account})
        if resolved and resolved.get('playerUuid'):
            body=await self.get('SmiteSource','getPlayerMatches',{'playerUuid':resolved['playerUuid'],'page':1,'pageSize':25})
            self.assets.update(((body or {}).get('assetMaps') or {}).get('itemMetaByMasterId') or {})
            for row in (body or {}).get('matches',[]):
                if row.get('hirezMatchId'):found.setdefault(row['hirezMatchId'],[]).append('SmiteSource')
                for player in (row.get('team1Players') or [])+(row.get('team2Players') or []):
                    route=player.get('preferredRoute') or {}
                    reverse={v:k for k,v in PLATFORM_SLUGS.items()}
                    if route.get('platform') in reverse and route.get('platformId'):
                        self.refs.add((reverse[route['platform']],str(route['platformId'])))
        body=await self.get('tracker.gg',f'matches/{platform}/{quote(account,safe="")}')
        for row in (body or {}).get('matches',[]):
            mid=(row.get('attributes') or {}).get('id')
            if mid:found.setdefault(mid,[]).append('tracker.gg')
        for providers in found.values():
            for name in providers:self.stats[name]['discovered']+=1
        return found

    async def match(self,name,mid):
        if name=='SmitePrime':return await self.get(name,f'matches-data/matches/{mid}/')
        if name=='SmiteSource':return await self.get(name,'getMatch',{'matchId':mid})
        return await self.get(name,f'matches/{mid}')


def references(name, match):
    if name=='tracker.gg':
        for row in match.get('segments',[]):
            a=row.get('attributes') or {}
            if a.get('platformSlug') in PORTALS.values() and a.get('platformUserIdentifier'):
                yield a['platformSlug'],str(a['platformUserIdentifier'])
    else:
        for row in match.get('players',[]):
            accounts=[row.get('player') or {}] if name=='SmitePrime' else (row.get('smitePlayer') or {}).get('platforms',[])
            for a in accounts:
                portal=a.get('portal_id') if name=='SmitePrime' else a.get('portalId')
                account=a.get('portal_user_id') if name=='SmitePrime' else a.get('portalUserId')
                if portal in PORTALS and account:yield PORTALS[portal],str(account)
