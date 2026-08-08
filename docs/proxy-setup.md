# Routing the Smite 2 crawl through a proxy

The Smite 2 collector reads an undocumented tracker.gg endpoint behind
Cloudflare. Everything it does is shaped by that — a solved clearance cookie, a
Firefox TLS fingerprint that has to match the user agent the cookie was minted
with, and a fixed gap between requests. This document covers the one piece that
cannot live in the chart: choosing an address for that traffic to leave from,
and proving it works before you depend on it.

You do not need any of this to run the crawl. Leave `credentials.egressProxy`
empty and traffic leaves from the cluster's own address, which is what every
deployment did before this existed.

## When you would want one

Two reasons, and only the second is a good one.

**A 429 — but read what it says.** The distinction that matters is the
`Retry-After` the run reports:

- **A short one** (seconds to a couple of minutes) is a pacing signal. The crawl
  widens its interval, waits, and carries on; you do not need a proxy, and
  raising `requestIntervalSeconds` is the cheaper fix if it keeps happening.
- **A long one** — `Retry-After: 3600` — is the hourly quota resetting. The
  crawl now *waits it out and resumes* rather than treating it as a ban, so this
  no longer ends a night. A proxy still helps, but as a multiplier rather than
  as a rescue.

### The limit, measured

**It is a request quota, not a rate and not a volume: 300 requests per address
per hour.** Measured 2026-08-07 through two independent proxies, so establishing
it cost nothing on the address the crawl depends on:

| exit | pace | refused at | elapsed | wire |
|---|---|---|---|---|
| `147.45.60.124` | 1.5 s | request **300** | 10.3 min | 92 MB |
| `192.9.171.168` | 1.0 s | request **300** | 13.9 min | 92 MB |

Both carried `Retry-After: 3600`. The count is identical and the elapsed time is
not, which is what rules out a rate. An earlier note here recorded a refusal at
187 requests after 309 at a *faster* pace and concluded "cumulative daily
volume"; the truth is simpler — that run began partway into an hour it had
already spent, and there was never a rate to find.

Three consequences:

- **Raising `requestIntervalSeconds` cannot buy allowance.** It only changes how
  early in the hour the quota is spent. 12 s is the quota rate exactly, and is
  the default for that reason — it spreads the 300 over the hour rather than
  firing them in ten minutes.
- **The run length is what decides the night.** 300/hour over an 18-hour run is
  ~5,400 requests, against the ~300 a run got when a reset ended it.
- **Each address gets its own 300.** The quota is per-egress, as are the cookie,
  the mint breaker and the stand-down, so exits add allowance linearly.

**Separating this traffic from your home address.** A better reason. The crawl
is a sustained multi-gigabyte pull against a WAF that has no allowance for it,
and keeping that off the address your household browses from is prudent
regardless of whether anything is currently failing.

## The two hard constraints

### One exit address, held for hours

The clearance cookie is bound to the address that solved the challenge, not just
to the user agent. A cookie minted on one exit and replayed from another is
refused. That refusal is expensive rather than merely useless: every 403
discards the cookie and mints a replacement, and there are only twelve solves a
day before a four-hour breaker arms.

**A rotating proxy pool cannot work here.** Not "works worse" — it fails within
minutes and then stands down for four hours. The exit must be stable for at
least the cookie's useful life, which runs to roughly six or seven hours. A
static IP, or a provider's sticky-session endpoint with a session length you can
actually configure.

The collector checks this for you: it records the address at mint time and again
at the end of the run, and stops with `EgressChanged` rather than minting into a
loop if they disagree.

### Bandwidth, which is far less than this document used to claim

Each match-list request *decodes* to about **2.9 MB** — one page carries all ten
players of 25 matches, and that density is the only reason a crawl is viable at
all. But it arrives **Brotli-compressed, at roughly 180 KB on the wire**, a
ratio of about 16x. curl_cffi's Firefox impersonation negotiates the encoding on
its own, so this has always been true; the figures here were derived from
`TrackerClient.bytes`, which counts chunks *after* decoding and therefore
overstates transfer by that factor.

| Workload | Requests | Decoded | On the wire |
|---|---|---|---|
| Nightly (18h, quota-bound) | ~5,400 | ~15 GB | **~1 GB/night → ~30 GB/month** |
| Full backfill (`--budget 14000`) | 14,000 | ~41 GB | **~2.5 GB/run** |

This does *not* eliminate metered residential proxies the way the old numbers
did — ~30 GB/month is an ordinary bill rather than several hundred dollars. The
binding constraint is stickiness, not gigabytes: one address held for the life
of a cookie.

`bytes` in the run record and on the status page is still the decoded figure.
Divide by ~16 for what actually crossed the network.

## What to use

### Start with a VPS running a proxy

A ~$5/month VPS with `dante` or `squid` and password auth. 118 GB/month sits
comfortably inside the 1–2 TB most plans include, and `ssh -D` is enough for a
first test before you configure anything properly.

The exit is a datacenter ASN, which is the one real strike against it. But what
carries this past Cloudflare is the solved cookie and the Firefox handshake, not
the address's reputation — and if you are setting this up in response to a 429,
you do not yet know the address was the problem. This tier costs almost nothing
to disprove, and the collector is provider-agnostic, so moving up costs a
config change rather than a rewrite.

### Escalate to a static ISP proxy only if that gets challenged

Sold as "static residential" or "ISP proxy": a residential ASN, datacenter
hosted, dedicated IP, billed per IP per month rather than per gigabyte, usually
with unmetered or generous bandwidth. That combination — residential ASN *and*
flat rate *and* a fixed address — is the only commercial category that satisfies
all three requirements at once. Several providers sell it; pricing moves
constantly enough that it is worth comparing at the time rather than trusting a
figure written here.

