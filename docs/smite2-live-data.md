# Smite 2 live-match data: what is reachable, and why the lobby lags

`/livematch` for Smite 2 reads tracker.gg, whose live snapshots refresh only
about every ten minutes. This document records the investigation into whether
anything fresher exists, so the question does not have to be re-opened from
scratch. The short answer, **revised 2026-08-11 against a real captured token**:
a player's own token *can* read any player's live session — the permission the
spec reserves for licensed studios is in the token — but it cannot turn a Steam
handle into the RallyHere UUID that read needs. The wall is identity resolution,
not session reading. What shipped so far is still the coarse Steam signal.

Everything below was checked against primary sources — the RallyHere OpenAPI
spec, the Steam Web API docs, and live tracker.gg traffic against a real match —
and the RallyHere section is now measured against the live backend rather than
read off the spec, which it contradicts. Where a claim rests on one observation,
or is not yet tested at all, it says so.

## The four sources, ranked by freshness

| Source | Freshness | Granularity | Verdict |
| --- | --- | --- | --- |
| RallyHere session API | seconds | full lobby | **open** to a player token — but only for a player whose UUID you can already name (see below) |
| RallyHere presence API | seconds | coarse (online/in-game) | reachable, carries no session id, same UUID problem |
| Steam `GetPlayerSummaries` | seconds | coarse (running the game) | **shipped** as the fallback |
| Discord guild presence | seconds | coarse (running the game) | viable, not yet built |
| tracker.gg `/live` | ~10 min | full lobby | what we use for the lobby itself |

## RallyHere: the real source, behind the real wall

Smite 2 runs on the RallyHere backend, and tracker.gg is downstream of it — so
RallyHere is as fresh as the data gets. Its API is publicly documented
([github.com/RallyHereInteractive/openapi-spec-environment][spec]), served
per-environment at `https://<env-id>.rally-here.io`. The `<env-id>` subdomain
for Smite 2 is not published; it is the host the game client's own requests go
to, visible only by sniffing that client's traffic.

The endpoints that would answer "who is in this player's match right now" all
exist. The question is authorization — and everything below is now **measured
against a real captured player token on 2026-08-11**, not read off the spec. The
spec-derived version of this section was wrong, and wrong in both directions:
the hop it called gated is open, and the hop it called open is gated. There are
three hops.

**1. Platform handle to player UUID — GATED. This is the wall.**
`GET /users/v1/platform-user?platform=Steam&platform_user_id=…` answers:

    403  Insufficient Permissions - Expected any of: `user:platform:read`, `user:*`

A player token holds neither. So a Steam id — which is exactly what the bot's
roster is keyed by — cannot be turned into the RallyHere UUID that every other
read wants. `GET /users/v2/player/{player_id}/uuid` *does* answer 200, but it
converts a RallyHere internal player id, which you only have for someone you
could already identify. Both were tried against the token owner's own Steam id,
where the right answer was known in advance.

**2. Player UUID to their session id — OPEN.**
`GET /session/v1/player/{player_uuid}/session`. The spec says this needs
`session:*` or `session:read-player:any` for anyone but yourself, and predicts a
player token carries only `session:read-player:self`. It carries both — the
captured token's 22 session permissions include, verbatim:

    session:read-player:any
    session:read-player:self

So the elevated, "licensed studios only" permission is in fact minted by a
normal Steam player login. Given a UUID, this token can ask what session that
player is in.

**3. Session id to roster — open to any member.**
`GET /session/v1/session/{session_id}/player` accepts `session:read:self`. Not
yet exercised live: no session was in progress during the capture, and
`sessions()` for an idle player comes back empty, so the envelope's shape is
still unread.

### Where that leaves a bot

The wall moved rather than fell. Reading a friend's live lobby is permitted;
*naming* the friend is not. The bot's roster is Discord id → `steam:7656…`, and
there is no route from that to a RallyHere UUID with this token.

What is not yet ruled out, and is the obvious next thing to measure: the token's
own self-scoped reads that carry *other* players' UUIDs as a side effect —
`/match/v1/player/{uuid}/match` (your own match history, whose rows describe
everyone in each match) and `/match/v1/player/{uuid}/recently-played`. If those
return UUIDs alongside display names or platform ids, then playing a single
match with someone is enough to learn their UUID permanently, and a small
directory built from your own history would bridge hop 1 for exactly the people
you actually play with — which is the whole population this bot cares about.
Untested as of writing.

### Presence does not bridge it

If `GET /presence/v1/player/uuid/{uuid}/presence` carried a session id, presence
would hand you what hop 2 needs. It does not. Read live, the whole document is:

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

Now redundant rather than insufficient. It was proposed to keep anything
account-bound out of the bot while still clearing hop 2 — but hop 2 needed no
clearing, and a courier account hits the identical hop 1 wall, since it also
lacks `user:platform:read`. A courier only ever helped for reading its *own*
lobby, which is "build for the lobby I am sitting in", not "look up anyone".

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

The precise lobby exists in RallyHere, is fresh to the second, and a player
token is allowed to read it — `session:read-player:any` is in the token, which
the spec said it would not be. The remaining gap is one hop earlier: nothing a
player token can call turns a Steam id into a RallyHere UUID
(`user:platform:read`, 403). So the path is open for anyone whose UUID is
already known and closed for everyone else, and whether the first group can be
grown from your own match history is the open question worth measuring next.

Until it is, the shipped ceiling stands: the coarse "is this person running
Smite 2" signal via Steam, plus surfacing tracker.gg's snapshot age so a
ten-minute-old lobby does not read as real time. What is newly *cheap*, and
needs no UUID lookup at all, is the token owner's own status — presence, state
and session for you, in seconds.

[spec]: https://github.com/RallyHereInteractive/openapi-spec-environment
