# smitele-bot

Smite-le — a Discord bot for a six-round Smite guessing game in the shape of
Wordle — plus the daily Hi-Rez match data collector that feeds it.

This repo holds both the source and the Helm chart that runs it on the home k3s
cluster, so a release is one commit in one place: `Chart.yaml` `appVersion`
tracks `values.yaml` `image.tag`.

## What runs

| Piece | Kind | What it does |
|---|---|---|
| bot | Deployment (1 replica) | The Discord bot. Slash commands for the guessing game, god/item builds, trivia, and player stats. |
| collector | CronJob (daily) | Walks a day of Hi-Rez match IDs across 12 queues, fetches details in batches of 10, and writes `match_details_<date>.json` to the NAS. Files older than 30 days move to `archive/`. |

The bot reads the corpus the collector writes: `SmiteProvider` loads every file
into a pandas DataFrame and refreshes on a loop, which is what backs the
build-from-real-matches commands.

## Storage

Two volumes, deliberately different:

- **`/data`** — a small ReadWriteOnce PVC, private to the bot. Hi-Rez session
  token, patch version marker, `gods.json`/`items.json` caches, and downloaded
  god/item/skin art. All regenerable; keeping it just avoids re-fetching a few
  hundred images on restart.
- **`/matchdata`** — the match-detail corpus, on the NAS over SMB. It grows
  every day and is written by the collector while being read by the bot, so it
  needs `ReadWriteMany`. local-path only offers `ReadWriteOnce`, which is why
  this is a network share rather than another PVC.

The bot mounts `/matchdata` read-only; only the collector writes.

## Prerequisites

1. **Namespace** — `kubectl create namespace discord` (shared with `vocard`).
2. **csi-driver-smb** — the cluster add-on that backs the SMB PV. Already
   installed; verify with `kubectl get csidriver smb.csi.k8s.io`.
3. **The SMB share** — *only needed for the collector*. Create a dataset and
   SMB share named `smite` on the TrueNAS box (`192.168.4.36`), readable and
   writable by the same user the other charts use. The collector's `Retain`
   reclaim policy means deleting the release never deletes the corpus.
4. **Credentials** — `cp values.local.yaml.example values.local.yaml` and fill
   in the Discord token plus the Hi-Rez dev ID and auth key.

## Deploying

```sh
./build.sh      # build + push ghcr.io/zdiemer/smitele-bot (public package)
./upgrade.sh    # helm upgrade --install into the discord namespace
```

The bot runs happily without the NAS share — it finds an empty corpus and skips
the match-derived features. To turn the collector on once the share exists, set
in `values.local.yaml`:

```yaml
matchData:
  enabled: true
  username: "…"
  password: "…"
collector:
  enabled: true
```

`collector.enabled` without `matchData.enabled` fails the render on purpose:
the job would otherwise write a day's corpus to a pod filesystem that
disappears with the pod.

### Backfilling a missed day

The collector defaults to *yesterday* — Hi-Rez only publishes match IDs for
completed days. Pass an explicit date to collect a different one:

```sh
kubectl -n discord create job --from=cronjob/smitele-bot-collector smitele-backfill
kubectl -n discord set env job/smitele-backfill SMITELE_COLLECT_DATE=2026-08-01
```

or run the script directly with the date as its first argument.

## Configuration

Credentials load from the environment first and `config.json` second, so a
local checkout keeps working from a file while the cluster gets a Secret:

| Env var | `config.json` key |
|---|---|
| `SMITELE_DISCORD_TOKEN` | `discordToken` |
| `SMITELE_HIREZ_DEV_ID` | `hirezDevId` |
| `SMITELE_HIREZ_AUTH_KEY` | `hirezAuthKey` |

Paths are all overridable, which is what lets the container split small state
from the big corpus:

| Env var | Default | Holds |
|---|---|---|
| `SMITELE_DATA_DIR` | `.` | session, version, gods/items caches |
| `SMITELE_CACHE_DIR` | `$SMITELE_DATA_DIR/cache` | downloaded art |
| `SMITELE_MATCH_DATA_DIR` | `src/match_data_collector/output` | the corpus |
| `SMITELE_MATCH_ARCHIVE_DIR` | `src/match_data_collector/archive` | corpus >30 days old |
| `SMITELE_CONFIG_FILE` | `config.json` | credentials, if not in the environment |
| `SMITELE_COLLECT_DATE` | yesterday | collector target day, `YYYY-MM-DD` |

## Hi-Rez API notes

The developer account allows **75,000 requests/day** and **500 sessions/day**
(`getdataused` reports current usage). A full collector run costs roughly 1,700
match-ID requests — 24 hours × 6 ten-minute windows × 12 queues — plus one
request per 10 matches for details, so a day lands in the low thousands with a
lot of headroom.

There is **no rate limiting** in the client. What it does have is session reuse
with automatic re-auth on expiry, which protects the session cap, and 10-ID
batching for match details, which cuts the detail request count by an order of
magnitude. Requests are issued sequentially, so nothing floods. If the quota
ever gets tight, a throttle in `_Base._make_request` is the place to add one.

### Smite 2

