# Smite 2 collector: upstream access blocked

As of 2026-10-18, the scheduled collector is suspended. Collection has **not**
been restored. Existing match data and aggregation remain available.

The October 18 run created browser clearance successfully, but the leaderboard
endpoint returned HTTP 403 twice and the collector exited without new matches.
A diagnostic request from the collector image and network on October 18 returned
Tracker Network's explicit "Blocked Request" page, directing scrapers to stop.
This is distinct from an expired-cookie challenge or Kubernetes DNS failure.

Tracker Network also states that it does not offer a public Smite 2 API:
https://feedback.tracker.gg/t/tracker-rate-limit-and-api-use/66344

`smite2.collector.suspend: true` preserves the CronJob without launching more
failed scheduled runs. `enabled` still controls whether the CronJob exists at
all. Suspension does not stop an already running Job or prevent manually
created Jobs; do not use manual runs as an automatic retry mechanism.

To restore collection, first obtain working upstream access or implement a
replacement data source. Verify a bounded collection run writes valid new match
rows before setting `smite2.collector.suspend: false` and deploying the chart.
The one-hour starting deadline prevents replaying a backlog of old schedules.

Do not delete the corpus, cached clearance, or failure records to address this
block: doing so cannot grant access and loses useful state.

## October 19: bounded replacement collector

A single request using the configured Tracker route still returned HTTP 403
with its explicit block page. No clearance/cooldown state was reset.

`src/smite2_collector/collect_prime.py` now offers a separate, one-off SmitePrime
path. It reads player history and full completed match reports, expands through
that site's person IDs, and writes the existing dated Parquet corpus and shared
seen-match index. It never calls Tracker or uses its proxy. Run only with the
legacy CronJob suspended and no other corpus writer active.

Start conservatively, for example:

```
python src/smite2_collector/collect_prime.py \
  --seed xbl:juicytot6298 --max-matches 10 --budget 40 \
  --minutes 15 --interval 12 --since-days 14
```

Requests are sequential, at least twelve seconds apart; any HTTP failure stops
the run without retries. Only whole completed matchmade rosters are accepted.
Gods and ordinary items resolve through the existing wiki catalogue. Explicit
`Aspect of ...` names use the existing slug-derived talent IDs; ambiguous talent
labels reject the match. Unknown core items stay zero in their original slots,
so they cannot become fabricated full builds. Curios are not core-item slots.

Writes are atomic per day and read back before the shared match index advances.
On restart, persisted match IDs repair an index left behind by interruption.
A separate exclusive lock prevents overlapping Prime collectors. A killed
process may leave that lock: confirm no writer remains before removing it.

Each checkpoint records source, requests, rows, accepted/rejected matches in
`last_prime_run.json` and the dashboard's `last_run.json`. This is a sampled,
page-one collection, not proof of complete coverage or a twelve-day backfill.
The scheduled Tracker job remains suspended pending a separate operational
switch to the replacement source.

**Validated in production on October 19:** Job
`smitele-prime-collect-20261019` completed successfully in 200 seconds: **10 new
matches, 68 player rows, 15 requests**, with two ambiguous-aspect matches
rejected. Readback confirmed durable records; the existing aggregation
preparation accepted a 46-row intermediate sample and identified 22 usable full
builds. This verifies corpus compatibility, not full population coverage.

## Three-source collection

`src/smite2_collector/collect_sources.py` is the combined entrypoint. It queries
SmitePrime, SmiteSource and tracker.gg histories, unions match UUIDs, and shares
one corpus/index. Verified platform accounts bridge site-local identities.
SmiteSource teammate routes and full scoreboards expand the player frontier,
including through previously collected histories. Each candidate can fall back
to the other sources when its first scoreboard is missing or invalid. Scoreboards
are never blended; a validated complete roster is written once with provenance.

Use the same CLI limits as `collect_prime.py`. All three adapters are enabled;
403 responses pause only that provider, durably, for at least 24 hours.
429 responses honor Retry-After (seconds or HTTP date), with a 60-second minimum
and added jitter. Timeouts, connection failures, HTTP 408/425 and 5xx responses
get three retries with exponential jittered delays (12–24, 24–48, 48–96 seconds).
Retry-After on a server error is also a minimum. Every attempt consumes the
request budget and respects the run deadline and global request spacing.
Exhaustion cools only that provider for 5–10 minutes; later requests can recover.
Cancellation and permanent request/schema errors are not retried. Logs and
source counters record the operation, attempt, retry delay and failure type,
without account identifiers or credentials.
Tracker uses the existing configured egress and any cached clearance, without
minting, resetting, or rotating routes. Its known October 19 refusal must remain
paused during the replacement run; enabled does not imply upstream access.

`last_sources_run.json` records each provider's requests, discoveries, rejected
scoreboards, persisted matches and availability. The combined collector shares
the standalone Prime writer lock, handles SIGTERM, and preserves atomic writes
and readback. Stop the old job before starting this one. The scheduled legacy
Tracker CronJob remains suspended. This bounded page-one crawl still does not
claim exhaustive twelve-day backfill coverage.

## Application migration and independent workers

Release 1.12.3 moves the scheduled CronJob to the normal bot image and
`collect_sources.py`. Each provider has its own asynchronous worker, pacing
clock, lock, retries and cooldown. Shared corpus appends are serial and atomic;
multiple providers discovering the same match cannot write it twice. The request
budget is a run-wide ceiling, not a shared rate limiter. The website displays
per-source run status and retry counts. Profile, stats, history and live lookups
use the same transport policy through the application provider.
