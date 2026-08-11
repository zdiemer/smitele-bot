# Smite 2 live-match data: what is reachable, and why the lobby lags

`/livematch` for Smite 2 reads tracker.gg, whose live snapshots refresh only
about every ten minutes. This document records the investigation into whether
anything fresher exists, so the question does not have to be re-opened from
scratch. The short answer, **revised 2026-08-11 against a real captured token,
and it is the opposite of what the spec-derived version concluded**: a player's
own token can read another player's live session *and* resolve a Steam id to the
RallyHere UUID that read needs. Every hop is open. The whole chain —
`steam id → uuid → live session → roster` — was walked end to end against the
bot's own roster and came back 200 at each step. The freshest source is not out
of reach; it is a capture and a few requests away.

The earlier belief that it was gated came from reading the OpenAPI spec's
per-endpoint permission notes and assuming a player token carries the minimum
they describe. It does not: the captured token holds `session:read-player:any`,
and the un-permissioned `/users/v1/player` lookup resolves handles that the
permission-gated `/users/v1/platform-user` refuses. Two wrong assumptions
pointing the same way made a reachable thing look walled.

Everything in the RallyHere section below is now **measured against the live
backend**, not read off the spec. Where a hop was confirmed only against idle
players (no live match was in progress during the capture), it says so.

## The four sources, ranked by freshness

| Source | Freshness | Granularity | Verdict |
| --- | --- | --- | --- |
| RallyHere session API | seconds | full lobby | **open** end to end for any roster player — steam id resolves to uuid, uuid reads the live session |
| RallyHere presence API | seconds | fine (online + client state, e.g. `InLobby`) | open; carries no session id, so it pairs with the session read |
| Steam `GetPlayerSummaries` | seconds | coarse (running the game) | **shipped** as today's fallback; RallyHere supersedes it |
| Discord guild presence | seconds | coarse (running the game) | viable, not yet built |
| tracker.gg `/live` | ~10 min | full lobby | what `/livematch` uses today |

## RallyHere: the real source, and it is reachable

Smite 2 runs on the RallyHere backend, and tracker.gg is downstream of it — so
RallyHere is as fresh as the data gets. Its API is publicly documented
([github.com/RallyHereInteractive/openapi-spec-environment][spec]), served
per-environment. For Smite 2 the host is `api-smite2.titanforgegames.com` — Titan
Forge fronts its RallyHere environment behind that custom domain via CNAME, which
is why sniffing the client only ever showed it and never an
`<env-id>.rally-here.io` host (the `rally-here.io` DNS lookups are the SDK
resolving sibling services). The env id is visible in the token's `iss` claim,
not needed to make requests.

The endpoints that answer "who is in this player's match right now" all exist,
and — measured against a real captured player token on 2026-08-11 — a player
token may call every one of them. There are three hops and none is gated.

**1. Steam id to player UUID — OPEN, via the right route.**
There are two lookup routes and only one works for a player token, which is the
whole trap the spec-reading fell into:

* `GET /users/v1/platform-user?platform=Steam&platform_user_id=…` — **403**,
  `Insufficient Permissions - Expected any of: user:platform:read, user:*`. The
  token holds neither. This is the route the earlier analysis found and called
  the wall.
