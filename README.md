# smitele-bot

Smite-le — a Discord bot for a six-round Smite guessing game in the shape of
Wordle — plus the daily Hi-Rez match data collector that feeds it.

This repo holds both the source and the Helm chart that runs it on the home k3s
cluster, so a release is one commit in one place: `Chart.yaml` `appVersion`
tracks `values.yaml` `image.tag`.

## What runs

| Piece | Kind | What it does |
|---|---|---|
| bot | Deployment (1 replica) | The Discord bot. Slash commands for the guessing game, god/item builds, trivia, and player stats — for both Smite 1 and Smite 2. |
| collector | CronJob (daily) | Walks a day of Hi-Rez match IDs across 12 queues, fetches details in batches of 10, and writes `match_details_<date>.json` to the NAS. Files older than 30 days move to `archive/`. |
| s2collector | CronJob (nightly, **off by default**) | Crawls tracker.gg for Smite 2 matches. Snowballs from the leaderboards rather than enumerating a day, because that source has no time enumeration. Writes into `smite2/output/`. |
| s2aggregate | CronJob (nightly, **off by default**) | The same aggregate as Smite 1's, over the Smite 2 corpus. |

### Which game a command answers for

Every command that can differ takes a `game:` option. `/set_game` stores a
per-server default so nobody has to pass it, and `DEFAULT_GAME` in
`src/HirezAPI/game.py` is the one place the global fallback is decided — Smite 1
for now.

A game with no provider is not offered at all: the choice list is derived from
what actually registered, so if wiki.smite2.com is unreachable the bot comes up
Smite-1-only rather than broken.

| Command | Smite 1 | Smite 2 |
|---|---|---|
| `/smitele`, `/trivia`, `$god`, `$item` | ✅ | ✅ from the wiki |
| `/build`, `/edge` | ✅ | ✅ once the crawl and aggregate have run |
| `/random_build` | ✅ | ⚠️ reduced — see below |
| `/queue_stats`, `/rank`, `/worshippers`, `/match_history`, `/first_match` | ✅ Hi-Rez | ✅ tracker.gg, per player |
| `/live_match` | ✅ | ❌ tracker.gg does not expose the lobby |

Two Smite 2 gaps are deliberate. `/random_build` does not use the build
optimizer and `$god -b` does not show derived combat numbers, because
`stat_calculator` and `build_optimizer` encode Smite 1's stat model — Physical
and Magical Power, its protection and mitigation formulas, a 130-entry archetype
table keyed on `GodId`. Smite 2 replaced Power with Strength and Intelligence
and changed the formulas, so running those over its items would produce
confident nonsense. Both show summed item stats instead, which is true in either
game, until that model is rewritten.

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

### Turning the Smite 2 crawl on

Static Smite 2 data needs no configuration — the bot reads wiki.smite2.com on
startup, about twenty requests, and caches it. Match data does:

```yaml
smite2:
  collector:
    enabled: true
  aggregate:
    enabled: true
```

Read `#### What measuring it actually found` below first. In short: this reads an
undocumented endpoint behind a WAF with no published rate limit and no allowance
for bulk pulls, which is why it is off by default and why
`requestIntervalSeconds` has no measured headroom. Raising that number is safe.
Lowering it is the one setting here that could get the address blocked.

Start small and watch the coverage table the job prints:

```sh
kubectl -n discord create job --from=cronjob/smitele-bot-s2collector smite2-crawl-manual
kubectl -n discord logs -f job/smite2-crawl-manual
```

Or locally, which writes nothing:

```sh
python src/smite2_collector/collect.py --dry-run
```

To route that traffic somewhere other than the cluster's own address, see
[docs/proxy-setup.md](docs/proxy-setup.md). Note that a rotating proxy pool
cannot work — the clearance cookie is bound to the address that solved the
challenge — and that the crawl moves ~2.6 MB per request, which rules out
per-gigabyte providers on cost alone.

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
| `SMITELE_EGRESS_PROXY` | `egressProxy` |

`SMITELE_EGRESS_PROXY` is optional and empty by default, in which case tracker.gg
traffic leaves from the host's own address. It reaches only the bot and the
s2collector — the aggregates and the trainer never talk to tracker.gg. Setting it
is not a one-line change: the clearance cookie is bound to the address that
solved the challenge, so see [docs/proxy-setup.md](docs/proxy-setup.md) before
picking a provider.

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
| `SMITELE_S2_MATCH_DATA_DIR` | `<matchdata>/smite2/output` | the Smite 2 corpus |
| `SMITELE_S2_MATCH_ARCHIVE_DIR` | `<matchdata>/smite2/archive` | Smite 2 corpus, rotated |

Smite 1's paths are deliberately unchanged rather than moved under a `smite/`
subtree for symmetry. The corpus is 250 days deep on a network share and the
aggregate is built from whatever `corpus_paths` finds there; relocating it would
risk the bot reading a half-moved directory to gain nothing. Smite 2 gets its own
subtree instead, which also means the two aggregates cannot contaminate each
other — there is a test asserting exactly that, because a mixed aggregate does
not raise, it just reports wrong win rates.

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

