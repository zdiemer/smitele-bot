"""Reading a Smite 2 lobby out of SmiteSource's shape.

The fixtures are trimmed from a real `matches.getMatch` response captured
against a live casual Conquest match on 2026-08-31 — same field names, same
nesting, same two-uuids-per-row ambiguity — because every way this parsing can
be wrong is quiet. A row matched on the wrong uuid finds nobody and reads as
"not in this lobby"; a team mapped the wrong way round turns five allies into
five enemies. Neither raises.
"""

from __future__ import annotations

import pytest

smitesource = pytest.importorskip("smite2.smitesource")
players_module = pytest.importorskip("smite2.players")

LiveMatch = players_module.LiveMatch
SmiteSourceUnavailable = smitesource.SmiteSourceUnavailable

# The uuid `/player/<id>` and every `playerUuid` argument mean. It is the one
# on `smitePlayer.person`, never the `hirezPlayerUuid` beside it.
ME = "247b8c65-8ec4-4f01-bfac-2a3007056bcd"
MATCH_ID = "178e2f5b-82dd-42e9-a229-621df2cb8fc3"


def row(display: str, god: str, team: int, public_uuid: str, hirez_uuid: str = ""):
    """One player row in the shape `getMatch` returns."""
    return {
        "teamId": team,
        # Present, different, and deliberately wrong to match on.
        "hirezPlayerUuid": hirez_uuid or f"hirez-{public_uuid}",
        "godMaster": {"canonicalName": god, "slug": god.lower()},
        "godRawName": f"Gods.{god}",
        "smitePlayer": {
            "displayName": display,
            "person": {"publicUuid": public_uuid, "displayName": display},
        },
    }


LOBBY = {
    "hirezMatchId": MATCH_ID,
    "queueType": "casual_conquest",
    "lobbyType": "Casual",
    "isComplete": False,
    "liveUpdatedAt": "2026-08-31T17:13:05.506Z",
    "players": [
        row("Syreon", "Anubis", 1, "44864b58-d719-4ef8-b53e-61d1e0f4e847"),
        row("Bunixoxo", "Osiris", 1, "9dbde740-f850-449a-ae3c-48165c678aff"),
        row("Knights", "Geb", 1, "25d45cc5-06ea-464f-bd9a-b67d308aa7ba"),
        row("Arkf", "Jing Wei", 1, "fa38943f-8f67-42d3-a6da-165e86ad24c5"),
        row("Fudge Rippler", "Loki", 1, "549fd7ed-989e-453c-9eb6-41c9d23d716c"),
        row("Illydotcom", "Nu Wa", 2, "29261561-80cb-4350-b2bc-bb8085c9dea1"),
        row("SoloOrTroll", "Gilgamesh", 2, "5fa8fd8a-cc1b-4d7d-ab03-17f0674c6c5a"),
        row("Fake", "Chiron", 2, "91de9b94-c2f5-4b18-9198-5a7bb2f9e97d"),
        row("PLOUDPAK420", "Neith", 2, "28e2e88b-3b0e-4a4a-b20f-5f346f2cfcd4"),
        row("juicytot6298", "Artio", 2, ME),
    ],
}


class FakeClient:
    """Stands in for the three procedures `live_match` calls."""

    def __init__(self, uuid=ME, match_id=MATCH_ID, match=None, resolve_error=None):
        self.uuid = uuid
        self.match_id = match_id
        self.match_body = LOBBY if match is None else match
        self.resolve_error = resolve_error
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def player_uuid(self, platform, handle):
        self.calls.append(("player_uuid", platform, handle))
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.uuid

    async def live_match_id(self, uuid):
        self.calls.append(("live_match_id", uuid))
        return self.match_id

    async def match(self, match_id):
        self.calls.append(("match", match_id))
        return self.match_body


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        client = FakeClient(**kwargs)
        monkeypatch.setattr(smitesource, "SmiteSourceClient", client)
        return client

    return install


