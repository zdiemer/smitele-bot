"""The application's player surface: independent providers, explicit stat scopes.

Totals are selected as a complete provider response, never added across sites.
Missing capabilities fall back to another site. Match histories join by game UUID.
"""
import asyncio
import time
from urllib.parse import quote
from dataclasses import replace

import paths
from game import Game
from smite2.transport import SourceTransport, NAMES
from smite2 import smitesource, smiteprime
from smite2.history import merge_histories, summary
from smite2.players import Segment, LiveMatch, LivePlayer, _segment, _live_player, _summarise


class Unavailable(RuntimeError):
    pass


def segment(key, name, row, source, scope, *, prime=False):
    """Normalize counts; compute KDA consistently rather than trusting site ratios."""
    count = int(row['matches_played'] if prime else row.get('totalMatches', row.get('gamesPlayed', 0)))
    wins, losses = int(row['wins']), int(row['losses'])
    if min(count, wins, losses) < 0 or count != wins+losses:
        raise ValueError('inconsistent aggregate counts')
    def total(key, prime_key, average=None):
        value = row.get(prime_key if prime else key)
        if value is None and average and average in row:
            value = round(row[average]*count)
        return value
    stats = dict(matchesPlayed=count, matchesWon=wins, matchesLost=losses,
                 matchesWinPct=100*wins/count if count else 0)
    for metric, value in {
        'kills':total('totalKills','kills','avgKills'),
        'deaths':total('totalDeaths','deaths','avgDeaths'),
        'assists':total('totalAssists','assists','avgAssists'),
        'damage':total('totalDamage','total_damage'),
        'goldEarned':total('totalGold','total_gold_earned'),
        'timePlayed':total('totalDurationSeconds','total_duration_seconds'),
    }.items():
        if value is not None:stats[metric]=value
    kills, deaths, assists = (stats.get(k,0) for k in ('kills','deaths','assists'))
    stats['kdaRatio']=(kills+assists/2)/(deaths or 1)
    for k in ('kills','deaths','assists'):stats[k+'PerMatch']=stats.get(k,0)/(count or 1)
    display={k:f'{v:,.2f}' if isinstance(v,float) else f'{v:,}' for k,v in stats.items()}
    if 'timePlayed' in stats:display['timePlayed']=f"{stats['timePlayed']/3600:,.1f}h"
    return Segment(key,name,None,stats,display,{'source':source,'scope':scope,'isRanked':key.startswith('ranked_')})


