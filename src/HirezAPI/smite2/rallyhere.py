"""Read Smite 2's own RallyHere backend — the source tracker.gg is downstream of.

Smite 2 runs on RallyHere, whose per-environment API is fronted for Smite 2 at
``api-smite2.titanforgegames.com`` (a CNAME onto the RallyHere env
``91d5c0da-…-.rally-here.io``). That backend is as fresh as the data gets —
inventory, matches, ranks, live sessions, presence — where tracker.gg lags it by
~10 minutes. This module reads it with a *player* token, for a player's own and
their friends' data, to feed build generation.

Auth: capture once, refresh forever
------------------------------------
A player access token lives ~6 hours. You get one the first time by capturing it
off your own running client (``scripts/win/`` does this) into a state JSON. From
then on this module renews it **without the game** using RallyHere's V1 login
``grant_type=refresh`` — ``POST /users/v1/login`` with the refresh token in
``portal_access_token`` and the client's ``Authorization: Basic`` credential.
Verified live 2026-08-11: it answers 200 with a fresh 6-hour access token, and
returns the **same refresh token** rather than a rotated one. So one capture
sustains indefinitely until that refresh token is revoked or expires, at which
point a fresh capture is needed and ``RallyHereAuthError`` says so.

The renewed access token is still written back to the state file, for two
reasons that survive the refresh token being stable: a restart then costs no
renewal at all, and if RallyHere ever does start rotating, the rotated one is
already being persisted rather than silently dropped.

In the cluster the capture arrives as a Secret (``credentials.rallyHere`` in
``values.local.yaml``) and the state file lives on the bot's data volume,
because a Secret is read-only and this state is written. See
:meth:`RallyHereAuth.load` for which of the two wins when they disagree.

Egress: reuses `egress`
-----------------------
Every request (refresh and reads) goes through ``egress.proxy_url()`` — the same
``SMITELE_EGRESS_PROXY`` abstraction the tracker.gg crawl uses — so this can be
pointed at the VPS lane and kept off the home address.

Scope, deliberately
-------------------
This is a personal tool for *your own and consenting friends'* data. The token
happens to carry broad permissions (``session:read-player:any`` and the whole
``session:*`` write set), but this module exposes only reads and is meant to be
pointed at yourself and your friends. Do not publish the ``Basic`` credential in
the state file (it is the application's embedded secret), and do not turn this
into an unattended reader of players who are not you and yours — that is the
unauthorized automated client Titan Forge's ToS forbids, and the behaviour that
gets the endpoint locked down for everyone.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import aiohttp

import paths

from . import egress

DEFAULT_BASE_URL = "https://api-smite2.titanforgegames.com"

# Where the token/refresh state lives. It is a *runtime* store, not a static
# secret — the refresh token rotates on every renewal and is written back — so
# it is a path, defaulting next to the bot but overridable per deployment.
STATE_ENV = "SMITELE_RH_STATE"
FILE_NAME = "rh_state.json"

# The capture as a *deployment* supplies it. These exist because a Kubernetes
# Secret is mounted read-only while this state is written on every renewal, so
# the Secret can only ever be a seed — it writes the state file on the data
# volume once, and the file is the living copy from then on.
TOKEN_ENV = "SMITELE_RH_TOKEN"
REFRESH_ENV = "SMITELE_RH_REFRESH_TOKEN"
CLIENT_BASIC_ENV = "SMITELE_RH_CLIENT_BASIC"
BASE_URL_ENV = "SMITELE_RH_BASE_URL"

# Renew this many seconds before the access token's own expiry, so a request
# never races an expiring token. RallyHere access tokens run ~6h; five minutes
# of skew is generous and costs one extra refresh a day at most.
_REFRESH_SKEW_SECONDS = 300

_LOGIN_PATH = "/users/v1/login"

# How many players' reads are in flight at once when fanning out over a friends
# list. This is one player's own token asking about their own friends, so the
# volume is small either way; the cap is here so a twenty-friend list arrives as
# a trickle rather than forty simultaneous requests from one bearer, which is
# what an unattended client looks like from the server's side.
_MAX_CONCURRENT_READS = 4

# Presence strings that mean "not reachable". Everything else — `online`,
# `in_game`, whatever a title defines — counts as present. Written as a
# deny-list on purpose: a status this module has never seen should read as
# online rather than silently disappear a friend who is playing.
_OFFLINE_STATUSES = ("offline", "invisible", "unknown", "")

# Session *types* are per-title configuration in RallyHere, not a fixed enum, so
# these are names to treat as "sitting in a group, not in a game" rather than
# anything the spec guarantees. Anything unrecognised counts as a match, because
# the failure that matters is calling a live match a party and reporting nobody
# is playing. Revisit against a real capture of `sessions()`.
_PARTY_SESSION_TYPES = ("party", "lobby", "social", "hub")


class RallyHereError(RuntimeError):
    """Base for anything this module raises."""


class RallyHereAuthError(RallyHereError):
    """The token could not be refreshed — capture a fresh one from the game.

    Raised when there is no refresh token or client credential to refresh with,
    or when RallyHere rejects the refresh (the refresh token expired or was
    revoked). It is deliberately distinct from a read failure so a caller can
    tell "re-capture needed" apart from "that player has no matches".
    """


class RallyHereHTTPError(RallyHereError):
    """A read returned a non-2xx status."""

    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"HTTP {status} for {path}: {body}")
        self.status = status
        self.path = path


def _decode_jwt_claims(token: str) -> Dict[str, Any]:
    """The JWT's middle segment as a dict, or {} — read, never trusted."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return {}


