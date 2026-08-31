"""The lobby, mid-match, from smitesource.com.

`docs/smite2-live-data.md` concluded that the one hop a lobby lookup needs —
an arbitrary player to the session they are in right now — was gated behind a
RallyHere permission no player token carries, and that every tracker site was
downstream of the same slow ingest. The first half still holds. The second was
wrong about this site: SmiteSource answers that hop directly, and answers it
with the match in progress rather than a roster.

Measured against a live match on 2026-08-31, not inferred:

    tracker.gg  ~10 min snapshot cadence, roster only
    SmiteSource  ~4.5 min refresh (267s and 272s between consecutive
                 `liveUpdatedAt` moves), served within ~10s of the refresh,
                 carrying every player's live K/D/A, damage and gold

So this is a second backend for `/livematch`, chosen by `SMITELE_LIVE_MATCH_SOURCE`
and falling back to tracker.gg whenever it cannot answer. It is deliberately not
the default: it is one site's undocumented API, and the fallback is what makes
turning it on cheap to reverse.

## The API

`GET /rpc/<router>/<procedure>?data={"json":{…}}`, answering `{"json": …}`.
No account, no token, no Cloudflare clearance — which is the whole reason this
module is a hundred lines and `tracker_client` is seven hundred.

**It still cannot be aiohttp, and that is measured.** The HTML pages sit behind
a Cloudflare managed challenge; the `/rpc` routes do not. But the same
fingerprinting applies to both, and on 2026-08-31 the RPC surface answered
plain `curl`, `curl_cffi` with and without impersonation, and refused `aiohttp`
outright:

    curl_cffi impersonate firefox  200      aiohttp  403 (challenge interstitial)
    curl_cffi no impersonation     200
    curl (system)                  200

Which is the same conclusion `tracker_client` reached by a different route, so
this uses curl_cffi too. It does *not* need the clearance machinery, and no
cookie is carried: there is nothing bound to an address here, so unlike
tracker.gg there is no identity to keep two halves of in agreement.

**No proxy, on purpose.** The egress discipline in `egress.py` exists because a
clearance cookie is bound to the address that solved for it. Nothing here is,
so routing these through a metered proxy would buy nothing and spend bandwidth
on a per-command probe — the same reasoning that leaves `steam.py` direct.

**No rate limiter, on purpose.** tracker.gg's pacing machinery answers to a
measured 300-requests-an-hour quota. Nothing comparable turned up here across
~150 requests, and the honest bound on what this module can spend is the
caller's own cache: `PlayerLookups` holds a lobby for 60s, so a channel full of
people asking about one player costs three requests a minute, not three a
command. Adding a limiter would be machinery with nothing to answer to.

Cloudflare caches two of the three at the edge — `getLiveMatch` for 240s,
`getPlayerOverlaySession` for 55s, `getMatch` not at all (`no-store`) — so a
popular player's lobby is largely served without touching the origin. The 240s
on the live route sits just under the site's own ~4.5 min refresh, so it costs
nothing in freshness.

**No connection pooling, and that was measured rather than assumed.** A session
per lookup looked like the obvious thing to fix: three requests, three
handshakes, once a command. It is not worth fixing. A fresh session's first
request costs ~45ms against ~25ms on a warm one, so pooling across lookups
would save something like 20ms on a path already bounded at four seconds, in
exchange for a process-lifetime session whose lifecycle has to survive the
event loop. The tail this module actually has — a 3s origin miss — is not a
connection cost and pooling does not touch it.

## Two details that are load-bearing

**The uuid is `smitePlayer.person.publicUuid`, not `hirezPlayerUuid`.** Both are
uuids, both sit on the same match row, and only the first is what `/player/<id>`
and every `playerUuid` argument mean. Matching on the other one finds nobody,
and finding nobody is indistinguishable from "not in this lobby" — which would
silently cost the player their own side of the match.

**`getPlayerOverlaySession` is used to resolve an identity and for nothing
else.** It carries a `liveMatch` of its own, and that field is scoped to the
session's queue: asked about a player who was at that moment nine minutes into
a *casual* Conquest match, it answered `liveMatch: null` while `getLiveMatch`
answered with the match. Trusting it would have made this backend blind to
every queue but the one the overlay happened to be tracking.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as curl_requests

HOST = "https://smitesource.com"

# What a lobby from here calls itself, so a footer can name its own source
# rather than crediting tracker.gg for someone else's data.
SOURCE_NAME = "SmiteSource"

# Same reasoning as tracker_client's: the impersonation target is our whole
# network identity, decided in one place.
IMPERSONATE = "firefox"

# Sized against `live_lobby.LOOKUP_TIMEOUT_SECONDS`, which gives a build's
# whole matchup lookup four seconds before it gives up and posts without one.
#
# Measured 2026-08-31, warm connection: 26ms, 33ms and 27ms median for the
# three procedures, so the usual chain is under a tenth of a second and a fresh
# session's first request costs ~45ms on top. What this actually bounds is the
# tail — one `getPlayerOverlaySession` for an uncached player took 3.07s while
# the origin computed it. Four seconds is comfortably past that and still short
# enough that `/livematch` answers rather than hangs; `/build` is bounded by its
# own wrapper regardless, and loses a matchup rather than a response.
TIMEOUT_SECONDS = 4

# tracker.gg's platform slugs are not this site's. `psn` and `xbl` are the two
# that differ; `nintendo` has no tracker.gg equivalent and is here because the
# endpoint accepts it.
PLATFORM_SLUGS = {
    "steam": "steam",
    "epic": "epic",
    "psn": "playstation",
    "xbl": "xbox",
    "xbox": "xbox",
    "playstation": "playstation",
    "nintendo": "nintendo",
}

# Team 1 is Order and team 2 is Chaos — read off a rendered match page against
# the same match's JSON rather than assumed, since guessing wrong swaps every
# ally for an enemy without erroring.
TEAMS = {1: "order", 2: "chaos"}

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class SmiteSourceUnavailable(RuntimeError):
    """We could not get an answer, so the caller should ask tracker.gg.

    Deliberately distinct from a `None` lobby. "This player is not in a match"
    is an answer, and re-asking tracker.gg for it would spend a request to
    replace a fresh no with a ten-minute-old one. "We could not tell" — a
    handle that is not a platform id, a timeout, a 500 — is not an answer, and
    is the only thing that should fall through.
    """


def looks_like_uuid(value: str) -> bool:
    return bool(_UUID.match((value or "").strip()))


def _pretty_mode(queue_type: str) -> str:
    """`casual_conquest` as `Casual Conquest`."""
    return " ".join(part.capitalize() for part in (queue_type or "").split("_") if part)


def _epoch(stamp: Optional[str]) -> float:
    """An ISO instant as epoch seconds, or 0 when there isn't one.

    Parsed as the UTC it declares itself to be. The tracker.gg path drops the
    zone and lets a naive value be read as local time, which is wrong by the
    host's offset; there is no reason to copy that here.
    """
    if not stamp:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class SmiteSourceClient:
    """The three procedures a lobby lookup needs, and nothing else."""

    def __init__(self):
        self.__session: Optional[curl_requests.AsyncSession] = None

    async def __aenter__(self) -> "SmiteSourceClient":
        # `trust_env` off for the same reason as tracker_client: curl_cffi
        # reads HTTPS_PROXY on its own, and this module's egress is a decision
        # made here rather than inherited from whatever the pod happens to set.
        self.__session = curl_requests.AsyncSession(
            impersonate=IMPERSONATE, trust_env=False
        )
        await self.__session.__aenter__()
        return self

    async def __aexit__(self, *args) -> None:
        if self.__session is not None:
            await self.__session.__aexit__(*args)
            self.__session = None

    async def rpc(self, procedure: str, payload: Dict[str, Any]) -> Any:
        """One `matches.getX`-style call, unwrapped from its envelope.

        Every failure raises `SmiteSourceUnavailable`, because every failure
        means the same thing to the only caller there is: ask tracker.gg.
        """
        if self.__session is None:
            raise RuntimeError("SmiteSourceClient used outside its context manager")

        path = procedure.replace(".", "/")
        params = {"data": json.dumps({"json": payload}, separators=(",", ":"))}
        try:
            response = await self.__session.get(
                f"{HOST}/rpc/{path}", params=params, timeout=TIMEOUT_SECONDS
            )
        except Exception as error:  # noqa: BLE001 — every transport failure is one case
            raise SmiteSourceUnavailable(f"{procedure}: {error}") from error

        if response.status_code != 200:
            raise SmiteSourceUnavailable(f"{procedure}: HTTP {response.status_code}")
        try:
            body = json.loads(response.content)
        except ValueError as error:
            raise SmiteSourceUnavailable(f"{procedure}: unparseable body") from error
        if not isinstance(body, dict) or "json" not in body:
            raise SmiteSourceUnavailable(f"{procedure}: unexpected envelope")
        return body["json"]

    async def player_uuid(self, platform: str, handle: str) -> Optional[str]:
        """A platform account id to the uuid the rest of the API is keyed by.

        `handle` has to be the *platform* id — a Steam 64, not a display name.
        This site's search box is a Next.js server action rather than an RPC
        procedure, so there is no name lookup on this surface to fall back to;
        a display name resolves to nothing and the caller goes to tracker.gg,
        which is exactly what should happen.
        """
        slug = PLATFORM_SLUGS.get((platform or "").lower())
        if not slug or not handle:
            return None
        body = await self.rpc(
            "matches.getPlayerOverlaySession", {"platform": slug, "platformId": handle}
        )
        if not isinstance(body, dict):
            return None
        uuid = body.get("playerUuid")
        return str(uuid) if uuid else None

    async def live_match_id(self, player_uuid: str) -> Optional[str]:
        """The match this player is in right now, by id, or None."""
        body = await self.rpc("matches.getLiveMatch", {"playerUuid": player_uuid})
        if not isinstance(body, dict) or body.get("isComplete"):
            return None
        match_id = body.get("hirezMatchId")
        return str(match_id) if match_id else None

    async def match(self, match_id: str) -> Dict[str, Any]:
        body = await self.rpc("matches.getMatch", {"matchId": match_id})
        if not isinstance(body, dict):
            raise SmiteSourceUnavailable("getMatch: not an object")
        return body


def _players(match: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    """Every row as `(uuid, god, team, handle)`, skipping what isn't a player."""
    out: List[Tuple[str, str, str, str]] = []
    for row in match.get("players") or []:
        if not isinstance(row, dict):
            continue
        smite_player = row.get("smitePlayer") or {}
        person = smite_player.get("person") or {}
        god = (row.get("godMaster") or {}).get("canonicalName") or ""
        if not god:
            # `godRawName` is `Gods.Anubis`; the tail is the name. Only reached
            # when the joined god row is missing, which a mid-match ingest can
            # briefly leave undone.
            god = str(row.get("godRawName") or "").rpartition(".")[2]
        if not god:
            continue
        out.append(
            (
                str(person.get("publicUuid") or ""),
                god,
                TEAMS.get(row.get("teamId"), ""),
                str(smite_player.get("displayName") or ""),
            )
        )
    return out


