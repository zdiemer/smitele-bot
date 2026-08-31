# Smite 2 live-match data: what is reachable, and why the lobby lags

`/livematch` for Smite 2 reads tracker.gg by default, whose live snapshots
refresh only about every ten minutes. This document records the investigation
into whether anything fresher exists, so the question does not have to be
re-opened from scratch.

The answer came in two parts, a fortnight apart. **2026-08-11:** the complete
lobby is reachable from the game's own backend only through a permission a
player token does not hold, and the fast alternatives there are coarse ("is
this person running the game") rather than precise ("who is in their match") —
so what shipped was the coarse-but-honest version. **2026-08-31:** that wall is
still exactly where it was, but it no longer has to be climbed. smitesource.com
answers the gated hop for us at roughly a four-and-a-half minute cadence, with
the match in progress rather than a roster.

Everything below was checked against primary sources — the RallyHere OpenAPI
spec, the Steam Web API docs, and live tracker.gg and smitesource.com traffic
against real matches — not inferred. Where a claim rests on an observation of a
single live match, it says so.

## The sources, ranked by freshness

| Source | Freshness | Granularity | Verdict |
| --- | --- | --- | --- |
| RallyHere session API | seconds | full lobby | gated — needs an elevated permission a player token lacks |
| RallyHere presence API | seconds | coarse (online/in-game) | reachable, but carries no session id |
| Steam `GetPlayerSummaries` | seconds | coarse (running the game) | **shipped** as the fallback |
| Discord guild presence | seconds | coarse (running the game) | viable, not yet built |
| tracker.gg `/live` | ~10 min | full lobby | the default lobby source |
| smitesource.com `/rpc` | ~4.5 min | full lobby, mid-match stats | **shipped**, behind `SMITELE_LIVE_MATCH_SOURCE` |

## RallyHere: the real source, behind the real wall

Smite 2 runs on the RallyHere backend, and tracker.gg is downstream of it — so
RallyHere is as fresh as the data gets. Its API is publicly documented
([github.com/RallyHereInteractive/openapi-spec-environment][spec]), served
per-environment at `https://<env-id>.rally-here.io`. The `<env-id>` subdomain
for Smite 2 is not published; it is the host the game client's own requests go
to, visible only by sniffing that client's traffic.

The endpoints that would answer "who is in this player's match right now" all
exist. The question is authorization, and reading the spec settles it. There
are three hops, and only one of them is gated:

**1. Handle to player UUID — open.**
`GET /users/v2/player/{player_id}/uuid` and `GET /users/v1/platform-user`
("Find Platform User By Id") map a platform identity to a RallyHere UUID.

**2. Player UUID to their session id — GATED. This is the wall.**
`GET /session/v1/player/{player_uuid}/session`, verbatim from the spec:

> Required Permissions:
> - For any player (including themselves) any of: `session:*`, `session:read-player:any`
> - For the player themselves: `session:read-player:self`

A player token carries `session:read-player:self`. It can ask "what session am
*I* in" and nothing more. Asking the same about *another* player needs
`session:read-player:any`, which is an elevated, service-client permission —
issued through the RallyHere Developer Portal to licensed studios, not minted by
a normal player login.

**3. Session id to roster — open to any member.**
`GET /session/v1/session/{session_id}/player` accepts `session:read:self`, not
just `:any`. Given a session id, a player token reads the whole roster.

### Why the in-game client can show you other players' matches

Because hop 3 is open. The client shows a friend's lobby, a party's lobby, or a
match you can spectate by already *holding* the session id — through a party,
an invite, or the match you are in — and then reading the roster with the
self-scoped permission every token has. What the client never does, and what a
"look up any stranger" bot would need, is hop 2: turning an arbitrary handle
into that stranger's live session id. That specific capability is the gated one.
The in-game visibility and the documented wall are not in tension; they are two
different hops.

### Presence does not bridge it

The obvious crack would be presence: if `GET /presence/v1/player/uuid/{uuid}/presence`
carried a session id, presence (broadly readable) would hand you the id that
hop 2 withholds. It does not. The `PlayerPresence` schema is:

    status, message, platform, display_name, custom_data, player_id,
    player_uuid, do_not_disturb, last_seen

No session id, no match id. Presence tells you online / in-game / offline and a
free-text `message`, which is the same coarse signal Steam and Discord give for
free. The one unverified crack: `custom_data` is a free-form blob, so *if* Smite
2 populates it with a match reference, presence would become the bridge. That is
an empirical question `scripts/probe_rallyhere.py` can answer against a live
friend; it is not promised by the schema.

### The courier-account idea

A dedicated throwaway ("courier") Steam account whose token the bot uses solves
the credential-hygiene problem — nothing of yours is embedded in the bot — but
it does **not** clear hop 2. A courier is still a player, still gets
`session:read-player:self`, still cannot look up a stranger's session. The one
case it helps: reading the courier's *own* lobby, if the courier is queued into
the match you want to read (hop 2 for yourself, then hop 3). That is "build for
the lobby I am sitting in," not "look up anyone."

Two further costs apply even if the probe were to come back 200:

- **Token lifetime.** RallyHere bearer tokens live minutes to about an hour. A
  bot would have to re-mint continuously by replaying the Steam session-ticket
  to OAuth exchange headlessly — an unattended fake client, fragile against any
  client patch.
- **Terms of service.** That headless-client loop is an unauthorized automated
  client. Low stakes for a throwaway account, non-zero for the endpoint, and
  exactly the behaviour that gets an endpoint locked down for everyone.

### Settling it: `scripts/probe_rallyhere.py`

The wall is documented, but the documented wall is worth confirming against a
real token before it is treated as final — Hi-Rez could grant player tokens
more than the spec's baseline. The probe does this on your own account, on your
own machine, with nothing account-bound going near the deployed bot. Supply a
bearer token and env host sniffed from your client (mitmproxy trusting its own
cert, read off any `*.rally-here.io` request) plus a friend's UUID:

    python scripts/probe_rallyhere.py \
        --base-url https://<env-id>.rally-here.io \
        --token "$RH_TOKEN" \
        --self-uuid <your-uuid> \
        --other-uuid <a-friends-uuid>

It decodes the token's own JWT permissions before sending anything — the verdict
is often legible straight off the token — then tests self-read, the gated
cross-player read (hop 2), and presence, and prints which wall it hit. A **403**
on the cross-player call confirms the spec and closes the path for a bot. A
**200** would mean player tokens are more privileged than documented, and the
conversation reopens with the token-lifetime and ToS costs in full view.

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
remote API. Other tracker sites (smite2.live, smitetracker.com) appear to be
downstream of the same slow ingest and expose no faster public feed.

**That claim used to include smitesource.com, and it was wrong.** See below.

## smitesource.com: faster, and it answers the gated hop

Measured 2026-08-31 against a live casual Conquest match. SmiteSource has what
this document concluded no public source had — arbitrary player to the session
they are in right now — and serves the match in progress rather than a roster:

    GET /rpc/matches/getLiveMatch?data={"json":{"playerUuid":"…"}}
    GET /rpc/matches/getMatch?data={"json":{"matchId":"…"}}

`liveUpdatedAt` advanced 17:08:38 → 17:13:05 → 17:17:37 — **267s and 272s
apart**, reaching us within ~10s of each move, against tracker.gg's ~10 minutes.
The match payload carries all ten players with live K/D/A, damage, mitigation,
gold, wards and per-ability damage.

This does **not** reopen the RallyHere analysis above: the permission wall is
still exactly where it was. SmiteSource is simply a third party that already
holds the access we cannot get, and reading their site is a different question
from minting a token.

Three things worth knowing before touching it:

- **No credential and no clearance.** Unlike tracker.gg, the `/rpc` routes
  answer a plain HTTP client — verified from a cold session with no cookies and
  no impersonation. The HTML pages are challenged; the API is not. So this path
  never launches Camoufox and never spends a solve.
- **It still cannot be aiohttp.** aiohttp draws a 403 where `curl`, `curl_cffi`
  and the browser all get 200 — the same fingerprint conclusion `tracker_client`
  reached, so `smitesource.py` uses curl_cffi too.
- **There is no name lookup on that surface.** The site's search box is a
  Next.js server action, not an RPC procedure. Resolution goes platform account
  id → `getPlayerOverlaySession` → uuid, so a display name resolves to nothing
  and falls back to tracker.gg. That is the main gap, and capturing the search
  action is the obvious way to close it.

Shipped in `src/HirezAPI/smite2/smitesource.py`, behind
`SMITELE_LIVE_MATCH_SOURCE=smitesource` (`bot.liveMatchSource` in values).
Default is unchanged, and anything that source cannot answer falls through to
the tracker.gg path below it — including, deliberately, only the *unanswerable*
cases: a fresh "not in a match" is an answer and is not second-guessed against
a ten-minute-old snapshot.

## Bottom line

The precise lobby exists in RallyHere and is fresh to the second, and the one
hop a bot needs — arbitrary player to their live session — is gated behind
`session:read-player:any`, which a player token does not carry. No courier
account or presence read routes around it, and that has not changed.

What changed is that it no longer has to. SmiteSource answers that hop for us
at a ~4.5 minute cadence with the match in progress, so the honest ceiling is
no longer the coarse "is this person running Smite 2" signal — that is now the
fallback's fallback. Surfacing the snapshot age still matters: four minutes is
much better than ten and is still not real time.

[spec]: https://github.com/RallyHereInteractive/openapi-spec-environment
