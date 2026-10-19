# Smite 2 sources working together

Investigated **2026-10-19** from public pages, their shipped JavaScript and
small unauthenticated requests. These are undocumented website interfaces, not
promised developer APIs. The captured, trimmed response fixtures live in
`tests/fixtures/smite2_sources/`.

## Implemented

The bot's `MultiSourcePlayerLookups` combines **SmiteSource + SmitePrime** recent
match pages, then asks **tracker.gg** if fewer than the requested number of
unique matches remain. Existing live-match selection still works; the
SmiteSource transport was repaired for its current required header.

- Join completed matches by the game UUID (`hirezMatchId` / `match.match_id`),
  never by timestamps, local integer IDs, player names, or god names.
- Prefer the complete SmiteSource scoreboard on conflict, then Prime, then
  tracker; this is deterministic precedence, not a claim about freshness.
  Fill absent image/rating fields only. Zero ratings are real values.
- Retain all contributing source names per match and show them in the history
  footer. Deduplicate before sorting by UTC start time and limiting results.
- Cache histories for three minutes, identities for fifteen minutes, collapse
  simultaneous history requests, bound each provider to twelve seconds, and
  back off failed source/player pairs for one minute. HTTP calls time out at
  four seconds. No automatic retry loops, browser solves, or proxy rotation.
- An outage differs from a successful empty history. A total outage reports
  temporary unavailability and is not cached as “no matches.”
- This is page-one history (at most 25), not an exhaustive crawl. SmiteSource
  uses its current default split, so tracker can fill short/older histories.

In the recorded sample, Prime had a completed Joust match starting
`2026-10-18T21:39:29.110Z` absent from the Source page, whose newest match began
`21:17:42.847Z`. This verifies complementary coverage; it does **not** establish
which site is generally fresher.

## Endpoint contracts

### SmiteSource

The [tracker page](https://smitesource.com/tracker) describes public profile,
match history, ranked and per-god views. The site's shipped client uses:

```
GET https://smitesource.com/rpc/matches/<procedure>?data=<urlencoded JSON>
x-smitesource-client: 1
```

The query value is `{"json":{...arguments...}}`; successful JSON has a `json`
member. The client marker header is necessary: the same requests returned 403
without it and 200 with it. No credentials or cookies were supplied.

| Procedure | Arguments verified | Result |
| --- | --- | --- |
| getPlayerByPlatform | platform, platformId | playerUuid, displayName, platforms; null for an unresolved account |
| getPlayerProfile | playerUuid | platform accounts and display data |
| getPlayerMatches | playerUuid, page: 1, pageSize: 10 | matches, hasMore, nextCursor, split, splitLabel, assetMaps |
| getPlayerOverview | playerUuid | current-split totals, ranks, roles, top gods |
| getPlayerGodStats | playerUuid | rows split by god, role and aspect |
| getLiveMatch | playerUuid | null was verified; existing live parser retained |

`getPlayerStats` returned null for the sampled player: a procedure existing in
the JavaScript is not proof that its response is useful. Valid split values
observed were `s0-4`, `s0-3`, `s0-2`, `s0-1`, `s0-all`, `alpha`; `all` was
rejected. Do not pin a previous season when requesting recent matches.

The profile's `playerUuid` and match row's `smitePlayer.person.publicUuid` are
site-local public IDs. `hirezPlayerUuid` is a different identity. Platform
slugs include `steam`, `epic`, `xbox`, `playstation`.

### SmitePrime

The [site](https://smiteprime.com/) ships a React API client. Verified routes:

```
GET /api/player-data/players/search/<encoded query>/?page=1
GET /api/player-data/players/person/<person_id>/
GET /api/player-data/players/<person_id>/matches/?page=1
GET /api/player-data/players/<person_id>/active-match/
GET /api/matches-data/career/?person_uuid=<person_id>&group_by=mode
Accept: application/json
```

Without `Accept`, a browser impersonation client received Django's HTML API
explorer, including for successful requests. Search/history have `results`,
`count`, `next`, `previous`; the active-match route returned **204** for the
sampled inactive player. No active payload was verified, so no Prime live
adapter was fabricated.

Search exposes a local `person_id` and linked platform accounts with
`portal_id`, `portal_user_id`, `display_name`, `player_uuid`. Match history
contains the queried player's row plus `other_players`: use the top-level row.
The top-level player duration differs from `match.duration_seconds`; use the
latter for the displayed match length.

### Identity bridge

Prime and Source returned **different person UUIDs for the same Xbox account**.
The bridge therefore resolves an exact name **on the requested platform**, then
passes its verified platform account ID to Source's `getPlayerByPlatform`.
Portal IDs observed: Steam 5, PSN 9, XboxLive 10, Epic 28. Ambiguous results,
fuzzy matches, wrong platforms and truncated name searches are rejected. A
caller-supplied Source UUID is not sent to Prime as a person UUID.

## Other candidates and boundaries

- [Smite2.live](https://smite2.live/) ships `player-search.js`, calling
  `/api/player-search?q=...` and reading `results` with `ign`, `platform`, `url`.
  No history response contract was verified; it is not enabled as a history
  provider. Its robots file explicitly excludes several AI agents.
- [SmiteTracker](https://smitetracker.com/) explicitly states it has no player
  data access and cannot provide account lookups, win rates or leaderboards.
  It is an editorial reference, not a replacement stats feed.
- [Tracker's own response](https://feedback.tracker.gg/t/tracker-rate-limit-and-api-use/66344)
  says there is no offered Smite 2 developer API. Its existing scraper and
  request budget remain the fallback.
- Source's robots file disallows general `/rpc/` crawling (with one leaderboard
  exception); this is not an advertised bulk-data export. These small public
  lookup probes do not establish permission or capacity for corpus crawling.

## Aggregate stats and corpus work still separate

Player overview/god/role aggregates and the scheduled collector **still use
tracker.gg**. A bounded manual SmitePrime corpus collector is now available;
see [collector status](smite2-collector-status.md). Source's default aggregate is a split, its god rows include
role/aspect subdivisions, and Prime's sampled KDA used full assists whereas
Source used half assists. Adding either site's totals to tracker would double
count overlapping games and potentially mix scopes and definitions.

Before extending the corpus: normalize engine god names and item master UUIDs
against the catalogue, map slot categories (including acorns/curios), require
completed match status, and deduplicate shared match/player IDs in storage.
Retain source, scope and observation time. The recorded raw histories provide
starting fixtures; the manual collector uses full match reports and validates
complete rosters before writing corpus rows.

Recheck a single player without invoking tracker or its clearance machinery:

```bash
.venv/bin/python scripts/probe_smite_sources.py xbl juicytot6298
.venv/bin/python -m pytest tests/test_multi_source_history.py tests/test_smitesource.py -q
```
