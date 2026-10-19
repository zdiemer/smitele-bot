"""Collector discovery built on the shared per-source HTTP policy."""
import asyncio
from urllib.parse import quote
from smite2.transport import SourceTransport, BudgetSpent, NAMES, retry_after
from smite2.smiteprime import exact_person
from smite2.smitesource import PLATFORM_SLUGS
from prime_rows import PORTALS

class Sources(SourceTransport):
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

async def discover(client, name, ref):
    """One provider's discovery, independent of other providers' wait state."""
    platform, account = ref
    found, refs = [], set()
    if name == 'SmitePrime':
        body = await client.get(name, f'player-data/players/search/{quote(account,safe="")}/?page=1')
        if not body:return found, refs
        try:identity=exact_person(body,platform,account)
        except RuntimeError:return found,refs
        refs.add((platform,str(identity['platform_id'])))
        body=await client.get(name,f'player-data/players/{identity["person_id"]}/matches/?page=1')
        found=[r['match']['match_id'] for r in (body or {}).get('results',[]) if (r.get('match') or {}).get('match_id')]
    elif name == 'SmiteSource':
        body=await client.get(name,'getPlayerByPlatform',{'platform':PLATFORM_SLUGS[platform],'platformId':account})
        if not body or not body.get('playerUuid'):return found,refs
        body=await client.get(name,'getPlayerMatches',{'playerUuid':body['playerUuid'],'page':1,'pageSize':25})
        client.assets.update(((body or {}).get('assetMaps') or {}).get('itemMetaByMasterId') or {})
        for row in (body or {}).get('matches',[]):
            if row.get('hirezMatchId'):found.append(row['hirezMatchId'])
            for player in (row.get('team1Players') or [])+(row.get('team2Players') or []):
                route=player.get('preferredRoute') or {}
                reverse={v:k for k,v in PLATFORM_SLUGS.items()}
                if route.get('platform') in reverse and route.get('platformId'):
                    refs.add((reverse[route['platform']],str(route['platformId'])))
    else:
        body=await client.get(name,f'matches/{platform}/{quote(account,safe="")}')
        found=[r['attributes']['id'] for r in (body or {}).get('matches',[]) if (r.get('attributes') or {}).get('id')]
    client.stats[name]['discovered']+=len(found)
    return found,refs