In progress, from two third-party sources rather than one. Hi-Rez has never
opened a public Smite 2 API — access has been limited to selected partners, with
"we are still working on the best way to open this up to a wider set of
developers" as the standing answer. Everything Smite 1 here talks to
`api.smitegame.com/smiteapi.svc`, which is still live (patch 12.1 as of writing).

Smite 2 instead comes from **tracker.gg** for per-match builds and
**wiki.smite2.com** for everything static — gods, abilities, items, tiers,
costs, passives, art. Neither source can replace the other: tracker.gg publishes
no god or item metadata at all, and the wiki has no match data. The sections
below record what each was measured to provide.

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

#### What measuring it actually found

`scripts/probe_tracker.py` crawled 120 match pages — 128 requests, 345 MB, 3.2
minutes at 1.5 s pacing — seeded from the leaderboards and snowballed. That is
2,744 distinct matches and 26,444 player-build rows. Several of the assumptions
above did not survive it.

**The cookie is not portable across TLS fingerprints.** That conclusion came
from testing exactly one replacement client. Holding one cookie constant:

| client | result |
|---|---|
| `urllib` | 200 |
| `aiohttp` — every header permutation, `Connection: close`, ALPN cleared, matching SSL context | **403** |
| `curl` (no impersonation) | **403** |
| `curl_cffi` impersonating **Chrome** | **403** |
| `curl_cffi` impersonating **Firefox** | 200 |

Cloudflare is checking that the handshake is *consistent with the user agent*.
Camoufox mints a Firefox UA, so only a Firefox-shaped handshake is honoured —
Chrome impersonation fails *because* the UA says Firefox. urllib passes because
its generic handshake is not classified as a mismatched browser, which is luck
rather than portability. The client therefore uses `curl_cffi` with Firefox
impersonation, and the impersonation target and the minted UA have to stay in
agreement: they are two halves of one identity.

**There is a per-match skill rating.** The earlier finding that the
`SkillRating` *leaderboard* 500s is correct but was over-read. Each player
segment carries `skillRating`, `prematchSkillRating` and `skillRatingDelta`
among its 59 stats. So `/rank` is viable, `/build`'s `high_mmr` is viable, and
`HighMmr` is a real grouping key in the aggregate rather than a constant.

**Position does not identify an item's role, and using it corrupts builds.**
The layout is only approximately "1 starter, 2 relic, 3-8 core":

| | observed |
|---|---|
| talents sitting at positions 3-8 | **2,079** |
| relics at position 1 | 156 |
| items at position 2 | 195 |
| `unknown` (hex-id junk) at positions 3-9 | 1,530 |

Selecting positions 3-8 as the six core slots therefore mis-slots a talent as an
item for ~8% of players — silently, since the result is a plausible build. Core
items must be selected by `equipmentType in {item-passive, item-active}` and
*then* ordered by position. With that rule, 53.2% of players have a full six-item
build, and the core positions are contiguous from 3 in the large majority of rows.

**The join works, and the god join is exact.** Matching tracker.gg's identifiers
against the wiki, weighted by occurrence over 189,140 item slots and 26,444 god
rows:

| | rate | residue |
|---|---|---|
| items | **99.60%** | one item, `brawlers-ruin`, which the wiki does not document (0.40%) |
| gods | **100.00%** | none |

Gods only reach 100% with all four of: stripping the `Gods.` prefix tracker.gg
sometimes emits, collapsing to alphanumerics (`jingwei` ↔ `jing-wei`), stripping
a leading article (`morrigan` ↔ `The Morrigan`), and indexing the **god page
list** rather than `Data:Gods.json` — which is missing Xing Tian entirely while
carrying Bastet twice. Slug matching alone gets 96.09%.

**Premades are measurable, not estimated.** Segments carry `partyId`, populated
for 56% of players, with parties of 2-5. That gives distinct query-units per
match directly: **7.16 of 10**, against the README's estimated ~6 above. It also
makes premade suppression exact rather than inferred from match overlap.

**The vocabulary is wider than the four platforms recorded above.** Observed
modes: `assault`, `arena`, `joust`, `conquest`, `conquest-ranked`, `duel`,
`joust-bots`. Platforms: `steam`, `psn`, `xbl`, `epic`, and also `twitch` and
`ign`. Regions: `nae`, `eu`, `las`, `unk`. Roles come back as `middle`, `carry`,
`support`, `jungle`, `solo`, plus a little dirt — 32 nulls and 6 `ENone`.

**A clearance cookie lasts hours, not half an hour.** Pinned and polled, one
cookie served requests continuously for **6.7 hours** and was still working when
the probe was stopped; the mint log shows no re-issue in that window. The 30
minutes usually quoted is a Cloudflare default rather than this site's setting.
A whole nightly crawl therefore fits inside a single clearance, so nothing needs
pre-warming and the only sensible refresh trigger is an observed 403. (Measuring
this needs a *pinned* cookie: the client's job is to make a 403 invisible by
re-minting, which would have reported success indefinitely while replacing the
thing under measurement.)