def _token_expiry(token: str) -> int:
    """The token's `exp` (unix seconds), or 0 if it can't be read."""
    exp = _decode_jwt_claims(token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


def _seed_from_env() -> Optional[Dict[str, Any]]:
    """The capture a deployment passes in, or None if it passes none."""
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    refresh = (os.environ.get(REFRESH_ENV) or "").strip()
    if not token and not refresh:
        return None
    seed: Dict[str, Any] = {
        "token": token,
        "refresh_token": refresh or None,
        # Explicit, so re-seeding cannot inherit the *previous* token's expiry
        # and believe a fresh capture is already stale.
        "exp": _token_expiry(token),
    }
    basic = (os.environ.get(CLIENT_BASIC_ENV) or "").strip()
    if basic:
        seed["client_basic"] = basic
    base_url = (os.environ.get(BASE_URL_ENV) or "").strip()
    if base_url:
        seed["base_url"] = base_url
    return seed


def _seed_fingerprint(seed: Dict[str, Any]) -> str:
    """A digest identifying which capture the state file was seeded from.

    A digest and never the token: this is written to the state file purely to
    answer "has the operator supplied a *new* capture since we last seeded",
    and there is no reason for a second copy of the credential to exist.
    """
    material = f"{seed.get('token') or ''}|{seed.get('refresh_token') or ''}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _proxy_and_auth() -> Tuple[Optional[str], Optional[aiohttp.BasicAuth]]:
    """The egress proxy split into (url-without-creds, auth) for aiohttp.

    `egress.proxy_url()` may carry inline credentials; aiohttp wants the proxy
    URL clean and the auth passed separately. None/None means direct.
    """
    url = egress.proxy_url()
    if not url:
        return None, None
    parts = urlsplit(url)
    auth: Optional[aiohttp.BasicAuth] = None
    if parts.username:
        auth = aiohttp.BasicAuth(parts.username, parts.password or "")
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url, auth


@dataclass
class TokenState:
    """The living token, plus what's needed to renew it.

    `client_basic` is the client's ``client_id:client_secret`` (the app's
    embedded credential, captured from the login request); it and the refresh
    token are the two things a headless refresh needs.
    """

    base_url: str
    access_token: str
    refresh_token: Optional[str]
    client_basic: Optional[str]
    exp: int

    @classmethod
    def from_capture(cls, data: Dict[str, Any]) -> "TokenState":
        """Build from a ``rh_capture.json`` (or persisted-state) dict.

        Accepts either a flat ``client_basic`` or the ``auth_requests`` shape the
        capture addon writes, from which the token endpoint's decoded Basic
        credential is lifted.
        """
        access = data.get("token") or data.get("access_token") or ""
        basic = data.get("client_basic")
        if not basic:
            for req in data.get("auth_requests", []) or []:
                if req.get("client_basic_decoded"):
                    basic = req["client_basic_decoded"]
                    break
        exp = _token_expiry(access) or int(data.get("exp") or 0)
        return cls(
            base_url=data.get("base_url") or DEFAULT_BASE_URL,
            access_token=access,
            refresh_token=data.get("refresh_token"),
            client_basic=basic,
            exp=exp,
        )


class RallyHereAuth:
    """Holds a token and keeps it fresh, persisting the rotated refresh token.

    Build it from a capture file with :meth:`load`. Ask it for a valid bearer
    with :meth:`bearer`, which refreshes transparently when the current token is
    within the skew window of expiry.
    """

    def __init__(self, state: TokenState, path: Optional[str] = None) -> None:
        self.state = state
        self._path = path
        self._raw: Dict[str, Any] = {}
        self._lock: Optional[asyncio.Lock] = None

    @classmethod
    def load(cls, path: Optional[str] = None) -> "RallyHereAuth":
        """The living state, seeded from the environment the first time.

        Two places hold a credential and only one of them can be right, so the
        precedence is the whole of this method:

        * The **state file** wins normally. It holds the access token this
          process last renewed, which is newer than the one baked into the
          deployment and saves a renewal on every restart.
        * The **environment** wins when it carries a capture the state file was
          never seeded from. That is either the first start, or you re-captured
          from the game precisely *because* the stored one stopped working;
          preferring the file there would ignore the fix.

        Which of the two it is comes from a digest of the seed recorded beside
        the state. Nothing in either place means no session at all, and that
        raises rather than half-starting.
        """
        path = path or os.environ.get(STATE_ENV) or os.path.join(paths.DATA_DIR, FILE_NAME)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, ValueError):
            raw = {}

        seed = _seed_from_env()
        fingerprint = _seed_fingerprint(seed) if seed else ""
        reseeding = bool(seed) and fingerprint != raw.get("seed_fingerprint")
        if reseeding:
            raw = {**raw, **seed, "seed_fingerprint": fingerprint}
        elif not raw:
            raise RallyHereAuthError(
                f"no RallyHere session: no state at {path}, and no ${TOKEN_ENV}/"
                f"${REFRESH_ENV} to seed one from — capture a token from the "
                "game with scripts/win/"
            )

        auth = cls(TokenState.from_capture(raw), path=path)
        auth._raw = raw
        if reseeding:
            # Written now rather than at the first renewal, so the fingerprint
            # that marks this capture as seeded exists even if the process dies
            # before it ever refreshes.
            auth._persist()
        return auth

    @property
    def self_uuid(self) -> Optional[str]:
        """The token owner's RallyHere player uuid."""
        claims = _decode_jwt_claims(self.state.access_token)
        return claims.get("active_player_uuid") or claims.get("player_uuid")

    def _persist(self) -> None:
        """Write the current token + rotated refresh token back to the file."""
        if not self._path:
            return
        self._raw.update(
            {
                "base_url": self.state.base_url,
                "token": self.state.access_token,
                "refresh_token": self.state.refresh_token,
                "exp": self.state.exp,
            }
        )
        tmp = f"{self._path}.tmp"
        # Owner-only: this file is the whole session. The refresh token in it
        # mints access tokens for as long as it lives — indefinitely, as far as
        # measurement goes, since renewal hands the same one back — and the
        # capture it came from carries the client's embedded credential beside
        # it.
        handle_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(self._raw, handle, indent=2, sort_keys=True)
        os.replace(tmp, self._path)

    def _refresh_lock(self) -> asyncio.Lock:
        """The renewal lock, made on first use.

        Not built in ``__init__`` because :meth:`load` runs at configuration
        time, which need not be inside a running event loop.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _fresh_enough(self) -> bool:
        return time.time() < self.state.exp - _REFRESH_SKEW_SECONDS

    async def bearer(
        self, session: aiohttp.ClientSession, retire: Optional[str] = None
    ) -> str:
        """A valid access token, refreshing first if it's near expiry.

        ``retire`` names a token the caller just had rejected, and asks for a
        renewal even though ``exp`` says there was time left — a token can be
        revoked server-side, or our clock can be behind.
        """
        if retire is None and self._fresh_enough():
            return self.state.access_token
        async with self._refresh_lock():
            # Re-check under the lock. Fanning reads out over a group of players
            # puts many coroutines at this line in the same instant, and without
            # this each would mint its own token: a burst of identical logins
            # from one bearer, which is both waste and the shape of a client
            # nobody wants to look like. Whoever arrives second finds the token
            # the winner installed and uses that instead.
            if retire is not None:
                if self.state.access_token != retire:
                    return self.state.access_token
            elif self._fresh_enough():
                return self.state.access_token
            await self._refresh(session)
        return self.state.access_token

    async def _refresh(self, session: aiohttp.ClientSession) -> None:
        if not self.state.refresh_token or not self.state.client_basic:
            raise RallyHereAuthError(
                "no refresh token or client credential in state — capture a "
                "fresh token from the game (scripts/win/)"
            )
        basic = base64.b64encode(self.state.client_basic.encode()).decode()
        proxy, proxy_auth = _proxy_and_auth()
        try:
            async with session.post(
                self.state.base_url + _LOGIN_PATH,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "grant_type": "refresh",
                    "portal_access_token": self.state.refresh_token,
                    "include_refresh": True,
                },
                proxy=proxy,
                proxy_auth=proxy_auth,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise RallyHereAuthError(
                        f"refresh rejected (HTTP {response.status}): {text[:200]} — "
                        "the refresh token is likely expired/revoked; re-capture"
                    )
                data = json.loads(text)
        except aiohttp.ClientError as error:
            raise RallyHereAuthError(f"refresh request failed: {error}") from error

        access = data.get("access_token")
        if not access:
            raise RallyHereAuthError(f"refresh returned no access_token: {data}")
        self.state.access_token = access
        # Measured 2026-08-11: `include_refresh` returns the *same* refresh
        # token, so this is normally a no-op. Written as a rotation anyway,
        # because the day it starts rotating, dropping the new one would end
        # the session at the next renewal and look like an expiry.
        self.state.refresh_token = data.get("refresh_token") or self.state.refresh_token
        self.state.exp = _token_expiry(access) or int(
            time.time() + int(data.get("expires_in") or 21600)
        )
        self._persist()


@dataclass
class SessionRef:
    """One session a player is in, as much of it as the API names."""

    session_id: str
    session_type: str = ""

    @property
    def is_party(self) -> bool:
        """Whether this looks like a group rather than a game.

        A heuristic over `_PARTY_SESSION_TYPES`, not a guarantee — session types
        are per-title configuration. Deliberately biased: an unfamiliar type
        reads as a match, so a real game is never reported as "just sitting in a
        party".
        """
        kind = (self.session_type or "").lower()
        return any(name in kind for name in _PARTY_SESSION_TYPES)


@dataclass
class PlayerStatus:
    """What can honestly be said about one player right now.

    `status` is RallyHere's own presence string, kept verbatim so a caller can
    show what the backend actually said rather than this module's reading of it.
    `error` is set when the reads failed for this player alone — a friend whose
    presence 403s should not take the rest of the list down with them.
    """

    uuid: str
    status: str = ""
    online: bool = False
    display_name: Optional[str] = None
    platform: Optional[str] = None
    message: Optional[str] = None
    # The client's own state — "InLobby" and the like. Read `online` first: the
    # message is whatever was last published and outlives the session that set
    # it, so a state beside `online=False` describes where they were, not where
    # they are.
    state: Optional[str] = None
    last_seen: Optional[str] = None
    sessions: List[SessionRef] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def in_match(self) -> bool:
        """In a session that isn't a party — the closest thing to "playing"."""
        return any(not ref.is_party for ref in self.sessions)

    @property
    def match_session_id(self) -> Optional[str]:
        """The session id to hand :meth:`RallyHereClient.session_players`."""
        for ref in self.sessions:
            if not ref.is_party:
                return ref.session_id
        return None


