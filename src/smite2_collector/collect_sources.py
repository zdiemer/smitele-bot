#!/usr/bin/env python3
"""Bounded three-source collector with shared durable match deduplication."""
import argparse
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import sys
import time
from uuid import UUID

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'HirezAPI'))
from collect_prime import persist, BudgetSpent
from collector_sources import Sources, NAMES, references
from prime_rows import normalize, InvalidMatch
from source_rows import normalize_source, normalize_tracker
from store import SeenMatches
import paths
from game import Game
from smite2 import last_run
from smite2.players import parse_player
from smite2.provider import Smite2Provider

async def collect_match(client, mid, preferred, provider):
    """Try other providers when a scoreboard is unavailable or invalid."""
    best=None
    refs=set()
    for name in dict.fromkeys([*preferred,*NAMES]):
        body=await client.match(name,mid)
        if not body:continue
        refs.update(references(name,body))
        try:
            date,rows=(normalize(body,provider) if name=='SmitePrime' else
                       normalize_source(body,client.assets,provider) if name=='SmiteSource' else
                       normalize_tracker(body,provider))
            if rows[0]['Match']!=mid:raise InvalidMatch('identity mismatch')
        except InvalidMatch:
            client.stats[name]['rejected']+=1
            continue
        unknown=sum(r.get('UnknownItems',0) for r in rows)
        if best is None or unknown<best[0]:best=(unknown,name,date,rows)
        if not unknown:break
    return best,refs

async def crawl(args):
    state=paths.game_model_dir(Game.SMITE_2)
    corpus=paths.game_match_data_dir(Game.SMITE_2)
    os.makedirs(state,exist_ok=True)
    lock=Path(state)/'prime-collector.lock'  # Shared with the earlier standalone writer.
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); os.close(fd)
    started=time.time()
    report=dict(source='multi-source',started=started,new_matches=0,rows_written=0,exit_reason='running')
    client=None
    def checkpoint():
        report.update(elapsed_seconds=time.time()-started,requests=client.requests if client else 0,
                      sources=client.stats if client else {})
        last_run.write(state,report)
        target=Path(state)/'last_sources_run.json'
        temp=target.with_suffix('.partial'); temp.write_text(json.dumps(report)); temp.replace(target)
    try:
        provider=Smite2Provider(silent=True); await provider.create()
        if not provider.gods or not provider.items:raise RuntimeError('catalogue unavailable')
        seen=SeenMatches(state)
        cutoff=(datetime.now(timezone.utc)-timedelta(days=args.since_days)).date().isoformat()
        pending=deque(parse_player(s) for s in args.seed)
        visited,attempted=set(),set()
        client=Sources(state,args.budget,args.interval,time.monotonic()+args.minutes*60)
        async with client:
            while pending and report['new_matches']<args.max_matches:
                ref=pending.popleft()
                if ref in visited:continue
                visited.add(ref)
                candidates=await client.histories(ref)
                pending.extend(client.refs)
                checkpoint()
                for raw,preferred in candidates.items():
                    try:mid=str(UUID(raw))
                    except ValueError:continue
                    if mid in attempted:continue
                    attempted.add(mid)
                    # Seen matches still provide frontier routes in histories.
                    if mid in seen:continue
                    result,refs=await collect_match(client,mid,preferred,provider)
                    pending.extend(refs)
                    if not result:continue
                    _,name,date,rows=result
                    if date<cutoff:continue
                    written=persist(corpus,seen,date,rows)
                    if written:
                        report['new_matches']+=1; report['rows_written']+=written
                        client.stats[name]['persisted']+=1
                        print(f'Persisted {mid}: {written} rows from {name}',flush=True)
                    checkpoint()
                    if report['new_matches']>=args.max_matches:break
            report['exit_reason']='ok' if report['rows_written'] else 'no_new_matches'
    except BudgetSpent:report['exit_reason']='budget'
    except BaseException as error:
        report['exit_reason']='terminated' if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt)) else 'failed'
        raise
    finally:
        try:checkpoint()
        finally:lock.unlink()
        print(json.dumps(report,sort_keys=True),flush=True)
    return 0 if report['rows_written'] else 1

async def run(args):
    task=asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,task.cancel)
    return await crawl(args)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed',action='append',required=True)
    parser.add_argument('--budget',type=int,default=1000)
    parser.add_argument('--max-matches',type=int,default=500)
    parser.add_argument('--minutes',type=float,default=240)
    parser.add_argument('--interval',type=float,default=12)
    parser.add_argument('--since-days',type=int,default=14)
    args=parser.parse_args()
    if min(args.budget,args.max_matches,args.minutes,args.since_days)<=0 or args.interval<12:
        parser.error('positive limits and at least 12 seconds between requests required')
    return asyncio.run(run(args))

if __name__=='__main__':sys.exit(main())
