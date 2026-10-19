"""Map verified SmitePrime complete match reports onto the existing corpus.

Never copy Prime's item integers: corpus IDs belong to the wiki catalogue.
Unmapped core items stay in their slot as zero (not a fabricated full build).
An unresolvable aspect rejects the match because zero would mean no aspect.
"""
from datetime import datetime, timezone
from uuid import UUID
from smite2.ids import item_id, slugify

from rows import player_rows

MODES = {
    'BP_GameMode_Joust_C': ('joust', False, 6),
    'BP_GameMode_Conquest_C': ('conquest', False, 10),
    'BP_GameMode_Arena_C': ('arena', False, 10),
    'BP_GameMode_Conquest_Ranked_C': ('conquest-ranked', True, 10),
    'BP_GameMode_Assault_OB_C': ('assault', False, 10),
    'BP_GameMode_Joust_Duel_C': ('duel-ranked', True, 2),
}
PORTALS = {5: 'steam', 9: 'psn', 10: 'xbl', 28: 'epic'}


class InvalidMatch(ValueError):
    pass


def normalize(match, provider):
    try:
        return _normalize(match, provider)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise InvalidMatch(str(error)) from error


def _normalize(match, provider):
    if match.get('type') != 'MatchReport' or match.get('state') != 'closed' or match.get('matchmade') is not True:
        raise InvalidMatch('not a completed matchmade report')
    mode, ranked, count = MODES[match['current_mode']]
    match_id = str(UUID(match['match_id']))
    stamp = datetime.fromisoformat(match['start_timestamp'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise InvalidMatch('timestamp has no timezone')
    stamp = stamp.astimezone(timezone.utc)
    if stamp > datetime.now(timezone.utc):
        raise InvalidMatch('future match')
    players = match['players']
    winner = str(match['winning_team'])
    if winner not in ('1', '2') or len(players) != count:
        raise InvalidMatch('incomplete roster or winner')
    if len({r['player_uuid'] for r in players}) != count:
        raise InvalidMatch('duplicate player')
    if any(sum(str(r['team_id']) == team for r in players) != count // 2 for team in ('1', '2')):
        raise InvalidMatch('incomplete teams')
    records = []
    for player in players:
        god_name = (player.get('god') or {}).get('god_display_name') or player['character_choice']
        god_id = provider.god_id_from_name(god_name)
        if not god_id:
            raise InvalidMatch(f'unknown god: {god_name}')
        account = player['player']
        platform = PORTALS.get(account['portal_id'])
        account_id = str(account.get('portal_user_id') or '')
        if not platform or not account_id:
            raise InvalidMatch('missing platform identity')
        team = 'order' if str(player['team_id']) == '1' else 'chaos'
        role = str(player.get('played_role') or player.get('assigned_role') or '').removeprefix('E').lower()
        stats = {}
        for source, target in [('kills', 'kills'), ('deaths', 'deaths'), ('assists', 'assists'), ('total_damage', 'damage')]:
            value = int(player[source])
            if value < 0:
                raise InvalidMatch('negative stat')
            stats[target] = {'value': value}
        # Only a Conquest rating belongs in Rank_Stat_Conquest.
        if mode == 'conquest-ranked' and player.get('finishing_rank') is not None:
            stats['skillRating'] = {'value': float(player['finishing_rank'])}
        raw = {
            'attributes': {'id': match_id, 'gamemode': mode},
            'metadata': {'timestamp': stamp.isoformat(), 'isRanked': ranked,
                         'winningTeamId': 'order' if winner == '1' else 'chaos'},
            'segments': [{'type': 'overview',
                'attributes': {'platformSlug': platform, 'platformUserIdentifier': account_id},
                'metadata': {'god': god_name, 'teamId': team, 'playedRole': {'key': role},
                             'partyId': player.get('party_session_id') or ''},
                'stats': stats}],
        }
        record = next(player_rows(raw, {god_name: god_id}))
        items = player['items']
        unknown = []
        for slot, column in [(f'item_{i}', f'ItemId{i}') for i in range(1, 7)] + [('starter', 'StarterId'), ('relic', 'ActiveId1'), ('talent', 'Aspect')]:
            item = items.get(slot)
            record[column] = 0
            if item is None:
                continue
            name = item['item_display_name'].strip()
            resolved = provider.item_by_name(name)
            if resolved is None:
                if slot == 'talent':
                    # The wiki item catalogue excludes aspects. Explicit
                    # display names use the same slug-derived identity as
                    # tracker talents; generic labels such as "Nike" cannot
                    # identify an aspect and must not be guessed.
                    if not name.startswith('Aspect of '):
                        raise InvalidMatch(f'unknown aspect: {name}')
                    record[column] = item_id(slugify(name))
                    continue
                unknown.append(name)
            else:
                record[column] = resolved.id
        # Curios are a separate seventh inventory category; not core items.
        record['UnknownItems'] = len(unknown)
        record['ItemSlots'] = sum(v is not None for k,v in items.items() if k != 'curio')
        record['Source'] = 'SmitePrime'
        record['SourcePlayerUuid'] = str(player['player_uuid'])
        record['CollectedAtUtc'] = datetime.now(timezone.utc).isoformat()
        records.append(record)
    return stamp.date().isoformat(), records