def _presence_fields(body: Any) -> Dict[str, Any]:
    """The PlayerPresence fields, from whatever wrapper they arrive in.

    The read has been seen returning the presence object directly, and the spec
    also describes envelope shapes; rather than bet on one, take the first dict
    in the body that carries a `status`.
    """
    if isinstance(body, dict):
        if "status" in body:
            return body
        for value in body.values():
            found = _presence_fields(value)
            if found:
                return found
    elif isinstance(body, list):
        for item in body:
            found = _presence_fields(item)
            if found:
                return found
    return {}


def _client_state(message: Any) -> Optional[str]:
    """The client's own state string, out of the free-text presence message.

    Measured 2026-08-11: Smite 2 publishes a small JSON document there —
    ``{"state": "InLobby"}`` — which is finer-grained than the `status` field
    beside it and is the nearest the backend comes to saying what a player is
    doing. Anything that is not that document is left alone; this is a free-text
    field and only this client's convention makes it more.
    """
    if not isinstance(message, str) or not message.strip().startswith("{"):
        return None
    try:
        document = json.loads(message)
    except ValueError:
        return None
    state = document.get("state") if isinstance(document, dict) else None
    return str(state) if isinstance(state, str) and state else None


def _session_refs(body: Any) -> List[SessionRef]:
    """Every session id in a sessions payload, however it is nested.

    Walks rather than indexing a known key for the same reason the probe walks
    the JWT: the envelope has moved across RallyHere versions, and the ids are
    unmistakable wherever they sit.
    """
    found: List[SessionRef] = []
    seen: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            session_id = node.get("session_id") or node.get("id")
            if isinstance(session_id, str) and session_id not in seen:
                seen.add(session_id)
                kind = node.get("session_type") or node.get("type") or ""
                found.append(SessionRef(session_id, str(kind)))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return found