* `GET /users/v1/player?platform=Steam&identities=<steamid>` ("Lookup Player By
  Portal") — **200**. It lists *no* required permission, only a bearer, and
  resolves any Steam id to its RallyHere player. Confirmed against five roster
  Steam ids; each returned a `player_uuid` and the correct display name
  (`StarFoxA`, `Indelmaen`, …). `identities` is an array, so a whole roster
  resolves in one request.

The UUIDs it returns are RallyHere's deterministic per-identity ones — v5, a
hash of the platform identity — so they are **stable forever** and can be cached
indefinitely. Resolve a roster once and the mapping never has to be rebuilt.
(One quirk: the token owner's *own* Steam id returns empty from this route,
while every other player resolves. Self comes from the token's
`active_player_uuid` claim anyway, so it costs nothing.)

**2. Player UUID to their live session — OPEN, cross-player.**
`GET /session/v1/player/{player_uuid}/session`. The spec says reading anyone but
yourself needs `session:*` or `session:read-player:any` and predicts a player
token carries only `:self`. It carries both — the captured token's 22 session
permissions include `session:read-player:any` verbatim. Called against every
resolved roster UUID: all 200. (Each returned an empty session — nobody was in a
match at capture time — so the *populated* envelope shape is confirmed reachable
but not yet parsed against real data. `smite2.rallyhere._session_refs` walks for
`session_id` wherever it sits, rather than betting on one shape, for exactly this
reason.)

**3. Session id to roster — permitted, not yet exercised.**
`GET /session/v1/session/{session_id}/player` accepts `session:read:self`, which
the token has. Untestable until a roster member is actually in a match; the
probe reads it automatically when hop 2 returns a session id.

### Where that leaves a bot

The complete-lobby path the earlier version of this document closed is in fact
open for exactly the population this bot serves: anyone whose Steam id is on the
roster. The chain is `steam:7656… → uuid (cached) → live session → roster`, all
in seconds, all with one captured player token. `src/HirezAPI/smite2/
rallyhere.py` implements hops 1 and 2 (`uuid_by_steam`, `roster_status`,
`status`); hop 3 (`session_players`) is wired and waits only on a live match to
read.

### Presence: a companion read, not a bridge

Presence does not carry a session id, so it does not replace hop 2 — the two are
read together, presence for coarse online/state and the session read for the
lobby. Read live, the whole presence document is:

    {"status": "offline", "message": "{\r\n\t\"state\": \"InLobby\"\r\n}",
     "platform": "Steam", "display_name": "…", "custom_data": {},
     "player_uuid": "…", "do_not_disturb": false}

No session id, no match id, and `custom_data` — the free-form blob that was the
one hoped-for crack — is empty. But `message` is more than the schema promises:
Smite 2 publishes a small JSON document there carrying the client's own state
(`InLobby`), which is finer than Steam's "is running the game". Note the trap in
that sample — `status` is `offline` while the message still says `InLobby`. The
message outlives the presence that set it, so it means something only when read
beside `status`. `smite2.rallyhere.PlayerStatus` exposes it as `state` and says
so.

### Token lifetime: a solved problem, not a cost

The earlier version of this document put bearer lifetime at "minutes to about an
hour" and assumed keeping one alive meant replaying a Steam session ticket
through a headless fake client. Measured:

* An access token lives **6 hours** (`expires_in: 21600`).
* It renews with no game and no Steam ticket at all: `POST /users/v1/login` with
  `{"grant_type": "refresh", "portal_access_token": <refresh token>,
  "include_refresh": true}` and the client's `Authorization: Basic`
  credential — both of which one mitmproxy capture yields.
* The renewal returns the **same** refresh token, not a rotated one. So a single
  capture sustains a session indefinitely, until that refresh token is revoked.

`src/HirezAPI/smite2/rallyhere.py` implements this, and `scripts/win/` does the
one-time capture.

### The courier-account idea

Moot. It was proposed to work around a hop-2 wall that turned out not to exist,
and it would have hit hop 1 anyway. There is no wall left for it to clear. The
one real question it raised — *whose* token the bot runs on — still stands, and
the answer is "yours, scoped to you and your friends" (see ToS, below).

### Terms of service, which the measurements do not change

That the permission is present does not make unattended use of it sanctioned.
The token is a game client's, kept alive by replaying that client's embedded
credential; pointing it at players who are not you and your consenting friends
is an unauthorized automated client, and is exactly the behaviour that gets an
endpoint locked down for everyone. The scope this is built for — your own and
your friends' status, at a human's request — is a personal-use call you are
entitled to make. Publishing the client credential is not.

## Steam presence: what shipped

`ISteamUser/GetPlayerSummaries` returns `gameid` whenever a **public** profile
is in a game; Smite 2's Steam appid is `2437170`. It answers in seconds, needs
only a free Web API key (`steamcommunity.com/dev/apikey`, not OAuth), and
authenticates the caller rather than the target — so it works for anyone whose
game-details visibility is public.

Its ceiling is the whole point: the Steam Web API exposes **no rich-presence
strings**, so "running Smite 2" is the entire resolution. Menus, queue and match
are indistinguishable, and it is Steam-only (not Epic / console). That is enough
to turn `/livematch`'s worst failure — a flat "isn't in a match" to someone
standing in the fountain — into "is in Smite 2 right now, tracker.gg just hasn't
posted the lobby yet."

Implemented in `src/HirezAPI/smite2/steam.py`, wired into `/livematch` in
`player_stats.py`, keyed by an optional `SMITELE_STEAM_API_KEY`
(`credentials.steamApiKey` in `values.local.yaml`). Absent a key, or for a
private profile / non-Steam handle / any error, it returns `None` and the
command answers exactly as it did before. Note the asymmetry a private profile
creates: a player whose own game-details are friends-only cannot be seen by this
probe, so it helps for looking *others* up more than for looking yourself up.

## Discord presence: viable, not built

Smite 2 is a Discord-detectable game (application id `1334654007884517509`,
`hook: true`), so a bot with the privileged `GUILD_PRESENCES` intent can see a
guild member is "Playing SMITE 2" in seconds. Same coarseness ceiling as Steam
unless the game publishes Rich Presence detail strings, for which no evidence
was found. Covers only members of a shared guild who share activity status. Not
built; noted here so the option is on record.

## tracker.gg: no faster public route

tracker.gg's own staff state they cannot offer a Smite 2 developer API. The
`api.tracker.gg/api/v2/smite2/...` routes the site uses are Cloudflare-gated and
inherit the same ~10-minute ingest, so bypassing the WAF buys no freshness. The
Overwolf overlay is fast only because it reads the *local* game client, not a
remote API. Other tracker sites (smite2.live, smitetracker.com, smitesource) are
all downstream of the same slow ingest and expose no faster public feed.

## Bottom line

The precise lobby exists in RallyHere, is fresh to the second, and one captured
player token reads the whole chain to it: a roster Steam id resolves to a stable
RallyHere UUID (`/users/v1/player`, no permission required), the UUID reads that
player's live session (`session:read-player:any`, present in the token), and the
session id reads its roster. All three hops returned 200 against real roster
players on 2026-08-11; only hop 3's populated shape awaits a live match to read.
The earlier conclusion — that this was gated and the coarse Steam signal was the
ceiling — was wrong, from reading the spec's permission notes instead of the
token.

So the Steam fallback is now a *fallback*, not the ceiling: it covers the moment
a token is uncaptured or a handle is off-roster, while RallyHere answers
precisely and in seconds for everyone the bot actually tracks. The remaining work
is integration, not discovery — wiring `smite2.rallyhere` into `/livematch`
ahead of tracker.gg — plus honoring the scope the client's own docstring draws:
you and your consenting friends, never an unattended reader of strangers.

[spec]: https://github.com/RallyHereInteractive/openapi-spec-environment