class MultiSourceLookups:
    def __init__(self, silent=False, transport=None):
        self.transport=transport
        self.silent=silent
        self.cache={};self.pending={};self.identities={}
        self.open_lock=asyncio.Lock()

    async def start(self):
        async with self.open_lock:
            if self.transport is None:
                transport=SourceTransport(paths.game_model_dir(Game.SMITE_2),float('inf'),1.5,float('inf'))
                await transport.__aenter__()
                self.transport=transport
        return self.transport

    async def close(self):
        tasks=list(self.pending.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.transport is not None:
            await self.transport.__aexit__(None,None,None)
            self.transport=None

    async def get(self,name,path,payload=None):
        client=await self.start()
        body=await client.get(name,path,payload)
        if body is None and client.stats[name]['status']!='active':
            raise Unavailable(f'{name} {client.stats[name]["status"]}')
        return body

    async def cached(self,key,operation,ttl=900):
        entry=self.cache.get(key)
        if entry and time.monotonic()-entry[0]<ttl:return entry[1]
        if key not in self.pending:
            async def load():
                try:
                    value=await operation()
                    self.cache[key]=(time.monotonic(),value)
                    return value
                finally:self.pending.pop(key,None)
            self.pending[key]=asyncio.create_task(load())
        return await asyncio.shield(self.pending[key])

    async def identity(self,platform,handle):
        async def resolve():
            body=await self.get('SmitePrime',f'player-data/players/search/{quote(handle,safe="")}/?page=1')
            try:return smiteprime.exact_person(body,platform,handle)
            except smiteprime.SmitePrimeUnavailable:
                if not handle.isdigit():raise
                # Prime's search indexes names, not every numeric portal ID.
                # Discover a name through the verified Source account, then
                # match the original portal ID in Prime's search results.
                overview=await self.source_overview(platform,handle)
                name=overview.get('displayName')
                if not name:raise Unavailable('No verified account bridge')
                body=await self.get('SmitePrime',f'player-data/players/search/{quote(name,safe="")}/?page=1')
                return smiteprime.exact_person(body,platform,handle)
        return await self.cached(('identity',platform,handle),resolve)

    async def source_id(self,platform,handle):
        async def resolve():
            account=handle
            if not handle.isdigit():
                try:account=str((await self.identity(platform,handle))['platform_id'])
                except Exception:pass
            body=await self.get('SmiteSource','getPlayerByPlatform',{'platform':smitesource.PLATFORM_SLUGS[platform],'platformId':account})
            if not body or not body.get('playerUuid'):raise Unavailable('No exact source account')
            return body['playerUuid']
        return await self.cached(('source_id',platform,handle),resolve)

    async def safe(self,operation):
        try:return await operation
        except Exception:return None

    async def choose(self,operations):
        """Run providers independently; first usable answer wins, no retry chain."""
        tasks=[asyncio.create_task(self.safe(op)) for op in operations]
        try:
            for done in asyncio.as_completed(tasks):
                result=await done
                if result:return result
            raise Unavailable('All player sources unavailable or empty')
        finally:
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    async def source_overview(self,platform,handle,mode='all',split=None):
        uuid=await self.source_id(platform,handle)
        payload={'playerUuid':uuid,'mode':mode}
        if split:payload['split']=split
        return await self.get('SmiteSource','getPlayerOverview',payload)

    async def source_segments(self,platform,handle,kind):
        overview=await self.cached(('source_overview',platform,handle),lambda:self.source_overview(platform,handle))
        scope=overview['splitLabel']+' · recorded matches'
        if kind=='gamemode':
            keys=[m for m in overview['availableModes'] if m!='all']
            bodies=await asyncio.gather(*(self.source_overview(platform,handle,m,overview['split']) for m in keys))
            result=[]
            for mode,body in zip(keys,bodies):
                if body['split']!=overview['split'] or body['mode']!=mode:raise ValueError('mixed aggregate scope')
                item=segment(mode,smitesource._pretty_mode(mode),body['totals'],'SmiteSource',scope)
                if mode.startswith('ranked_'):
                    current=body.get('currentRank') or {};peak=body.get('peakRank') or {}
                    if current.get('sr') is not None:item.stats['skillRating']=current['sr']
                    if peak.get('sr') is not None:item.stats['peakSkillRating']=peak['sr']
                result.append(item)
            return result
        if kind=='god':
            uuid=await self.source_id(platform,handle)
            rows=await self.get('SmiteSource','getPlayerGodStats',{'playerUuid':uuid,'split':overview['split'],'mode':'all'})
            # Aspect variants are disjoint buckets; combine only within this
            # one response and verify they reconcile to its same-split totals.
            if sum(r['gamesPlayed'] for r in rows)!=overview['totals']['totalMatches']:
                raise ValueError('god breakdown does not reconcile')
            merged={}
            for row in rows:
                item=segment(row['godSlug'],row['godName'],row,'SmiteSource',scope)
                if item.key not in merged:merged[item.key]=item
                else:
                    old=merged[item.key]
                    for key in ('matchesPlayed','matchesWon','matchesLost','kills','deaths','assists','timePlayed'):
                        old.stats[key]=old.stats.get(key,0)+item.stats.get(key,0)
                    old.stats['matchesWinPct']=100*old.wins/(old.matches or 1)
                    old.stats['kdaRatio']=(old.stats['kills']+old.stats['assists']/2)/(old.stats['deaths'] or 1)
                    old.display.update({k:str(v) for k,v in old.stats.items()})
            return list(merged.values())
        if kind=='role':
            uuid=await self.source_id(platform,handle)
            rows=await self.get('SmiteSource','getPlayerRoleStats',{'playerUuid':uuid,'split':overview['split'],'mode':'all'})
            return [segment(r['role'].lower(),r['role'],r,'SmiteSource',scope) for r in rows]
        raise Unavailable('Unsupported aggregate')

    async def prime_segments(self,platform,handle,kind):
        if kind not in ('god','gamemode'):raise Unavailable('Prime does not publish role totals')
        identity=await self.identity(platform,handle)
        group='mode' if kind=='gamemode' else 'god'
        rows=await self.get('SmitePrime',f'matches-data/career/?person_uuid={identity["person_id"]}&group_by={group}&limit=500')
        result=[]
        for row in rows:
            key=smiteprime.MODES.get(row['mode'],row['mode']) if kind=='gamemode' else row['god'].removeprefix('gods.')
            result.append(segment(key,smitesource._pretty_mode(key) if kind=='gamemode' else row['god_display_name'],row,'SmitePrime','Recorded career matches',prime=True))
        return result

    async def tracker_segments(self,platform,handle,kind):
        rows=await self.get('tracker.gg',f'profile/{platform}/{quote(handle,safe="")}/segments/{kind}')
        result=[_segment(r,kind) for r in rows or []]
        for item in result:item.metadata.update(source='tracker.gg',scope='Tracker profile totals')
        return result

    async def segments(self,platform,handle,kind):
        return await self.cached(('segments',platform,handle,kind),lambda:self.choose([
            self.source_segments(platform,handle,kind),self.prime_segments(platform,handle,kind),
            self.tracker_segments(platform,handle,kind)]))

    async def overview(self,platform,handle):
        modes=await self.segments(platform,handle,'gamemode')
        info={'platformUserHandle':handle,'avatarUrl':None}
        try:
            identity=await self.identity(platform,handle)
            body=await self.get('SmitePrime',f'player-data/players/person/{identity["person_id"]}/')
            account=next((p for p in body['players'] if p['portal_id']==smiteprime.PORTALS[platform] and str(p['portal_user_id'])==str(identity['platform_id'])),{})
            info['platformUserHandle']=account.get('display_name') or handle
        except Exception:pass
        info.update(source=modes[0].metadata['source'],scope=modes[0].metadata['scope'])
        return info,modes

    async def profile(self,platform,handle):
        info,modes=await self.overview(platform,handle)
        return {'platformInfo':info,'segments':[{'type':'gamemode','attributes':{'gamemode':m.key},
                'metadata':dict(m.metadata,name=m.name),'stats':{k:{'value':v,'displayValue':m.display.get(k,str(v))} for k,v in m.stats.items()}} for m in modes]}

    async def history(self,name,platform,handle,limit):
        if name=='SmiteSource':
            uuid=await self.source_id(platform,handle)
            body=await self.get(name,'getPlayerMatches',{'playerUuid':uuid,'page':1,'pageSize':limit})
            return [summary(match_id=r.get('hirezMatchId'),mode=r.get('queueType') or '',timestamp=r.get('startTimestamp'),duration=r.get('durationSeconds'),won=r.get('won'),god=r.get('godSlug'),god_name=r.get('godName'),role=r.get('playedRole') or r.get('assignedRole'),kills=r.get('kills'),deaths=r.get('deaths'),assists=r.get('assists'),rating=r.get('finishingSr'),starting_rating=r.get('startingSr'),source=name) for r in body['matches']]
        if name=='SmitePrime':
            identity=await self.identity(platform,handle)
            owner=self
            class Client(smiteprime.SmitePrimeClient):
                async def get(self,path):return await owner.get(name,path)
            return await Client().recent_matches(identity,limit)
        body=await self.get(name,f'matches/{platform}/{quote(handle,safe="")}')
        return [row for m in body.get('matches',[]) if (row:=_summarise(m,platform,handle)) is not None][:limit]

    async def recent_matches(self,platform,handle,limit=10):
        if limit<=0:return []
        async def load():
            histories=await asyncio.gather(*(self.safe(self.history(n,platform,handle,min(limit,25))) for n in NAMES))
            if all(h is None for h in histories):raise Unavailable('All match history sources unavailable')
            return merge_histories([h for h in histories if h is not None],limit)
        return await self.cached(('history',platform,handle,limit),load,180)

    async def live(self,name,platform,handle):
        if name=='SmiteSource':
            uuid=await self.source_id(platform,handle)
            active=await self.get(name,'getLiveMatch',{'playerUuid':uuid})
            if active is None or active.get('isComplete'):return None
            mid=active['hirezMatchId']
            match=await self.get(name,'getMatch',{'matchId':mid})
            if match['isComplete']:return None
            rows=smitesource._players(match)
            mine=next((r for r in rows if r[0]==uuid),None)
            if not mine:raise Unavailable('Missing own live roster row')
            mode=match['queueType']
            return LiveMatch(mid,mode,smitesource._pretty_mode(mode),mode.startswith('ranked'),mine[1],mine[2],
                             [LivePlayer(r[1],r[2],r[3]) for r in rows],smitesource._epoch(match.get('liveUpdatedAt')),name)
        if name=='SmitePrime':
            identity=await self.identity(platform,handle)
            active=await self.get(name,f'player-data/players/{identity["person_id"]}/active-match/')
            if active is None:return None  # Verified HTTP 204 contract.
            mid=active.get('match_id') or (active.get('match') or {}).get('match_id')
            if not mid:raise Unavailable('Unknown Prime active-match envelope')
            match=await self.get(name,f'matches-data/matches/{mid}/')
            if match.get('state')=='closed':return None
            rows=match.get('players') or []
            mine=next((r for r in rows if (r.get('person') or {}).get('person_id')==identity['person_id']),None)
            if not mine:raise Unavailable('Missing own Prime live roster row')
            def god(r):return (r.get('god') or {}).get('god_display_name') or str(r.get('character_choice') or '').removeprefix('Gods.')
            players=[LivePlayer(god(r),smitesource.TEAMS.get(int(r['team_id']),''),(r.get('player') or {}).get('display_name','')) for r in rows]
            if any(not p.god or not p.team for p in players):raise Unavailable('Incomplete Prime roster')
            mode=smiteprime.MODES.get(match['current_mode'],match['current_mode'])
            return LiveMatch(mid,mode,smitesource._pretty_mode(mode),mode.startswith('ranked'),god(mine),smitesource.TEAMS[int(mine['team_id'])],players,0,name)
        own=await self.get(name,f'matches/{platform}/{quote(handle,safe="")}/live')
        if not own:return None
        mid=(own.get('attributes') or {}).get('id')
        if not mid:return None
        mine=_live_player(own) or next((p for s in own.get('segments',[]) if (p:=_live_player(s))),None)
        match=await self.get(name,f'matches/{mid}')
        players=[p for s in match.get('segments',[]) if (p:=_live_player(s))]
        meta=match.get('metadata') or {};attrs=match.get('attributes') or {}
        if not mine or not players:raise Unavailable('Incomplete Tracker roster')
        if meta.get('winningTeamId'):return None
        return LiveMatch(mid,attrs.get('gamemode',''),meta.get('gamemodeName',''),bool(meta.get('isRanked')),mine.god,mine.team,players,smitesource._epoch(meta.get('snapshotTimestamp')),name)

    async def live_match(self,platform,handle):
        async def load():
            # A source saying "not live" is valid but may lag; still allow the
            # other sources to find a lobby. Failures aren't cached as negatives.
            tasks=[asyncio.create_task(self.live(n,platform,handle)) for n in NAMES]
            answered=False
            try:
                for done in asyncio.as_completed(tasks):
                    try:result=await done
                    except Exception:continue
                    answered=True
                    if result:return result
                if not answered:raise Unavailable('All live match sources unavailable')
                return None
            finally:
                for task in tasks:
                    if not task.done():task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        try:return await self.cached(('live',platform,handle),load,60)
        except Unavailable:return None

    async def prime_ranked(self,platform,handle):
        rows=await self.prime_segments(platform,handle,'gamemode')
        identity=await self.identity(platform,handle)
        ranked=await self.get('SmitePrime',f'player-data/players/{identity["person_id"]}/ranked-sr/?days=30')
        # The SR summary combines modes. Verify each point's mode against its
        # full match before attaching it to a mode; never copy that combined SR.
        used=set()
        for point in sorted(ranked.get('points',[]),key=lambda p:p['played_at'],reverse=True)[:3]:
            match=await self.get('SmitePrime',f'matches-data/matches/{point["match_id"]}/')
            mode=smiteprime.MODES.get(match.get('current_mode'))
            if not mode or not mode.startswith('ranked_') or mode in used:continue
            mine=next((r for r in match.get('players',[]) if (r.get('person') or {}).get('person_id')==identity['person_id']),None)
            if not mine or mine.get('finishing_rank') is None:continue
            for row in rows:
                if row.key==mode:
                    row.stats['skillRating']=float(mine['finishing_rank'])
                    row.metadata['ratingScope']='Last recorded ranked match: '+point['played_at'][:10]
                    used.add(mode)
        return rows

    async def ranked_segments(self,platform,handle):
        async def rated(name):
            rows=await (self.source_segments(platform,handle,'gamemode') if name=='SmiteSource' else
                        self.tracker_segments(platform,handle,'gamemode') if name=='tracker.gg' else
                        self.prime_ranked(platform,handle))
            if not any(r.stats.get('skillRating') is not None for r in rows):return None
            return rows
        try:return await self.cached(('rank',platform,handle),lambda:self.choose([rated(n) for n in NAMES]))
        except Unavailable:return await self.segments(platform,handle,'gamemode')