class TestReadingRows:
    def test_teams_are_order_and_chaos(self):
        """Team 1 is Order and team 2 is Chaos, read off a rendered match page
        against the same match's JSON. Reversing it never errors."""
        parsed = smitesource._players(LOBBY)  # noqa: SLF001
        by_name = {name: team for _, _, team, name in parsed}
        assert by_name["Syreon"] == "order"
        assert by_name["juicytot6298"] == "chaos"
        assert sum(1 for _, _, team, _ in parsed if team == "order") == 5

    def test_a_row_without_a_god_is_skipped(self):
        stripped = dict(LOBBY, players=LOBBY["players"] + [{"teamId": 1}])
        assert len(smitesource._players(stripped)) == 10  # noqa: SLF001

    def test_god_falls_back_to_the_raw_name(self):
        """A mid-match ingest can leave the joined god row undone; the raw
        `Gods.Anubis` still names it."""
        partial = dict(LOBBY, players=[dict(LOBBY["players"][0], godMaster=None)])
        assert smitesource._players(partial)[0][1] == "Anubis"  # noqa: SLF001


class TestFindingYourself:
    @pytest.mark.asyncio
    async def test_the_lobby_is_split_around_the_public_uuid(self, fake):
        fake()
        found = await smitesource.live_match("steam", ME)
        assert isinstance(found, LiveMatch)
        assert found.own_god == "Artio"
        assert found.own_team == "chaos"
        assert len(found.players) == 10
        assert sorted(found.allies) == sorted(
            ["Nu Wa", "Gilgamesh", "Chiron", "Neith"]
        )
        assert len(found.enemies) == 5
        assert found.mode_name == "Casual Conquest"
        assert found.ranked is False
        # The footer credits whoever actually answered.
        assert found.source == smitesource.SOURCE_NAME
        # And ages it against the site's own refresh, not when we asked.
        assert found.snapshot_at == pytest.approx(1788196385.506, abs=0.01)

    @pytest.mark.asyncio
    async def test_matching_the_other_uuid_would_find_nobody(self, fake):
        """`hirezPlayerUuid` sits on the same row and is not what a player
        lookup is keyed by. Asking with one is not an answer, so it must raise
        rather than quietly return a sideless lobby."""
        fake(uuid="hirez-" + ME)
        with pytest.raises(SmiteSourceUnavailable):
            await smitesource.live_match("steam", "76561198242178092")

    @pytest.mark.asyncio
    async def test_a_uuid_handle_skips_resolution(self, fake):
        client = fake()
        await smitesource.live_match("steam", ME)
        assert [call[0] for call in client.calls] == ["live_match_id", "match"]

    @pytest.mark.asyncio
    async def test_a_display_name_is_resolved_first(self, fake):
        client = fake()
        await smitesource.live_match("steam", "76561198242178092")
        assert client.calls[0] == ("player_uuid", "steam", "76561198242178092")


class TestWhenThereIsNoAnswer:
    @pytest.mark.asyncio
    async def test_not_in_a_match_is_none_not_an_error(self, fake):
        """A `None` sends nobody to tracker.gg — it is a real, fresh answer,
        and re-asking would replace it with a ten-minute-old one."""
        fake(match_id=None)
        assert await smitesource.live_match("steam", ME) is None

    @pytest.mark.asyncio
    async def test_an_unresolvable_handle_raises(self, fake):
        """A display name resolves to nothing here, and that has to fall
        through to tracker.gg rather than read as 'not playing'."""
        fake(uuid=None)
        with pytest.raises(SmiteSourceUnavailable):
            await smitesource.live_match("steam", "SomeDisplayName")

    @pytest.mark.asyncio
    async def test_a_match_that_ended_between_calls_is_none(self, fake):
        fake(match=dict(LOBBY, isComplete=True))
        assert await smitesource.live_match("steam", ME) is None


class TestTheEnvelope:
    @pytest.mark.asyncio
    async def test_a_non_200_is_unavailable(self):
        client = smitesource.SmiteSourceClient()
        client._SmiteSourceClient__session = FakeSession(status=500)  # noqa: SLF001
        with pytest.raises(SmiteSourceUnavailable):
            await client.rpc("matches.getMatch", {})

    @pytest.mark.asyncio
    async def test_a_body_without_the_json_key_is_unavailable(self):
        client = smitesource.SmiteSourceClient()
        client._SmiteSourceClient__session = FakeSession(body=b'{"nope":1}')  # noqa: SLF001
        with pytest.raises(SmiteSourceUnavailable):
            await client.rpc("matches.getMatch", {})

    @pytest.mark.asyncio
    async def test_the_json_key_is_unwrapped(self):
        client = smitesource.SmiteSourceClient()
        client._SmiteSourceClient__session = FakeSession(body=b'{"json":{"a":1}}')  # noqa: SLF001
        assert await client.rpc("matches.getMatch", {}) == {"a": 1}