async def live_match(platform: str, handle: str):
    """The lobby this player is in, in the shape the bot already renders.

    Returns None when the player is demonstrably not in a match, and raises
    `SmiteSourceUnavailable` when this source could not tell — see that class
    for why the two must not collapse into one. Nothing here logs: the reason
    is carried on the exception, and the one caller is better placed to say
    what it did about it.
    """
    # Imported here rather than at module scope because `players` reaches the
    # other way for the toggle, and the repo's habit for that is a local import
    # at the point of use.
    from smite2.players import LiveMatch, LivePlayer  # noqa: PLC0415

    async with SmiteSourceClient() as client:
        if looks_like_uuid(handle):
            uuid = handle.strip()
        else:
            uuid = await client.player_uuid(platform, handle)
        if not uuid:
            raise SmiteSourceUnavailable(f"no player for {platform}:{handle}")

        match_id = await client.live_match_id(uuid)
        if match_id is None:
            return None
        match = await client.match(match_id)

    if match.get("isComplete"):
        # It ended between the two calls. Not in a match is the true answer.
        return None

    rows = _players(match)
    mine = next((row for row in rows if row[0] and row[0] == uuid), None)
    if mine is None or not rows:
        # Without our own row there is no side, and a lobby with no sides is
        # worse than no lobby — it would render ten strangers as enemies.
        raise SmiteSourceUnavailable(f"{uuid} not among the rows of {match_id}")

    queue_type = str(match.get("queueType") or "")
    return LiveMatch(
        match_id=str(match_id),
        mode=queue_type,
        mode_name=_pretty_mode(queue_type),
        ranked=(
            queue_type.startswith("ranked")
            or str(match.get("lobbyType") or "").lower() == "ranked"
        ),
        own_god=mine[1],
        own_team=mine[2],
        players=[
            LivePlayer(god=god, team=team, handle=handle_)
            for _, god, team, handle_ in rows
        ],
        # What the site itself says about how stale this is, rather than when we
        # asked. Its live poll lands roughly every four and a half minutes.
        snapshot_at=_epoch(match.get("liveUpdatedAt")),
        source=SOURCE_NAME,
    )


# --- the toggle ------------------------------------------------------------
#
# Backend-only: there is no Discord option for this and there should not be.
# Which site answers `/livematch` is an operational choice about a third party's
# undocumented API, not something to ask a player in a slash command.

ENV_VAR = "SMITELE_LIVE_MATCH_SOURCE"
TRACKER = "tracker"
SMITESOURCE = "smitesource"


def selected_source() -> str:
    """Which backend `/livematch` should try first.

    Read per call rather than captured at import: it costs a dict lookup, and
    it means a rollout can be reversed by editing the Deployment rather than
    by rebuilding anything. Anything unrecognised is tracker.gg, so a typo
    degrades to today's behaviour instead of to no behaviour.
    """
    return (
        SMITESOURCE
        if (os.environ.get(ENV_VAR) or "").strip().lower() == SMITESOURCE
        else TRACKER
    )
