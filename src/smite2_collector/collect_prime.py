#!/usr/bin/env python3
"""Bounded, single-worker SmitePrime corpus collection. No Tracker requests.

Use only while the legacy collector is suspended and no other writer is active.
Whole completed matches are normalized, atomically appended, then indexed.
A crash between append and index is recovered by checking persisted match IDs.
"""
import argparse
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'HirezAPI'))

import paths
from game import Game
import match_storage
from smite2 import last_run
from smite2.players import parse_player
from smite2.provider import Smite2Provider
from smite2.smiteprime import SmitePrimeClient
from prime_rows import normalize, InvalidMatch
from store import SeenMatches, merge_day, output_path


class BudgetSpent(Exception):
    pass


class PacedClient(SmitePrimeClient):
    def __init__(self, budget, interval, deadline):
        self.budget, self.interval, self.deadline = budget, interval, deadline
        self.requests = 0
        self.last_request = 0

    async def get(self, path):
        if self.requests >= self.budget or time.monotonic() >= self.deadline:
            raise BudgetSpent()
        await asyncio.sleep(max(0, self.last_request + self.interval - time.monotonic()))
        if time.monotonic() >= self.deadline:
            raise BudgetSpent()
        self.requests += 1
        self.last_request = time.monotonic()
        # Any HTTP failure stops this run. No retries, rotating proxies or solves.
        return await super().get(path)


def persist(corpus, seen, date, records):
    match_id = records[0]['Match']
    destination = output_path(corpus, date)
    if os.path.exists(destination):
        existing = match_storage.read_frame(destination)
        if match_id in set(existing['Match'].astype(str)):
            seen.add(match_id, date)
            seen.save()
            return 0
    written = merge_day(corpus, date, records)
    if written != len(records):
        raise RuntimeError('corpus append failed; match not indexed')
    # Read back durable data before marking the match collected.
    stored = match_storage.read_frame(destination)
    actual = stored.loc[stored['Match'].astype(str) == match_id]
    if len(actual) != len(records) or actual['GodId'].isna().any():
        raise RuntimeError('corpus readback failed; match not indexed')
    seen.add(match_id, date)
    seen.save()
    return written


async def crawl(args):
    state = paths.game_model_dir(Game.SMITE_2)
    corpus = paths.game_match_data_dir(Game.SMITE_2)
    os.makedirs(state, exist_ok=True)
    lock = os.path.join(state, 'prime-collector.lock')
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    started = time.time()
    report = dict(source='SmitePrime', started=started, budget=args.budget,
                  new_matches=0, rows_written=0, requests=0, rejected_matches=0)
    reason = 'ok'
    client = None
    def checkpoint():
        report.update(elapsed_seconds=time.time()-started, exit_reason=reason,
                      requests=client.requests if client else 0)
        last_run.write(state, report)
        # Preserve a source-specific report as well as the dashboard's last run.
        target = os.path.join(state, 'last_prime_run.json')
        with open(target+'.partial','w') as f:json.dump(report,f)
        os.replace(target+'.partial',target)
    try:
        provider = Smite2Provider(silent=True)
        await provider.create()
        if not provider.gods or not provider.items:
            raise RuntimeError('catalogue unavailable')
        seen = SeenMatches(state)
        cutoff = (datetime.now(timezone.utc)-timedelta(days=args.since_days)).date().isoformat()
        pending = deque()
        visited, attempted = set(), set()
        client = PacedClient(args.budget, args.interval, time.monotonic()+args.minutes*60)
        async with client:
            for seed in args.seed:
                identity = await client.resolve(*parse_player(seed))
                pending.append(str(UUID(identity['person_id'])))
            while pending and report['new_matches'] < args.max_matches:
                person = pending.popleft()
                if person in visited:continue
                visited.add(person)
                page = await client.get(f'player-data/players/{person}/matches/?page=1')
                if not isinstance(page,dict) or not isinstance(page.get('results'),list):
                    raise RuntimeError('history schema changed')
                for row in page['results']:
                    summary = row.get('match') or {}
                    match_id = str(UUID(summary['match_id']))
                    if summary.get('start_timestamp','')[:10] < cutoff:continue
                    if match_id in attempted or match_id in seen:continue
                    attempted.add(match_id)
                    match = await client.get(f'matches-data/matches/{match_id}/')
                    if match.get('match_id') != match_id:
                        raise RuntimeError('match identity mismatch')
                    # Expand through site-local person IDs from this site only.
                    for player in match.get('players') or []:
                        linked = (player.get('person') or {}).get('person_id')
                        if linked and linked not in visited:
                            pending.append(str(UUID(linked)))
                    try:
                        date, records = normalize(match, provider)
                    except InvalidMatch as error:
                        report['rejected_matches'] += 1
                        print(f'Skip {match_id}: {error}',flush=True)
                        continue
                    if date < cutoff:continue
                    written = persist(corpus,seen,date,records)
                    if written:
                        report['new_matches'] += 1
                        report['rows_written'] += written
                        print(f'Persisted {match_id}: {written} rows on {date}',flush=True)
                    reason = 'running'
                    checkpoint()
                    if report['new_matches'] >= args.max_matches:break
            reason = 'ok'
    except BudgetSpent:
        reason = 'budget'
    except BaseException as error:
        reason = 'terminated' if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt)) else 'blocked'
        # Transport exception strings could contain infrastructure details.
        print(f'Stopped: {type(error).__name__}',flush=True)
        raise
    finally:
        try:
            checkpoint()
        finally:
            os.unlink(lock)
        print(json.dumps(report,sort_keys=True),flush=True)
    return 0 if report['rows_written'] else 1


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seed',action='append',required=True)
    p.add_argument('--budget',type=int,default=30)
    p.add_argument('--max-matches',type=int,default=10)
    p.add_argument('--minutes',type=float,default=15)
    p.add_argument('--interval',type=float,default=12)
    p.add_argument('--since-days',type=int,default=14)
    args=p.parse_args()
    if min(args.budget,args.max_matches,args.minutes,args.since_days)<=0 or args.interval<12:
        p.error('positive limits and an interval of at least 12 seconds are required')
    return asyncio.run(crawl(args))


if __name__=='__main__':
    sys.exit(main())