Not supported, and not currently possible: Hi-Rez has never opened a public
Smite 2 API — access has been limited to selected partners, with "we are still
working on the best way to open this up to a wider set of developers" as the
standing answer. Everything here talks to the Smite 1 API at
`api.smitegame.com/smiteapi.svc`, which is still live (patch 12.1 as of writing).

The client is shaped so this is a small change if that ever lands: `_Base`
takes a `base_url` and handles signing and sessions generically, and `Smite` is
a thin subclass that only sets `BASE_URL` and the route wrappers. A `Smite2`
subclass alongside it would reuse all of the auth machinery. What would *not*
carry over is anything keyed to Smite 1 content — `GodId`, `QueueId`, item
data, and the scraped Smite wiki URLs are all game-specific.

#### tracker.gg as a third-party source

Since Hi-Rez has not opened up, the only other place carrying per-match Smite 2
builds is tracker.gg. Its internal API was probed on 2026-08-06; what follows is
what that found. **Nothing here is implemented** — this is a feasibility record
so the question does not have to be re-answered from scratch.

The site slug is `smite2` (`smite-2` 404s) and the API host is `api.tracker.gg`.
Both the site and the API refuse plain HTTP clients — the site serves a
Cloudflare challenge, the API its own WAF block — so the probe drove a real
browser (Camoufox) to get past them. Confirmed endpoints:

| Endpoint | Notes |
|---|---|
| `/api/v2/smite2/standard/matches/{platform}/{handle}` | 25 matches, ~2.9 MB, pages with `?next=N` (≥80 pages deep, ~1 year) |
| `/api/v2/smite2/standard/matches/{matchId}` | adds nothing the list above lacks |
| `/api/v2/smite2/standard/profile/{platform}/{handle}` | ~1.3 MB |
| `/api/v2/smite2/standard/profile/{platform}/{handle}/segments/{god,gamemode,role}` | 87 god segments for an active player |
| `/api/v1/smite2/standard/leaderboards?type=stats&board=…` | 50 players/page; boards are `Kills`, `Wins`, `Assists`, `Damage`, `GoldEarned`, `XpEarned`, `TimePlayed` |

Platforms are `steam` (steamid64), `epic`, `psn` and `xbl` (handles).
`segments/overview`, `segments/item`, `sessions` and every `/api/v1/…/profile/…`
route return "not implemented". The ranked `SkillRating` board 500s on every
parameter set tried; it appears to be reachable server-side only.

Two things a crawler depends on were checked rather than assumed. **Arbitrary
players are queryable**: four players taken only from inside another player's
match segments — never on a leaderboard, possibly never having visited the site
— all returned 200 with `requestingPlayerAttributes` echoing the queried id, so
a snowball can keep expanding. And **history is deep**: `?next=80` still returned
25 matches and a live cursor, reaching back about a year with no page overlap.

No date or time-range parameter was found on any route. That is the single most
consequential gap, for the reason the daily-job section below sets out, and it
was not established by exhaustive fuzzing — only by the absence of any such
parameter in the site's own requests. If one exists, much of what follows gets
cheaper.

The match-list endpoint is the one worth having. Each response carries all ten
players of all 25 matches, and every player row includes the god, assigned and
played role, a `buildId`, ~55 stats, and a full ordered item list — 250
player-builds per request. Per-match detail is therefore redundant.

Two things make it awkward to feed the existing corpus. **Item identity is a
slug**, not an integer: `build_features.annotate` wants `Dict[int, Item]` and
keys `IsFullBuild` off `item.tier >= 3`, and tracker.gg publishes no tier — an
`equipmentType` of `starter`/`relic`/`item-passive`/`item-active`/`talent` is
all there is, so `is_starter` survives the move and `tier` does not. And
**`ActiveId1`/`ActiveId2` have no analogue**: Smite 2 gives one relic and one
starter where Smite 1 gives two relics, so `RELIC_COLUMNS`, `EMPTY_RELIC_IDS`
and `IsFullRelics` are all Smite 1 shapes. Talents ("aspects") are new and have
no Smite 1 concept at all.

The six-item model does survive. The `items` array is variable length (3-9 over
250 sampled rows) but positionally structured: 1 is the starter, 2 the relic,
3-8 the six core item slots, with talents appended after. `ItemId1..6` maps onto
positions 3-8, and `hash_builds` is order-independent and slot-count-agnostic,
so it works unchanged on a six-wide matrix.

Three details that will silently corrupt a build if missed:

- `position` is **not contiguous** — `[1, 2, 4, 5, 6, 7]` is ordinary and means
  an unfilled slot. Zipping the array to slots by index shifts every item down.
- Array length varies per row. There is no fixed width to rely on.
- Their data has dirt in it. At least one god comes back as the unnormalized
  `Gods.CuChulainn`, and some items arrive as `equipmentType: "unknown"` with a
  hex id and a name like `Unk Item 2D71`.

#### What it would cost to collect

The collection model inverts. `match_data_collector` enumerates *every* match by
queue and ten-minute window, which is why the corpus is an unbiased daily
sample. tracker.gg exposes matches only per player, so a collector has to seed
from the leaderboards and snowball through the nine other players in each match.

