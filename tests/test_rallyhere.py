"""The RallyHere client, exercised where being wrong is expensive.

The expensive failures are all about the session: losing it means relaunching
the game under mitmproxy to capture another, which is the tedium this module
exists to end. So the renewal path is pinned hard — one refresh under
concurrency, a 401 retried once rather than looped, and the seed-versus-stored
precedence that decides which credential a restart comes up with.

Nothing reaches the network: the session is a fake that hands back scripted
statuses and records what was asked. The presence shape is a real one, read off
the live backend on 2026-08-11; the sessions envelope is not — no live session
has been observed yet — and the tests that lean on it say so.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import time

import pytest

aiohttp = pytest.importorskip("aiohttp", reason="aiohttp not installed")

from smite2 import egress  # noqa: E402
from smite2 import rallyhere as rh  # noqa: E402


@pytest.fixture(autouse=True)
def no_ambient_environment(monkeypatch, tmp_path):
    """The host's own environment must not decide what a test measures.

    Including the seed: a developer with a real capture exported would
    otherwise have `load()` reach for their live token.
    """
    monkeypatch.setattr(egress, "proxy_url", lambda: None)
    for name in (
        rh.STATE_ENV,
        rh.TOKEN_ENV,
        rh.REFRESH_ENV,
        rh.CLIENT_BASIC_ENV,
        rh.BASE_URL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    # Nothing may fall back to the checkout's own data directory.
    monkeypatch.setattr(rh.paths, "DATA_DIR", str(tmp_path / "empty-data"))


def jwt(exp: float, **claims) -> str:
    """A JWT-shaped token. Only the middle segment is ever read."""
    payload = {"exp": int(exp), **claims}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """Stands in for aiohttp: scripted answers, and a log of what was asked.

    `gets` maps a path substring to either one (status, body) pair or a list of
    them consumed in order — which is how the "401 then 200" retry is written.
    `posts` is the login endpoint; each call pops the next scripted answer.
    """

    def __init__(self, gets=None, posts=None) -> None:
        self._gets = gets or {}
        self._posts = list(posts or [])
        self.get_calls = []
        self.post_calls = []

    def get(self, url, headers=None, params=None, **kwargs):
        self.get_calls.append((url, headers or {}, params))
        for fragment, scripted in self._gets.items():
            if fragment in url:
                if isinstance(scripted, list):
                    status, body = scripted.pop(0) if scripted else (404, "{}")
                else:
                    status, body = scripted
                return FakeResponse(status, body)
        return FakeResponse(404, "{}")

    def post(self, url, headers=None, json=None, **kwargs):
        self.post_calls.append((url, headers or {}, json))
        status, body = self._posts.pop(0) if self._posts else (200, "{}")
        return FakeResponse(status, body)


def auth_for(access: str, refresh="refresh-1", basic="client:secret", path=None):
    state = rh.TokenState(
        base_url=rh.DEFAULT_BASE_URL,
        access_token=access,
        refresh_token=refresh,
        client_basic=basic,
        exp=rh._token_expiry(access),
    )
    return rh.RallyHereAuth(state, path=path)


def client_with(session: FakeSession, auth=None) -> rh.RallyHereClient:
    auth = auth or auth_for(jwt(time.time() + 3600, player_uuid="me"))
    client = rh.RallyHereClient(auth)
    client._session = session
    return client


def login_body(access: str, refresh="refresh-2") -> str:
    return json.dumps(
        {"access_token": access, "refresh_token": refresh, "expires_in": 21600}
    )


class TestCaptureLoading:
    def test_the_client_credential_is_lifted_out_of_the_auth_requests(self):
        """The capture addon records the login call; the Basic is inside it."""
        state = rh.TokenState.from_capture(
            {
                "token": jwt(time.time() + 60, player_uuid="me"),
                "refresh_token": "r",
                "auth_requests": [
                    {"method": "GET", "url": "https://x/health"},
                    {"method": "POST", "url": "https://x/users/v1/login",
                     "client_basic_decoded": "client-id:client-secret"},
                ],
            }
        )
        assert state.client_basic == "client-id:client-secret"

    def test_expiry_comes_from_the_token_not_the_file(self):
        """A stale `exp` beside a fresh token would refresh on every call."""
        expires = int(time.time() + 1234)
        state = rh.TokenState.from_capture(
            {"token": jwt(expires), "exp": 1, "refresh_token": "r"}
        )
        assert state.exp == expires

    def test_an_unreadable_token_falls_back_to_the_recorded_expiry(self):
        state = rh.TokenState.from_capture({"token": "not-a-jwt", "exp": 99})
        assert state.exp == 99

    def test_the_self_uuid_is_read_off_the_token(self):
        auth = auth_for(jwt(time.time() + 60, active_player_uuid="uuid-me"))
        assert auth.self_uuid == "uuid-me"


class TestRenewal:
    async def test_a_fresh_token_is_handed_back_without_a_request(self):
        session = FakeSession()
        auth = auth_for(jwt(time.time() + 3600))
        assert await auth.bearer(session) == auth.state.access_token
        assert session.post_calls == []

    async def test_a_token_inside_the_skew_window_is_renewed(self):
        """Renew *before* expiry, so no request races an expiring token."""
        fresh = jwt(time.time() + 7200)
        session = FakeSession(posts=[(200, login_body(fresh))])
        auth = auth_for(jwt(time.time() + rh._REFRESH_SKEW_SECONDS - 10))
        assert await auth.bearer(session) == fresh
        assert len(session.post_calls) == 1

    async def test_the_refresh_presents_the_captured_client_credential(self):
        fresh = jwt(time.time() + 7200)
        session = FakeSession(posts=[(200, login_body(fresh))])
        auth = auth_for(jwt(time.time() - 1), basic="an-id:a-secret")
        await auth.bearer(session)
        url, headers, body = session.post_calls[0]
        assert url.endswith(rh._LOGIN_PATH)
        assert headers["Authorization"] == "Basic " + base64.b64encode(
            b"an-id:a-secret"
        ).decode()
        assert body["grant_type"] == "refresh"
        assert body["portal_access_token"] == "refresh-1"

    async def test_twenty_concurrent_reads_renew_once(self):
        """Fanning out over a group must not mint twenty tokens.

        Every coroutine finds the token stale in the same instant. Without the
        lock each mints its own, which is a burst of identical logins from one
        bearer — waste, and the shape of a client nobody wants to look like.
        """
        fresh = jwt(time.time() + 7200)
        session = FakeSession(posts=[(200, login_body(fresh))])
        auth = auth_for(jwt(time.time() - 1))
        tokens = await asyncio.gather(*(auth.bearer(session) for _ in range(20)))
        assert len(session.post_calls) == 1
        assert set(tokens) == {fresh}

    async def test_a_rotated_refresh_token_is_kept_if_one_ever_arrives(self):
        """Live, RallyHere hands back the same refresh token (2026-08-11).

        Pinned anyway: on the day it starts rotating, dropping the new one
        would end the session at the next renewal, looking exactly like an
        expiry and sending you back to the game for no reason.
        """
        session = FakeSession(
            posts=[(200, login_body(jwt(time.time() + 7200), "refresh-rotated"))]
        )
        auth = auth_for(jwt(time.time() - 1))
        await auth.bearer(session)
        assert auth.state.refresh_token == "refresh-rotated"

    async def test_a_renewal_that_returns_no_refresh_token_keeps_the_old_one(self):
        session = FakeSession(
            posts=[(200, json.dumps({"access_token": jwt(time.time() + 7200)}))]
        )
        auth = auth_for(jwt(time.time() - 1), refresh="refresh-1")
        await auth.bearer(session)
        assert auth.state.refresh_token == "refresh-1"

    async def test_retiring_a_token_someone_else_already_replaced_is_free(self):
        """Concurrent 401s must not each trigger their own renewal."""
        session = FakeSession(posts=[(200, login_body(jwt(time.time() + 7200)))])
        auth = auth_for(jwt(time.time() + 3600))
        current = auth.state.access_token
        assert await auth.bearer(session, retire="a-token-from-before") == current
        assert session.post_calls == []

    async def test_a_rejected_refresh_says_recapture(self):
        session = FakeSession(posts=[(401, '{"error":"invalid_grant"}')])
        auth = auth_for(jwt(time.time() - 1))
        with pytest.raises(rh.RallyHereAuthError, match="re-capture"):
            await auth.bearer(session)

    async def test_no_refresh_token_is_an_auth_error_not_a_crash(self):
        auth = auth_for(jwt(time.time() - 1), refresh=None)
        with pytest.raises(rh.RallyHereAuthError, match="capture a fresh token"):
            await auth.bearer(FakeSession())

    async def test_the_rotated_token_is_persisted_owner_only(self, tmp_path):
        """One capture has to sustain the bot across restarts."""
        path = tmp_path / "rh_state.json"
        path.write_text(json.dumps({"token": "old", "refresh_token": "refresh-1"}))
        fresh = jwt(time.time() + 7200)
        session = FakeSession(posts=[(200, login_body(fresh, "refresh-2"))])
        auth = auth_for(jwt(time.time() - 1), path=str(path))
        auth._raw = json.loads(path.read_text())
        await auth.bearer(session)

        written = json.loads(path.read_text())
        assert written["token"] == fresh
        assert written["refresh_token"] == "refresh-2"
        # It holds a live credential; nobody else on the box needs to read it.
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    async def test_load_reads_the_state_path_from_the_environment(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "capture.json"
        path.write_text(json.dumps({"token": jwt(time.time() + 60, player_uuid="me")}))
        monkeypatch.setenv(rh.STATE_ENV, str(path))
        assert rh.RallyHereAuth.load().self_uuid == "me"

    def test_load_with_nothing_at_all_says_where_to_get_one(self):
        with pytest.raises(rh.RallyHereAuthError, match="scripts/win"):
            rh.RallyHereAuth.load()


class TestSeeding:
    """A read-only Secret seeding a state file that has to be writable.

    Getting the precedence backwards is a mistake in both directions: prefer
    the seed always and every restart throws away the token this process
    renewed; prefer the file always and re-capturing after an expiry does
    nothing at all.
    """

    def seed(self, monkeypatch, token, refresh="refresh-1", basic="id:secret"):
        monkeypatch.setenv(rh.TOKEN_ENV, token)
        monkeypatch.setenv(rh.REFRESH_ENV, refresh)
        monkeypatch.setenv(rh.CLIENT_BASIC_ENV, basic)

    def test_a_first_start_writes_the_state_file_from_the_environment(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "rh_state.json"
        monkeypatch.setenv(rh.STATE_ENV, str(path))
        token = jwt(time.time() + 3600, player_uuid="me")
        self.seed(monkeypatch, token)

        auth = rh.RallyHereAuth.load()
        assert auth.state.access_token == token
        assert auth.state.client_basic == "id:secret"
        # Written immediately, so the fingerprint marking this capture as
        # seeded exists even if the process dies before its first renewal.
        assert json.loads(path.read_text())["refresh_token"] == "refresh-1"

    def test_a_restart_keeps_the_renewed_token_over_the_seed(
        self, tmp_path, monkeypatch
    ):
        """The stored token is newer than the one baked into the deployment."""
        path = tmp_path / "rh_state.json"
        monkeypatch.setenv(rh.STATE_ENV, str(path))
        self.seed(monkeypatch, jwt(time.time() + 3600, player_uuid="me"))
        rh.RallyHereAuth.load()

        renewed_token = jwt(time.time() + 7200, player_uuid="me")
        stored = json.loads(path.read_text())
        stored["token"] = renewed_token
        path.write_text(json.dumps(stored))

        assert rh.RallyHereAuth.load().state.access_token == renewed_token

    def test_a_new_capture_in_the_secret_overrides_the_stored_state(
        self, tmp_path, monkeypatch
    ):
        """Re-capturing is how a dead session is fixed; it has to take."""
        path = tmp_path / "rh_state.json"
        monkeypatch.setenv(rh.STATE_ENV, str(path))
        self.seed(monkeypatch, jwt(time.time() + 3600, player_uuid="me"))
        rh.RallyHereAuth.load()

        fresh = jwt(time.time() + 7200, player_uuid="me")
        self.seed(monkeypatch, fresh, refresh="refresh-fresh")
        auth = rh.RallyHereAuth.load()
        assert auth.state.access_token == fresh
        assert auth.state.refresh_token == "refresh-fresh"

    def test_a_re_seed_does_not_inherit_the_old_expiry(self, tmp_path, monkeypatch):
        """A stale `exp` beside a fresh token means renewing on every call."""
        path = tmp_path / "rh_state.json"
        path.write_text(
            json.dumps({"token": "old", "exp": 1, "seed_fingerprint": "stale"})
        )
        monkeypatch.setenv(rh.STATE_ENV, str(path))
        expires = int(time.time() + 4321)
        self.seed(monkeypatch, jwt(expires, player_uuid="me"))
        assert rh.RallyHereAuth.load().state.exp == expires

    def test_the_seed_records_a_digest_and_never_the_token(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "rh_state.json"
        monkeypatch.setenv(rh.STATE_ENV, str(path))
        self.seed(monkeypatch, jwt(time.time() + 3600), refresh="a-secret-refresh")
        rh.RallyHereAuth.load()

        written = json.loads(path.read_text())
        assert "a-secret-refresh" not in written["seed_fingerprint"]
        assert len(written["seed_fingerprint"]) == 16

    def test_the_base_url_is_overridable_for_another_environment(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(rh.STATE_ENV, str(tmp_path / "rh_state.json"))
        self.seed(monkeypatch, jwt(time.time() + 3600))
        monkeypatch.setenv(rh.BASE_URL_ENV, "https://other.rally-here.io")
        assert rh.RallyHereAuth.load().state.base_url == "https://other.rally-here.io"

    def test_the_default_state_path_is_on_the_data_volume(
        self, tmp_path, monkeypatch
    ):
        """No SMITELE_RH_STATE in the chart; the default has to be writable."""
        monkeypatch.setattr(rh.paths, "DATA_DIR", str(tmp_path))
        self.seed(monkeypatch, jwt(time.time() + 3600))
        rh.RallyHereAuth.load()
        assert (tmp_path / rh.FILE_NAME).exists()

    def test_a_seed_without_a_client_credential_still_loads(
        self, tmp_path, monkeypatch
    ):
        """It reads fine for six hours; only the renewal needs the Basic.

        Worth loading rather than refusing, so a capture that missed the login
        request fails at the renewal with a message about it — not at startup.
        """
        monkeypatch.setenv(rh.STATE_ENV, str(tmp_path / "rh_state.json"))
        monkeypatch.setenv(rh.TOKEN_ENV, jwt(time.time() + 3600, player_uuid="me"))
        monkeypatch.setenv(rh.REFRESH_ENV, "refresh-1")
        auth = rh.RallyHereAuth.load()
        assert auth.self_uuid == "me" and auth.state.client_basic is None


class TestReads:
    async def test_a_401_is_retried_once_with_a_renewed_token(self):
        """A token can be revoked before its own `exp`; renew and retry."""
        fresh = jwt(time.time() + 7200)
        session = FakeSession(
            gets={"/presence": [(401, "expired"), (200, '{"status":"online"}')]},
            posts=[(200, login_body(fresh))],
        )
        client = client_with(session)
        assert await client.presence("them") == {"status": "online"}
        assert len(session.post_calls) == 1
        assert session.get_calls[1][1]["Authorization"] == f"Bearer {fresh}"

    async def test_a_second_401_is_reported_rather_than_looped(self):
        session = FakeSession(
            gets={"/presence": [(401, "nope"), (401, "still nope")]},
            posts=[(200, login_body(jwt(time.time() + 7200)))],
        )
        with pytest.raises(rh.RallyHereHTTPError) as caught:
            await client_with(session).presence("them")
        assert caught.value.status == 401

    async def test_a_403_carries_the_path_and_the_body(self):
        session = FakeSession(gets={"/session": (403, "Not authenticated")})
        with pytest.raises(rh.RallyHereHTTPError, match="Not authenticated"):
            await client_with(session).sessions("them")

    async def test_a_read_with_no_uuid_uses_the_token_owner(self):
        session = FakeSession(gets={"/match": (200, "{}")})
        await client_with(session).matches()
        assert "/match/v1/player/me/match" in session.get_calls[0][0]

    async def test_a_read_with_no_uuid_and_no_owner_refuses_to_guess(self):
        auth = auth_for(jwt(time.time() + 3600))  # no player_uuid claim
        with pytest.raises(rh.RallyHereError, match="no player uuid"):
            await client_with(FakeSession(), auth).presence()

    async def test_friend_uuids_are_deduplicated_and_exclude_yourself(self):
        body = json.dumps(
            {
                "friends": [
                    {"player_uuid": "me", "other_player_uuid": "friend-a"},
                    {"player_uuid": "me", "other_player_uuid": "friend-b"},
                    {"player_uuid": "me", "other_player_uuid": "friend-a"},
                ]
            }
        )
        client = client_with(FakeSession(gets={"/friends": (200, body)}))
        assert await client.friend_uuids() == ["friend-a", "friend-b"]


class TestSteamResolution:
    """steam id -> uuid, the hop that makes the roster reachable at all.

    The shapes here are the live ones (2026-08-11): `/users/v1/player` answers
    with the identity keyed twice, and an unknown handle is an absent row, not a
    null. Getting "absent" wrong is the costly bug — a bogus id mapped to a real
    player's status would report the wrong person as in a match.
    """

    def lookup_body(self, mapping):
        """The `identity_platforms_by_platform` shape, one row per steam id."""
        rows = [
            {"identity": {sid: {"player_id": 1, "player_uuid": uuid}}}
            for sid, uuid in mapping.items()
        ]
        return json.dumps(
            {
                "identity_platforms_by_platform": {"Steam": rows},
                "identity_platforms": {"5": rows},
            }
        )

    async def test_a_steam_id_resolves_to_its_uuid(self):
        body = self.lookup_body({"765611980": "uuid-a"})
        session = FakeSession(gets={"/users/v1/player": (200, body)})
        assert await client_with(session).uuid_by_steam("765611980") == "uuid-a"

    async def test_an_unknown_handle_is_absent_not_none(self):
        """`.get(id)` must tell "no such player" apart from a resolved one."""
        session = FakeSession(gets={"/users/v1/player": (200, self.lookup_body({}))})
        resolved = await client_with(session).uuids_by_steam(["70000"])
        assert resolved == {}
        assert await client_with(session).uuid_by_steam("70000") is None

    async def test_a_whole_roster_resolves_in_one_request(self):
        body = self.lookup_body({"a": "uuid-a", "b": "uuid-b", "c": "uuid-c"})
        session = FakeSession(gets={"/users/v1/player": (200, body)})
        resolved = await client_with(session).uuids_by_steam(["a", "b", "c"])
        assert resolved == {"a": "uuid-a", "b": "uuid-b", "c": "uuid-c"}
        assert len(session.get_calls) == 1
        # The identities ride as an array param, not a joined string.
        assert session.get_calls[0][2]["identities"] == ["a", "b", "c"]

    async def test_a_large_roster_is_chunked(self):
        ids = [f"id-{index}" for index in range(45)]
        body = self.lookup_body({sid: f"uuid-{sid}" for sid in ids})
        session = FakeSession(gets={"/users/v1/player": (200, body)})
        resolved = await client_with(session).uuids_by_steam(ids)
        assert len(resolved) == 45
        # 45 over a batch of 20 is three requests.
        assert len(session.get_calls) == 3

    async def test_duplicate_ids_collapse_before_the_request(self):
        body = self.lookup_body({"a": "uuid-a"})
        session = FakeSession(gets={"/users/v1/player": (200, body)})
        await client_with(session).uuids_by_steam(["a", "a", "a"])
        assert session.get_calls[0][2]["identities"] == ["a"]

    async def test_status_by_steam_folds_resolution_and_read(self):
        body = self.lookup_body({"765": "uuid-a"})
        session = FakeSession(
            gets={
                "/users/v1/player": (200, body),
                "/presence": (200, json.dumps({"status": "online"})),
                "/session": (200, "{}"),
            }
        )
        status = await client_with(session).status_by_steam("765")
        assert status.uuid == "uuid-a" and status.online

    async def test_status_by_steam_of_an_unknown_handle_is_none(self):
        session = FakeSession(gets={"/users/v1/player": (200, self.lookup_body({}))})
        assert await client_with(session).status_by_steam("70000") is None

    async def test_roster_status_keys_by_steam_id(self):
        body = self.lookup_body({"a": "uuid-a", "b": "uuid-b"})
        session = FakeSession(
            gets={
                "/users/v1/player": (200, body),
                "/presence": (200, json.dumps({"status": "offline"})),
            }
        )
        statuses = await client_with(session).roster_status(["a", "b"])
        assert set(statuses) == {"a", "b"}
        assert statuses["a"].uuid == "uuid-a"


class TestStatus:
    def presence_body(self, status="online", **fields):
        return json.dumps({"status": status, "display_name": "Zach", **fields})

    def sessions_body(self, *pairs):
        return json.dumps(
            {"sessions": [{"session_id": sid, "session_type": kind} for sid, kind in pairs]}
        )

    async def test_the_live_presence_shape_reads(self):
        """Verbatim from the backend, 2026-08-11 — the one real shape here.

        Note what it says: `status` "offline" while the message still carries
        `InLobby` from the last session. The message outlives the presence that
        set it, which is why `state` is documented as needing `online` read
        first rather than standing on its own.
        """
        body = json.dumps(
            {
                "status": "offline",
                "message": '{\r\n\t"state": "InLobby"\r\n}',
                "platform": "Steam",
                "display_name": "axofrats",
                "custom_data": {},
                "player_uuid": "8da0474e-4a27-53f6-95f7-1943cc955448",
                "do_not_disturb": False,
            }
        )
        status = await client_with(FakeSession(gets={"/presence": (200, body)})).status(
            "8da0474e-4a27-53f6-95f7-1943cc955448"
        )
        assert (status.online, status.state) == (False, "InLobby")
        assert (status.display_name, status.platform) == ("axofrats", "Steam")

    async def test_a_message_that_is_not_the_state_document_is_left_alone(self):
        """It is a free-text field; only this client's convention makes it more."""
        session = FakeSession(
            gets={"/presence": (200, self.presence_body(message="afk brb"))}
        )
        assert (await client_with(session).status("friend-a")).state is None

    async def test_an_online_player_in_a_match_reports_the_session_id(self):
        session = FakeSession(
            gets={
                "/presence": (200, self.presence_body("online", platform="steam")),
                "/session": (200, self.sessions_body(("sess-1", "matchmaking"))),
            }
        )
        status = await client_with(session).status("friend-a")
        assert (status.online, status.in_match) == (True, True)
        assert status.match_session_id == "sess-1"
        assert (status.display_name, status.platform) == ("Zach", "steam")

    async def test_a_party_is_not_a_match(self):
        session = FakeSession(
            gets={
                "/presence": (200, self.presence_body()),
                "/session": (200, self.sessions_body(("sess-p", "party"))),
            }
        )
        status = await client_with(session).status("friend-a")
        assert status.online and not status.in_match
        assert status.match_session_id is None

    async def test_an_unfamiliar_session_type_counts_as_a_match(self):
        """Session types are per-title config; err toward reporting the game.

        Calling a real match a party would answer "nobody is playing" while
        they are in one — the worse of the two ways to be wrong.
        """
        session = FakeSession(
            gets={
                "/presence": (200, self.presence_body()),
                "/session": (200, self.sessions_body(("sess-x", "conquest-ranked"))),
            }
        )
        assert (await client_with(session).status("friend-a")).in_match

    async def test_an_offline_player_costs_one_request(self):
        """No session read for someone who cannot be in one."""
        session = FakeSession(gets={"/presence": (200, self.presence_body("offline"))})
        status = await client_with(session).status("friend-a")
        assert not status.online and status.sessions == []
        assert len(session.get_calls) == 1

    async def test_one_unreadable_friend_does_not_raise(self):
        """A 403 on their presence is about them; the list must survive it."""
        session = FakeSession(gets={"/presence": (403, "Not authenticated")})
        status = await client_with(session).status("friend-a")
        assert status.error and not status.online

    async def test_an_expired_session_leaves_presence_intact(self):
        session = FakeSession(
            gets={
                "/presence": (200, self.presence_body()),
                "/session": (404, "no session"),
            }
        )
        status = await client_with(session).status("friend-a")
        assert status.online and status.error and status.sessions == []

    async def test_a_dead_session_still_raises_for_re_capture(self):
        """`error` is for *their* failures; ours must reach the caller."""
        session = FakeSession(
            gets={"/presence": (401, "expired")},
            posts=[(401, '{"error":"invalid_grant"}')],
        )
        auth = auth_for(jwt(time.time() - 1, player_uuid="me"))
        with pytest.raises(rh.RallyHereAuthError):
            await client_with(session, auth).status("friend-a")

    async def test_friends_status_puts_you_first_then_your_friends(self):
        friends = json.dumps({"friends": [{"other_player_uuid": "friend-a"}]})
        session = FakeSession(
            gets={
                "/friends": (200, friends),
                "/presence": (200, self.presence_body("offline")),
            }
        )
        statuses = await client_with(session).friends_status()
        assert [s.uuid for s in statuses] == ["me", "friend-a"]

    async def test_statuses_keeps_the_order_it_was_given(self):
        """Fanned out concurrently; the answers still line up with the input."""
        session = FakeSession(gets={"/presence": (200, self.presence_body("offline"))})
        uuids = [f"friend-{index}" for index in range(10)]
        statuses = await client_with(session).statuses(uuids)
        assert [s.uuid for s in statuses] == uuids
