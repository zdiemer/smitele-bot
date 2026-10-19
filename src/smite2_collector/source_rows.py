"""Provider-specific validation into the shared corpus, never mixed scoreboards."""
from datetime import datetime, timezone
from uuid import UUID

from prime_rows import normalize as normalize_prime, InvalidMatch, PORTALS
from rows import player_rows
from smite2.queues import from_mode_string, Smite2QueueId


def normalize_source(match, assets, provider):
    """SmiteSource full-match inventory slots are ONE based (history is not)."""
    try:
        players = []
        for row in match['players']:
            if row.get('isBot'):
                raise InvalidMatch('bot roster')
            accounts = [a for a in row['smitePlayer']['platforms']
                        if a.get('portalId') in PORTALS and a.get('portalUserId')]
            if not accounts:
                raise InvalidMatch('missing platform account')
            account = accounts[0]
            inventory = {}
            for item in row['items']:
                category = item['c']
                if category in ('C', 'T'):
                    continue  # Curios are not core; aspects have explicit fields.
                if category == 'I':
                    slot = int(item['s'])
                    if not 1 <= slot <= 6:
                        raise InvalidMatch('unexpected full-match item slot')
                    key = f'item_{slot}'
                elif category in ('S', 'R'):
                    key = {'S': 'starter', 'R': 'relic'}[category]
                else:
                    raise InvalidMatch('unknown inventory category')
                if key in inventory:
                    raise InvalidMatch('duplicate inventory slot')
                name = (assets.get(item.get('m')) or {}).get('name')
                # Missing joins remain explicitly unknown, not a fabricated ID.
                inventory[key] = {'item_display_name': name or '[unmapped source item]'}
            if row.get('isAspect') is True:
                name = row.get('aspectName')
                if not name or not name.startswith('Aspect of '):
                    raise InvalidMatch('unknown aspect')
                inventory['talent'] = {'item_display_name': name}
            elif row.get('isAspect') is not False or any(i['c'] == 'T' for i in row['items']):
                raise InvalidMatch('ambiguous aspect state')
            players.append({
                'player_uuid': row['hirezPlayerUuid'],
                'player': {'portal_id': account['portalId'], 'portal_user_id': account['portalUserId']},
                'god': {'god_display_name': (row.get('godMaster') or {}).get('canonicalName')},
                'character_choice': row['godRawName'], 'team_id': str(row['teamId']),
                'party_session_id': row.get('partySessionId'),
                'played_role': row.get('playedRole'), 'assigned_role': row.get('assignedRole'),
                'kills': row['kills'], 'deaths': row['deaths'], 'assists': row['assists'],
                'total_damage': row['totalDamage'], 'finishing_rank': row.get('finishingSr'),
                'items': inventory,
            })
        common = {
            'type': 'MatchReport', 'state': 'closed' if match['isComplete'] is True else 'live',
            'matchmade': match['isMatchmade'], 'match_id': match['hirezMatchId'],
            'current_mode': match['gameMode'], 'start_timestamp': match['startTimestamp'],
            'winning_team': str(match['winningTeam']), 'players': players,
        }
        date, rows = normalize_prime(common, provider)
        for row in rows:
            row['Source'] = 'SmiteSource'
        return date, rows
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise InvalidMatch(str(error)) from error


def normalize_tracker(match, provider):
    """Keep the existing Tracker mapping, validating a complete roster first."""
    try:
        meta, attrs = match['metadata'], match['attributes']
        match_id = str(UUID(attrs['id']))
        stamp = datetime.fromisoformat(meta['timestamp'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
            raise InvalidMatch('invalid match timestamp')
        if meta.get('isLive') or meta.get('isSnapshot') or meta.get('state') in ('pending', 'live'):
            raise InvalidMatch('live snapshot')
        if meta.get('winningTeamId') not in ('order', 'chaos'):
            raise InvalidMatch('missing winner')
        queue = from_mode_string(attrs['gamemode'], bool(meta.get('isRanked')))
        sizes = {Smite2QueueId.CONQUEST:10, Smite2QueueId.RANKED_CONQUEST:10,
                 Smite2QueueId.JOUST:6, Smite2QueueId.ARENA:10, Smite2QueueId.ASSAULT:10,
                 Smite2QueueId.DUEL:2, Smite2QueueId.RANKED_DUEL:2}
        count = sizes[queue]
        segments = [s for s in match['segments'] if s.get('type') == 'overview' and (s.get('metadata') or {}).get('god')]
        if len(segments) != count:
            raise InvalidMatch('incomplete roster')
        gods = {}
        identities = set()
        for segment in segments:
            a, m = segment['attributes'], segment['metadata']
            identity = (a['platformSlug'], str(a['platformUserIdentifier'] or ''))
            if identity[0] not in PORTALS.values() or not identity[1]:
                raise InvalidMatch('missing platform account')
            identities.add(identity)
            god = provider.god_id_from_name(m['god'])
            if not god:
                raise InvalidMatch('unknown god')
            gods[m['god']] = god
            for stat in ('kills','deaths','assists','damage'):
                if int(segment['stats'][stat]['value']) < 0:
                    raise InvalidMatch('negative stat')
        if len(identities) != count or any(sum(s['metadata'].get('teamId') == t for s in segments) != count//2 for t in ('order','chaos')):
            raise InvalidMatch('invalid roster identities or teams')
        rows = list(player_rows(dict(match, segments=segments), gods))
        for row in rows:
            row.update(Match=match_id, Source='tracker.gg', CollectedAtUtc=datetime.now(timezone.utc).isoformat())
        return stamp.astimezone(timezone.utc).date().isoformat(), rows
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise InvalidMatch(str(error)) from error