class RallyHereClient:
    """Read player data from the Smite 2 RallyHere backend.

    Owns an aiohttp session; use as an async context manager. Every read is
    authenticated with a fresh bearer from the :class:`RallyHereAuth` and routed
    through the configured egress proxy.
    """

    def __init__(self, auth: RallyHereAuth) -> None:
        self.auth = auth
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "RallyHereClient":
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _send(
        self, path: str, token: str, params: Optional[Dict[str, Any]]
    ) -> Tuple[int, str]:
        assert self._session is not None, "use RallyHereClient as an async context manager"
        proxy, proxy_auth = _proxy_and_auth()
        async with self._session.get(
            self.auth.state.base_url + path,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            proxy=proxy,
            proxy_auth=proxy_auth,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            return response.status, await response.text()

    async def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        assert self._session is not None, "use RallyHereClient as an async context manager"
        token = await self.auth.bearer(self._session)
        status, text = await self._send(path, token, params)
        if status == 401:
            # Rejected before its own `exp` said it would be: revoked, or our
            # clock was behind. One forced renewal tells that apart from a dead
            # session — if the refresh also fails it raises RallyHereAuthError,
            # which is the caller's signal to re-capture from the game.
            token = await self.auth.bearer(self._session, retire=token)
            status, text = await self._send(path, token, params)
        if status >= 400:
            raise RallyHereHTTPError(status, path, text[:300])
        return json.loads(text) if text else {}

    # --- reads. `uuid` defaults to the token owner where the API has a `/me`. --

    def _who(self, uuid: Optional[str]) -> str:
        who = uuid or self.auth.self_uuid
        if not who:
            raise RallyHereError("no player uuid given and none in the token")
        return who

    async def inventory(self, uuid: Optional[str] = None) -> Any:
        """Owned items / unlocks — the build inputs."""
        return await self._get(f"/inventory/v2/player/{self._who(uuid)}/inventory")

    async def matches(
        self, uuid: Optional[str] = None, page_size: Optional[int] = None
    ) -> Any:
        """Match history."""
        params = {"page_size": page_size} if page_size else None
        return await self._get(f"/match/v1/player/{self._who(uuid)}/match", params)

    async def stats(self, uuid: Optional[str] = None) -> Any:
        """Aggregate player stats."""
        return await self._get(f"/match/v1/player/{self._who(uuid)}/stats")

    async def recently_played_with(self, uuid: Optional[str] = None) -> Any:
        """Other players recently in matches with this one."""
        return await self._get(f"/match/v1/player/{self._who(uuid)}/recently-played")

    async def ranks(self, uuid: Optional[str] = None) -> Any:
        """All ranked standings."""
        return await self._get(f"/rank/v2/player/{self._who(uuid)}/rank")

    async def friends(self, uuid: Optional[str] = None) -> Any:
        """The friends list (raw)."""
        return await self._get(f"/friends/v2/player/{self._who(uuid)}/friend")

    async def friend_uuids(self, uuid: Optional[str] = None) -> List[str]:
        """Just the friends' player uuids, for fanning reads out over them."""
        body = await self.friends(uuid)
        found: List[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("other_player_uuid", "player_uuid") and isinstance(value, str):
                        found.append(value)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(body)
        me = self.auth.self_uuid
        return [u for u in dict.fromkeys(found) if u != me]

    async def sessions(self, uuid: Optional[str] = None) -> Any:
        """The player's live sessions (party / match), fresh to the second."""
        return await self._get(f"/session/v1/player/{self._who(uuid)}/session")

    async def session_players(self, session_id: str) -> Any:
        """The roster of a session id."""
        return await self._get(f"/session/v1/session/{session_id}/player")

    async def presence(self, uuid: Optional[str] = None) -> Any:
        """Coarse online / in-game / lobby state."""
        return await self._get(f"/presence/v1/player/uuid/{self._who(uuid)}/presence")

    # --- the status read the bot actually wants -----------------------------

    async def status(
        self, uuid: Optional[str] = None, with_sessions: bool = True
    ) -> PlayerStatus:
        """One player's presence, plus the sessions they're in.

        Two requests per player, because presence alone answers "online" and
        never "in a match" — the `PlayerPresence` schema carries no session id,
        which is the whole reason the sessions read is separate. Pass
        `with_sessions=False` for the cheap online/offline answer.

        A per-player HTTP failure lands in `error` rather than raising: one
        friend the token cannot see should not cost the rest of the list. An
        auth failure still raises, because that one is about *us*, not them.
        """
        who = self._who(uuid)
        try:
            fields = _presence_fields(await self.presence(who))
        except RallyHereHTTPError as error:
            return PlayerStatus(uuid=who, error=str(error))

        state = str(fields.get("status") or "").lower()
        status = PlayerStatus(
            uuid=who,
            status=str(fields.get("status") or ""),
            online=state not in _OFFLINE_STATUSES,
            display_name=fields.get("display_name"),
            platform=fields.get("platform"),
            message=fields.get("message"),
            state=_client_state(fields.get("message")),
            last_seen=fields.get("last_seen"),
        )
        if with_sessions and status.online:
            # Only when they're online: an offline player has no session, and
            # the request would spend a round trip to learn nothing.
            try:
                status.sessions = _session_refs(await self.sessions(who))
            except RallyHereHTTPError as error:
                status.error = str(error)
        return status

    async def statuses(
        self, uuids: Iterable[str], with_sessions: bool = True
    ) -> List[PlayerStatus]:
        """:meth:`status` over several players, a few at a time, order kept."""
        limit = asyncio.Semaphore(_MAX_CONCURRENT_READS)

        async def one(uuid: str) -> PlayerStatus:
            async with limit:
                return await self.status(uuid, with_sessions=with_sessions)

        return list(await asyncio.gather(*(one(uuid) for uuid in uuids)))

    async def friends_status(
        self,
        uuid: Optional[str] = None,
        include_self: bool = True,
        with_sessions: bool = True,
    ) -> List[PlayerStatus]:
        """Who of your friends is playing right now — you first, then them."""
        who = self._who(uuid)
        uuids = [who] if include_self else []
        uuids.extend(u for u in await self.friend_uuids(who) if u != who)
        return await self.statuses(uuids, with_sessions=with_sessions)
