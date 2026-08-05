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