class FakeSession:
    def __init__(self, status=200, body=b'{"json":null}'):
        self.status = status
        self.body = body

    async def get(self, *args, **kwargs):
        class Response:
            status_code = self.status
            content = self.body

        return Response()


class TestSmallParts:
    def test_a_uuid_is_recognised(self):
        assert smitesource.looks_like_uuid(ME)
        assert not smitesource.looks_like_uuid("76561198242178092")
        assert not smitesource.looks_like_uuid("")

    def test_queue_types_read_as_names(self):
        assert smitesource._pretty_mode("casual_conquest") == "Casual Conquest"  # noqa: SLF001
        assert smitesource._pretty_mode("") == ""  # noqa: SLF001

    def test_the_stamp_is_read_as_the_utc_it_declares(self):
        """`Z` is not a local time. Reading it as one is wrong by the host's
        offset, which shows up as an age the footer reports hours out."""
        assert smitesource._epoch("2026-08-31T17:13:05.506Z") == pytest.approx(  # noqa: SLF001
            1788196385.506, abs=0.01
        )
        assert smitesource._epoch(None) == 0.0  # noqa: SLF001
        assert smitesource._epoch("not a date") == 0.0  # noqa: SLF001

    def test_a_lobby_defaults_to_naming_tracker(self):
        """Every lobby built before there was a second backend came from
        tracker.gg, so that has to stay the default the footer credits."""
        assert (
            LiveMatch(
                match_id="x", mode="", mode_name="", ranked=False,
                own_god="", own_team="", players=[],
            ).source
            == "tracker.gg"
        )


def tracker_segment(god: str, team: str, handle: str) -> dict:
    return {
        "type": "overview",
        "attributes": {"teamId": team},
        "metadata": {"godName": god, "platformUserHandle": handle},
    }


class FakeTracker:
    """The two tracker.gg calls `PlayerLookups.live_match` makes."""

    def __init__(self):
        self.calls = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def live_match(self, platform, handle):
        self.calls.append("live_match")
        return {
            "attributes": {"id": "tracker-match", "teamId": "order"},
            "metadata": {"godName": "Achilles"},
        }

    async def match(self, match_id):
        self.calls.append("match")
        return {
            "attributes": {"gamemode": "conquest"},
            "metadata": {"gamemodeName": "Conquest", "isRanked": False},
            "segments": [
                tracker_segment("Achilles", "order", "zachd"),
                tracker_segment("Bastet", "chaos", "someone"),
            ],
        }


def lookups():
    tracker = FakeTracker()
    return players_module.PlayerLookups(tracker, silent=True), tracker


class TestDispatch:
    """Which backend `PlayerLookups.live_match` actually asks."""

    @pytest.mark.asyncio
    async def test_off_by_default_nothing_changes(self, monkeypatch):
        monkeypatch.delenv(smitesource.ENV_VAR, raising=False)
        looker, tracker = lookups()
        found = await looker.live_match("steam", "zachd")
        assert tracker.calls == ["live_match", "match"]
        assert found.source == "tracker.gg"

    @pytest.mark.asyncio
    async def test_on_it_answers_without_tracker(self, monkeypatch, fake):
        monkeypatch.setenv(smitesource.ENV_VAR, "smitesource")
        fake()
        looker, tracker = lookups()
        found = await looker.live_match("steam", ME)
        assert found.own_god == "Artio"
        assert tracker.calls == []

    @pytest.mark.asyncio
    async def test_unavailable_falls_through_to_tracker(self, monkeypatch):
        """A handle SmiteSource cannot resolve is not an answer, so the older
        source still gets asked. This is what makes the toggle safe to flip."""
        monkeypatch.setenv(smitesource.ENV_VAR, "smitesource")

        async def unavailable(*args, **kwargs):
            raise SmiteSourceUnavailable("no player")

        monkeypatch.setattr(smitesource, "live_match", unavailable)
        looker, tracker = lookups()
        found = await looker.live_match("steam", "SomeDisplayName")
        assert tracker.calls == ["live_match", "match"]
        assert found.source == "tracker.gg"

    @pytest.mark.asyncio
    async def test_a_fresh_no_is_not_second_guessed(self, monkeypatch, fake):
        """"Not in a match" from the fresher source is an answer. Asking
        tracker.gg to confirm it spends a request to replace a four-minute-old
        no with a ten-minute-old one."""
        monkeypatch.setenv(smitesource.ENV_VAR, "smitesource")
        fake(match_id=None)
        looker, tracker = lookups()
        assert await looker.live_match("steam", ME) is None
        assert tracker.calls == []

    @pytest.mark.asyncio
    async def test_the_cache_does_not_survive_a_flip(self, monkeypatch, fake):
        """Both backends answer for the same player and one is minutes
        fresher, so a shared key would serve the source just turned off."""
        monkeypatch.setenv(smitesource.ENV_VAR, "smitesource")
        fake()
        looker, tracker = lookups()
        assert (await looker.live_match("steam", ME)).source == smitesource.SOURCE_NAME

        monkeypatch.delenv(smitesource.ENV_VAR, raising=False)
        assert (await looker.live_match("steam", ME)).source == "tracker.gg"
        assert tracker.calls == ["live_match", "match"]


