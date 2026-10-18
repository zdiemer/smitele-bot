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
