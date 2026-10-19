"""Three public sources with independent durable refusal handling, no solves."""
import asyncio
import json
import random
from email.utils import parsedate_to_datetime
import time
from pathlib import Path
from curl_cffi import requests
from smite2 import egress, cooldown
from smite2.clearance import ClearanceStore
CLEARANCE_FILE = "clearance.json"
from smite2.tracker_client import new_session
class BudgetSpent(Exception):
    pass


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


class SourceTransport:
    def __init__(self, state, budget, interval, deadline):
        self.path = Path(state)/'collector_source_access.json'
        self.pauses = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.budget, self.interval, self.deadline = budget, interval, deadline
        self.requests = 0
        self.previous_by_source = {name: 0 for name in NAMES}
        self.locks = {name: asyncio.Lock() for name in NAMES}
        self.jitter = 0.4
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
        try:
            for source, until in json.loads(self.path.read_text()).items():
                self.pauses[source] = max(self.pauses.get(source, 0), until)
        except (OSError, ValueError):
            pass
        self.pauses[name] = max(self.pauses.get(name, 0), time.time()+delay)
        temp = self.path.with_suffix('.partial')
        try:
            temp.write_text(json.dumps(self.pauses))
            temp.replace(self.path)
        except OSError:
            pass  # Bot snapshot mounts are read-only; in-memory pause still applies.

    async def get(self, name, path, payload=None):
        async with self.locks[name]:
            return await self._get(name, path, payload)

    async def _get(self, name, path, payload=None):
        stat = self.stats[name]
        try:
            persisted = json.loads(self.path.read_text())
            self.pauses[name] = max(self.pauses.get(name, 0), persisted.get(name, 0))
        except (OSError, ValueError):
            pass
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
            await self.wait(max(0, self.previous_by_source[name]+self.interval-time.monotonic()))
            self.requests += 1
            stat['requests'] += 1
            self.previous_by_source[name] = time.monotonic()+random.uniform(0, self.interval*self.jitter)
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
                if code in (204, 404):
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
                            list_response = ('/career/' in path or path in ('getPlayerGodStats','getPlayerRoleStats') or '/segments/' in path)
                            if not isinstance(body, list if list_response else dict):
                                raise ValueError('unexpected envelope')
                            expected = ('results' if name == 'SmitePrime' and path.startswith('player-data/') and ('/search/' in path or '/matches/' in path) else
                                        'matches' if path == 'getPlayerMatches' or (name == 'tracker.gg' and path.startswith('matches/') and path.count('/') == 2) else None)
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