class FakeProvider:
    """What `live_lobby._smite2` reaches for on a provider."""

    def __init__(self, lookups):
        from game import Game  # noqa: PLC0415

        self.game = Game.SMITE_2
        self.players = lookups
        self.seen = []

    def god_id_from_name(self, name):
        self.seen.append(name)
        # Any stable non-None id; the build code only needs identity.
        return abs(hash(name)) % 10000


class TestTheBuildCommandPath:
    """`/build` infers game state from the same lookup, through `live_lobby`.

    It is a separate caller with a separate failure mode — it gives the whole
    matchup four seconds and posts without one rather than late with one — so
    the toggle reaching it is worth pinning rather than assuming.
    """

    @pytest.mark.asyncio
    async def test_the_build_lobby_comes_from_smitesource_when_on(
        self, monkeypatch, fake
    ):
        live_lobby = pytest.importorskip("live_lobby")
        monkeypatch.setenv(smitesource.ENV_VAR, "smitesource")
        fake()
        looker, tracker = lookups()
        lobby = await live_lobby.lookup(FakeProvider(looker), f"steam:{ME}")
        assert lobby is not None and lobby.known
        assert lobby.mode_name == "Casual Conquest"
        # Four allies and five enemies, from the SmiteSource lobby.
        assert len(lobby.allies) == 4
        assert len(lobby.enemies) == 5
        assert tracker.calls == []

    @pytest.mark.asyncio
    async def test_the_build_lobby_still_falls_back(self, monkeypatch):
        live_lobby = pytest.importorskip("live_lobby")
        monkeypatch.setenv(smitesource.ENV_VAR, "smitesource")

        async def unavailable(*args, **kwargs):
            raise SmiteSourceUnavailable("no player")

        monkeypatch.setattr(smitesource, "live_match", unavailable)
        looker, tracker = lookups()
        lobby = await live_lobby.lookup(FakeProvider(looker), "steam:SomeName")
        assert lobby is not None and lobby.known
        assert tracker.calls == ["live_match", "match"]

    @pytest.mark.asyncio
    async def test_a_slow_source_costs_a_matchup_not_a_build(self, monkeypatch):
        """`live_lobby` caps the lookup, so a hung source degrades a build to
        one without a matchup. That is why the per-request timeout is sized
        under this cap rather than over it."""
        live_lobby = pytest.importorskip("live_lobby")
        monkeypatch.setattr(live_lobby, "LOOKUP_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setenv(smitesource.ENV_VAR, "smitesource")

        async def slow(*args, **kwargs):
            import asyncio  # noqa: PLC0415

            await asyncio.sleep(5)

        monkeypatch.setattr(smitesource, "live_match", slow)
        looker, _ = lookups()
        assert await live_lobby.lookup(FakeProvider(looker), f"steam:{ME}") is None


class TestTheToggle:
    def test_the_default_is_tracker(self, monkeypatch):
        monkeypatch.delenv(smitesource.ENV_VAR, raising=False)
        assert smitesource.selected_source() == smitesource.TRACKER

    def test_it_is_opt_in(self, monkeypatch):
        monkeypatch.setenv(smitesource.ENV_VAR, "smitesource")
        assert smitesource.selected_source() == smitesource.SMITESOURCE

    def test_case_and_padding_do_not_matter(self, monkeypatch):
        monkeypatch.setenv(smitesource.ENV_VAR, "  SmiteSource ")
        assert smitesource.selected_source() == smitesource.SMITESOURCE

    def test_a_typo_degrades_to_today(self, monkeypatch):
        """Anything unrecognised has to mean tracker.gg. A misspelling that
        disabled the lookup entirely would be a silent outage."""
        monkeypatch.setenv(smitesource.ENV_VAR, "smitsource")
        assert smitesource.selected_source() == smitesource.TRACKER