Discovery is free: one request surfaces 151 distinct players, so the frontier
reaches any plausible player base in three hops. Collection is the cost, and it
scales with how much coverage is wanted. At the 1.5 s pacing the probe used
without drawing errors, and 2.9 MB per response:

| Player base | Census (every player's own history) | Observe every player as an opponent |
|---|---|---|
| 1,000 | 1,000 reqs · 25 min · 2.9 GB | 46 reqs · 1 min · 0.1 GB |
| 10,000 | 10,000 reqs · 4.2 hr · 29 GB | 610 reqs · 15 min · 1.8 GB |
| 100,000 | 100,000 reqs · 1.7 days · 290 GB | 7,600 reqs · 3.2 hr · 22 GB |
| 1,000,000 | 1,000,000 reqs · 17 days · 2.9 TB | 91,500 reqs · 1.6 days · 265 GB |

Bandwidth binds long before time does: a census of any serious player base moves
hundreds of gigabytes to extract a corpus the Smite 1 collector produces in ~6 MB
a day. The right-hand column treats each request as 250 build samples rather than
one player's history, which is the right way to think about it, but its
coupon-collector maths assumes players mix uniformly and **they measurably do
not**. Two steam ids sampled from the same match history turned out to share
25 of 25 recent matches — a duo that queues exclusively together. Premades mean
the ten players in a match are not ten independent draws; the effective number is
nearer six. Querying both halves of a duo is entirely wasted, and the tail to
full coverage is worse than the table shows.

Whatever gets built, the sample is drawn from players tracker.gg happens to know
about, seeded from leaderboards. That is a skill-biased population, and the
build aggregate currently assumes it is looking at an unbiased one.

#### A daily job cannot be complete the way this one is

The obvious shape for this is what `match_data_collector` already does: a nightly
job that collects every match from the previous calendar day. That is worth
ruling out explicitly, because it is the natural thing to reach for and it does
not work.

Hi-Rez enumerates matches **by time** — queue × hour × ten-minute window — so
completeness is structural. About 1,700 requests and the day is provably whole.
tracker.gg has no time enumeration, only player lookup, which creates a
bootstrapping problem: *you cannot know who played yesterday without querying
them*, and a query costs the same whether that player played twenty matches or
none.

Two measured inefficiencies compound it. **One page spans about three days** —
the sampled player's 25 most recent matches covered three calendar days at
7/17/1 — so only ~8 of the 25 matches in a 2.9 MB response fall on any given
target day, and with no date parameter there is no way to narrow it. And every
match arrives once per queried participant, which is not waste to be engineered
away but the very mechanism that provides coverage.

Estimating ~115k matches/day at 5 matches per active player gives ~230k daily
actives. With the effective six-independent-players figure from above:

| Coverage of one day | Requests | Time @1.5 s | Transfer |
|---|---|---|---|
| 90% | 73,000 | 30.6 hr | 208 GB |
| 95% | 91,000 | 37.7 hr | 256 GB |
| 99% | 123,000 | 51.4 hr | 350 GB |

**None of that fits in a day.** Landing 95% inside a twenty-hour window needs
0.80 s/request sustained, and whether tracker.gg tolerates that is precisely the
thing that cannot be established without probing a rate limit — which is how one
gets blocked. The rows are also optimistic: they assume only players who actually
played yesterday get queried, where a real roster pays full price for its
inactives too.

So daily completeness costs ~250 GB and 40+ hours to approximate what the Smite 1
collector gets in 1,700 requests and 6 MB. The sensible reading is that
completeness is a property this source structurally cannot provide, and chasing
it is the wrong goal:

| Requests/day | Target-day matches | Player-build rows | Transfer |
|---|---|---|---|
| 4,000 | 33,000 | 330,000 | 11 GB |
| 8,000 | 66,000 | 660,000 | 23 GB |

8,000 requests — 3.3 hours, 23 GB — yields ~660k player-build rows from the
target day, the same order as the Smite 1 corpus's daily volume, at a fifteenth
of the cost of chasing 95%. Aggregate build win rates need sample size and low
bias, not a census.

The catch is that the cheap sample is the biased one. Coverage concentrates on
high-activity players, and the matches missed are disproportionately casual,
low-activity lobbies — the population whose builds differ most from the sampled
head. That bias, not the bandwidth, is the real cost, and nothing in
`build_aggregate` currently models it.

Both tables rest on an estimated concurrency figure rather than a measured one.
Sampling match timestamps across a few hundred random players would replace the
estimate with a real production rate, and should happen before anyone commits to
these numbers.

#### Practical notes

The `cf_clearance` cookie a browser mints is **portable** — replaying it from
`urllib`, with a completely different TLS fingerprint, still returned 200 — so a
collector needs a browser only to mint clearance, not per request. This also
keeps multi-megabyte payloads out of the browser, which matters because parsing
several of them inside the page is enough to OOM it.

And this is an undocumented endpoint behind a WAF with no published rate limit or
terms allowance for bulk pulls: cache aggressively, pace deliberately, and expect
it to break without notice.