**Coverage is measurable but the sample is not the game.** Capture–recapture
(Chapman-corrected, splitting the queried roster in half by hash) over the two
best-sampled days:

| day | matches seen | est. reachable total | coverage |
|---|---|---|---|
| 2026-08-05 | 807 | ~3,300 | 24% |
| 2026-08-06 | 272 | ~670 | 41% |

Read that carefully. The estimator measures the population *reachable from these
seeds*, which for a leaderboard-seeded snowball is the high-activity head, not
the whole player base. It does **not** confirm the ~115k matches/day the tables
above assume, and it does not refute it either — it is a different quantity.
What it does establish is that the collector can measure its own coverage as it
runs, which is what lets a budget be set against a coverage target instead of
against the estimate the tables rest on.

The day-by-day counts also confirm the three-day span structurally: crawling
page 1 only, the sample thins sharply going back — 807 matches on the newest
full day, 480 the day before, 258 the day before that.

#### wiki.smite2.com as the static-data source

tracker.gg carries builds and nothing else. `/smitele` needs lore, titles,
pantheons, abilities and skins; `/trivia` needs item costs, passives and trees;
`build_features.annotate` needs an item **tier**, which the section above records
tracker.gg as not publishing. All of it is on wiki.smite2.com — a Weird Gloop
MediaWiki 1.45 with a public `api.php`, Scribunto, and Weird Gloop's `Bucket`
structured-data extension. No Cargo, no Semantic MediaWiki.

| Source | Gives |
|---|---|
| `Data:Gods.json` — main namespace, the colon is literal | 88 records: `slug`, `name`, `title`, `pantheon`, `primaryDamageType`, `characterTags`, `roleTags`, 20-level `baseStats` curves |
| `bucket("god_infoboxs2")` / `bucket("item_infobox")` | the page-name enumerations: 88 gods, 266 items |
| God pages | `{{God infoboxS2}}`, `==Lore==`, `{{Ability}}` per slot, `{{#invoke:SkinViewer}}` |
| Item pages | `{{Item infobox}}` with `tier`, `cost`, `totalcost`, `type`, `stat1..N`, `passive`, and a nested `{{Recipe}}` tree |
| `Category:Stat templates` | the 26-member `{{Int}}`→Intelligence map, so the stat vocabulary is read rather than hardcoded |

`Category:Items` is **not** the item enumeration — it holds six container
categories and no items. The Bucket query is.

`scripts/probe_wiki.py` ingests every god and every item and scores each field
the domain model needs. Current run: **100% on every scored check.** What that
run established beyond the pass:

- **The tracker.gg join key is free.** All 88 gods carry a `Gods.X`
  `characterTag` — `Gods.CuChulainn` — which is the exact unnormalized token
  tracker.gg emits. No fuzzy name matching needed on the god side.
- **`tier 3` ⇔ `type ∈ {Offensive, Defensive, Hybrid}`**, exactly 133 items, and
  all 133 have both a tier and at least one stat. That is precisely the
  population `IsFullBuild` cares about, so the predicate is fully supported.
  Tier is absent only where it is meaningless: 12 relics, 15 consumables, 5
  curios, 17 god-specific items. Starters are typed `Starter` at tiers 1–2.
- **`Data:Gods.json` has 88 records but 87 distinct slugs.** Bastet appears
  twice, differing only in `title`, and one copy is a stub missing its
  protection curves. Ingestion dedupes on slug preferring the fuller record, so
  the id derived from the slug is the same in every process.
- **Section scoping is load-bearing.** 70 of 88 god pages would over-count
  abilities if the whole page were parsed, because `==God Aspect==` repeats
  every `{{Ability}}` in an enhanced form.
- **Abilities cannot be keyed by name.** Stance and transform gods publish one
  set per form — Merlin 14 across three stances, Artio and Cu Chulainn 10,
  Mordred 8, Yemoja 7 — and names recur across forms with *different* numbers:
  Merlin's Flicker has three distinct stat blocks. They are an ordered list with
  a slot, nothing more.
- The slot vocabulary is looser than it looks. Basic Attack / Passive / 1st /
  2nd / 3rd / Ultimate holds for 86 of 88; Mordred has two Passives and two
  Ultimates, and Yemoja has a `1st Ability (Alt)`.
- Only one god, Princess Bari, has no mana curve. Resource type is a
  `characterTag` — `Character.Resource.Primary.{Mana,Rage,Spirit,Omi,Health.Percent}`
  — which replaces `god.py`'s hardcoded Cu Chulainn / Yemoja special case with
  something data-driven.
- Every stat template in use (20 of the 26 in the category) resolves through
  `Category:Stat templates`, so a new stat added to the game needs no code change.

Cost of a full refresh is ~20 requests and ~4 MB, so this is polled rather than
crawled. There is no `getpatchinfo` equivalent; cache invalidation hashes the
revision ids of `Data:Gods.json` plus every god and item page, which costs ~9
`rvprop=ids` requests and catches a single item's cooldown being edited.
`Data:PatchLogs.json` is not usable for this — it lags the data pages.
