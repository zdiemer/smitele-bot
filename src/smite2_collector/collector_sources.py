"""Three public sources with independent durable refusal handling, no solves."""
import asyncio
import json
import random
from email.utils import parsedate_to_datetime
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

def retry_after(value):
    """Seconds or HTTP date; malformed headers fall back to local backoff."""
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp()-time.time()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, delay) if delay < float('inf') else 0


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

    async def wait(self, delay):
        # Never sleep beyond the run's deadline or issue an over-budget retry.
        if self.requests >= self.budget or time.monotonic()+delay >= self.deadline:
            raise BudgetSpent()
        await asyncio.sleep(delay)
        if time.monotonic() >= self.deadline:
            raise BudgetSpent()

    def pause(self, name, delay):
        self.pauses[name] = time.time()+delay
        temp = self.path.with_suffix('.partial')
        temp.write_text(json.dumps(self.pauses))
        temp.replace(self.path)

    async def get(self, name, path, payload=None):
        stat = self.stats[name]
        if self.pauses.get(name, 0) > time.time():
            stat['status'] = 'paused'
            return None
        if stat.get('resume_at', 0) > time.time():
            stat['status'] = 'cooling_down'
            return None
        # Log the operation, never account identifiers, cookies or proxy URLs.
        operation = (path if name == 'SmiteSource' else
                     'search' if '/search/' in path else
                     'history' if '/players/' in path or path.count('/') == 2 else 'match')
        headers = {'Accept': 'application/json'}
        params = None
        if name == 'SmitePrime':
            url = 'https://smiteprime.com/api/'+path
        elif name == 'SmiteSource':
            url = 'https://smitesource.com/rpc/matches/'+path
            headers['x-smitesource-client'] = '1'
            params = {'data': json.dumps({'json': payload})}
        else:
            url = 'https://api.tracker.gg/api/v2/smite2/standard/'+path
            if self.clearance:
                headers = self.clearance.headers()
        for attempt in range(4):
            await self.wait(max(0, self.previous+self.interval-time.monotonic()))
            self.requests += 1
            stat['requests'] += 1
            self.previous = time.monotonic()
            stat['operation'] = operation
            stat['attempt'] = attempt+1
            response = None
            try:
                response = await (self.tracker if name == 'tracker.gg' else self.public).get(
                    url, params=params, headers=headers, timeout=min(25, self.deadline-time.monotonic()))
            except (requests.RequestsError, TimeoutError, OSError) as error:
                failure = type(error).__name__
            else:
                code = response.status_code
                stat['http_status'] = code
                if code in (403, 429):
                    # Blocks are not transient. Rate limits honor either form of
                    # Retry-After, then add jitter without retrying early.
                    delay = max(86400 if code == 403 else 60,
                                retry_after(response.headers.get('Retry-After')))
                    delay += random.uniform(0, min(30, delay/10))
                    self.pause(name, delay)
                    stat.update(status='paused', resume_at=self.pauses[name])
                    print(f'{name} {operation}: HTTP {code}; paused {delay:.1f}s', flush=True)
                    return None
                if code == 404:
                    stat['status'] = 'active'
                    return None
                if code not in (408, 425) and not 500 <= code <= 599:
                    if code != 200:
                        stat.update(status='request_failed', error=f'HTTP {code}')
                        return None
                    try:
                        body = response.json()
                        body = body['json'] if name == 'SmiteSource' else body.get('data') if name == 'tracker.gg' else body
                        if body is not None:
                            if not isinstance(body, dict):
                                raise ValueError('unexpected envelope')
                            expected = ('results' if name == 'SmitePrime' and path.startswith('player-data/') else
                                        'matches' if path == 'getPlayerMatches' or (name == 'tracker.gg' and path.count('/') == 2) else None)
                            if expected and not isinstance(body.get(expected), list):
                                raise ValueError('unexpected history')
                    except (ValueError, KeyError, TypeError, AttributeError) as error:
                        # A schema problem is not repaired by repeating a request.
                        stat.update(status='invalid_response', error=type(error).__name__)
                        return None
                    stat.update(status='active', resume_at=0)
                    stat.pop('error', None)
                    return body
                failure = f'HTTP {code}'
            stat['error'] = failure
            if attempt == 3:
                delay = random.uniform(300, 600)
                stat.update(status='cooling_down', resume_at=time.time()+delay)
                stat['exhausted'] = stat.get('exhausted', 0)+1
                print(f'{name} {operation}: {failure}; attempts exhausted, cooling down {delay:.1f}s', flush=True)
                return None
            ceiling = min(120, 24 * 2**attempt)
            delay = random.uniform(ceiling/2, ceiling)
            if response is not None:
                delay = max(delay, retry_after(response.headers.get('Retry-After')))
            stat.update(status='retrying', retry_delay=delay)
            stat['retries'] = stat.get('retries', 0)+1
            print(f'{name} {operation}: {failure}; retry {attempt+1}/3 in {delay:.1f}s', flush=True)
            await self.wait(delay)

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
