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
from collector_sources import Sources, NAMES, references, discover
from prime_rows import normalize, InvalidMatch
from source_rows import normalize_source, normalize_tracker
from store import SeenMatches
import paths
from game import Game
from smite2 import last_run
from smite2.players import parse_player
from catalogue import load as load_catalogue

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

async def workers(client, seeds, provider, seen, corpus, cutoff, report, checkpoint, max_matches):
    """Independent network workers; durable appends remain serial and atomic."""
    queues={name:asyncio.PriorityQueue() for name in NAMES}
    offered={name:set() for name in NAMES}
    active=set()
    enabled=set(NAMES)
    serial=0
    def offer(kind,value):
        nonlocal serial
        serial+=1
        for name in list(enabled):
            key=(kind,value)
            if key not in offered[name]:
                offered[name].add(key)
                queues[name].put_nowait((0 if kind=="match" else 1,serial,kind,value))
    for ref in seeds:offer('history',ref)
    async def worker(name):
        while report['new_matches']<max_matches:
            try:_,_,kind,value=await asyncio.wait_for(queues[name].get(),timeout=1)
            except asyncio.TimeoutError:
                if not active and all(queues[n].empty() for n in enabled):return
                continue
            active.add(name)
            try:
                # A cooldown delays only this worker. An explicit persisted
                # pause may outlast the run; leave other workers free to finish.
                until=max(client.pauses.get(name,0),client.stats[name].get('resume_at',0))
                if until>time.time():
                    if until-time.time()>=client.deadline-time.monotonic():
                        client.stats[name]['status']='paused'
                        return
                    await client.wait(until-time.time())
                if kind=='history':
                    matches,refs=await discover(client,name,value)
                    for ref in refs:offer('history',ref)
                    for raw in matches:
                        try:mid=str(UUID(raw))
                        except ValueError:continue
                        if mid not in seen:offer('match',mid)
                elif value not in seen:
                    body=await client.match(name,value)
                    if not body:continue
                    linked=list(references(name,body))
                    for ref in linked:offer('history',ref)
                    # A match discovered by Prime can reach the Source worker
                    # before its first history. Source details carry item UUIDs
                    # but no names, so obtain its asset map before normalizing.
                    if name=='SmiteSource' and not client.assets and linked:
                        matches,refs=await discover(client,name,linked[0])
                        for ref in refs:offer('history',ref)
                        for raw in matches:
                            try:mid=str(UUID(raw))
                            except ValueError:continue
                            if mid not in seen:offer('match',mid)
                    try:
                        if name=='SmiteSource' and not client.assets:
                            raise InvalidMatch('Source item catalogue unavailable')
                        date,rows=(normalize(body,provider) if name=='SmitePrime' else
                                   normalize_source(body,client.assets,provider) if name=='SmiteSource' else
                                   normalize_tracker(body,provider))
                        if rows[0]['Match']!=value:raise InvalidMatch('identity mismatch')
                    except InvalidMatch:
                        client.stats[name]['rejected']+=1
                        continue
                    # No await between this check, append and indexing: two
                    # providers finishing together can never double-write.
                    if value in seen or date<cutoff or report['new_matches']>=max_matches:continue
                    written=persist(corpus,seen,date,rows)
                    if written:
                        report['new_matches']+=1;report['rows_written']+=written
                        client.stats[name]['persisted']+=1
                        print(f'Persisted {value}: {written} rows from {name}',flush=True)
            except BudgetSpent:
                report['exit_reason']='budget'
                return
            finally:
                active.discard(name)
                queues[name].task_done()
                checkpoint()
    async def run_worker(name):
        try:await worker(name)
        finally:enabled.discard(name)
    tasks=[asyncio.create_task(run_worker(name)) for name in NAMES]
    try:await asyncio.gather(*tasks)
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

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
        provider, report["catalogue_source"] = await load_catalogue(
            paths.game_data_file(Game.SMITE_2, "source_catalogue.json"))
        print(f'Catalogue: {report["catalogue_source"]}; {len(provider.gods)} gods, {len(provider.items)} items', flush=True)
        seen=SeenMatches(state)
        cutoff=(datetime.now(timezone.utc)-timedelta(days=args.since_days)).date().isoformat()
        client=Sources(state,args.budget,args.interval,time.monotonic()+args.minutes*60)
        client.jitter=args.jitter
        async with client:
            await workers(client,[parse_player(s) for s in args.seed],provider,seen,corpus,cutoff,report,checkpoint,args.max_matches)
            if report['exit_reason']=='running':
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
    parser.add_argument('--jitter',type=float,default=0.4)
    parser.add_argument('--since-days',type=int,default=14)
    args=parser.parse_args()
    if min(args.budget,args.max_matches,args.minutes,args.since_days)<=0 or args.interval<12 or not 0<=args.jitter<=1:
        parser.error('positive limits and at least 12 seconds between requests required')
    return asyncio.run(run(args))

if __name__=='__main__':sys.exit(main())