### What not to use

| | Why not |
|---|---|
| Rotating residential pools | Billed per GB, *and* the exit changes per connection. Both constraints violated. |
| Consumer VPNs | Exit IPs are shared among many users and are heavily pre-flagged by Cloudflare — often worse than your own address. Most offer no way to pin one exit for six hours. |
| Free proxy lists | Unstable exits, and you are handing an unknown third party your traffic. |

## Setup

1. **Buy one static or sticky exit.** Budget ~120 GB/month, plus ~36 GB for each
   backfill you plan to run.

2. **Prefer username/password auth over IP allowlisting.** Many providers offer
   both. The cluster's own outbound address can change, which breaks allowlist
   auth silently and mid-run.

3. **Confirm the protocol.** HTTP `CONNECT` or SOCKS5 both work — the crawl and
   the headless browser that mints the cookie accept either. Note the exact URL
   form: `http://user:password@host:port` or `socks5://user:password@host:port`.

4. **Verify the exit is sticky.** Query an IP echo through the proxy, wait
   several hours, query again. Two different answers mean the tier is wrong and
   the cookie will not survive. This is the cheapest check that rules out a bad
   provider, so do it before step 5.

   ```sh
   curl -s --proxy 'http://user:password@host:port' https://api.ipify.org
   ```

5. **Verify Cloudflare will serve that address at all**, before committing it to
   the chart:

   ```sh
   SMITELE_EGRESS_PROXY='http://user:password@host:port' \
     python src/smite2_collector/collect.py --check-egress
   ```

   This resolves the outbound address, mints one cookie through the proxy,
   issues a single API request, and prints the address seen at each stage. It
   writes nothing and does not crawl. A pre-flagged IP fails here, which is
   where you want to find out — discovering it during a nightly costs a solve
   out of a budget of twelve.

   All three stages must report the **same** address. If the mint and the
   request disagree, something is proxying one and not the other; check that no
   `HTTPS_PROXY` is set in the environment, since that is the classic way to get
   a proxied crawl with an unproxied mint.

6. **Wire it in.** The URL holds a password, so it belongs in
   `values.local.yaml` with the other credentials, never in `values.yaml`:

   ```yaml
   credentials:
     egressProxy: "http://user:password@host:port"
   ```

   ```sh
   ./upgrade.sh
   ```

   The bot and the s2collector pick it up; the aggregates and the trainer do not
   receive it, because they never talk to tracker.gg.

7. **Watch bandwidth for the first week** against whatever your plan includes,
   before pointing a 36 GB backfill through it.

## Verifying after deploy

```sh
kubectl -n discord create job --from=cronjob/smitele-bot-s2collector smite2-crawl-manual
kubectl -n discord logs -f job/smite2-crawl-manual
```

The run reports the address it minted on and the address it finished on. They
must match, and both must be the proxy rather than your own address.

The clearance file is keyed by egress, so you can confirm what it believes:

```sh
kubectl -n discord exec deploy/smitele-bot -- \
  sh -c 'cat /matchdata/smite2/clearance.json'
```

Expect a bucket named for the proxy with its credentials stripped —
`http://host:port`, no username or password. A credential appearing in that file
is a bug worth reporting.

## Should the bot use it too?

By default, yes. The bot and the collector deliberately share one clearance
file so the bot reuses the collector's solve rather than minting its own; a
cookie lasting six or seven hours means the bot usually never mints at all.
Splitting them across two addresses halves that sharing and doubles your daily
solves against a budget of twelve.

Set `bot.useEgressProxy: false` if you would rather not spend proxy bandwidth on
the bot's handful of per-command requests. That is a legitimate trade — the
per-egress keying in the clearance file is what makes it *safe*, since the two
processes will simply keep separate cookies instead of handing each other one
neither can use.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every request 403s immediately after a successful mint | The mint and the crawl left from different addresses. Usually `HTTPS_PROXY` set in the environment, which the crawl honours and the browser does not — configure the proxy only through `credentials.egressProxy`. |
| `EgressChanged` | The exit moved mid-run. The proxy is rotating; you need a sticky or static tier. |
| `backing off for another N min` | The breaker armed after twelve mints in 24h. Something is invalidating cookies faster than they should expire — almost always the two rows above. `--reset-clearance` clears it once the cause is fixed. |
| `no route to the internet through <proxy>` | The proxy refused or timed out before Cloudflare was reached. Credentials, allowlist, or the provider being down. Deliberately does *not* arm the breaker. |
| Mint succeeds, crawl 403s only after some hours | Normal cookie expiry if it recovers on its own. Persistent means the exit changed. |
| `STANDING DOWN — 47 min left of a refusal recorded for …` | Working as intended. A previous run was handed a long `Retry-After` and recorded the deadline, so this one declined to start. See below. |

## Stand-downs

A long `Retry-After` is recorded to `tracker_cooldown.json` beside the clearance
state, keyed by egress, and outlives the run that earned it. Any crawl starting
inside the window refuses before issuing a single request and exits 3 — a
distinct code, because declining to crawl is a different outcome from crawling
and failing.

This exists because the deadline used to die with the process, so the nightly
would fire into a live ban at 02:40, collect nothing, and spend reputation
confirming it.

Three things follow:

- **It is keyed by address.** A ban on your home connection does not stand down
  a proxied crawl, which is the mechanical reason a proxy is a remedy rather
  than a workaround.
- **`--check-egress` reports it but does not obey it.** Checking is how you find
  out whether a ban has lifted, and one request is not what re-earns it.
- **`--reset-cooldown` overrides it**, for when you know the ban is over. It is
  deliberately manual — kept separate from `--reset-clearance`, so that clearing
  a stuck cookie cannot quietly clear a WAF ban as a side effect.
