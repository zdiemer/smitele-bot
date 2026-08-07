"""The public read side: what it serves, and what it must never serve.

Three properties worth pinning, all of which are one careless line from being
wrong:

  - `/api/*` never falls through to the SPA shell. A client asking for JSON and
    getting `200 text/html` debugs badly, and aiohttp's catch-all route makes
    that the default outcome unless something stops it.
  - readiness does not depend on the snapshot. Tying them means one SMB blip
    cycles every replica out of the Service and takes the whole site down,
    where the right answer is a stale badge on a working page.
  - a missing snapshot is 503, not an empty 200. "The job has not run" and
    "there is genuinely nothing to report" are different, and only one of them
    is somebody's problem.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "HirezAPI"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "match_data_collector"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "web"))

pytest.importorskip("pandas")
aiohttp_test = pytest.importorskip("aiohttp.test_utils")
serve = pytest.importorskip("serve")
snapshot = pytest.importorskip("snapshot")


@pytest.fixture
def site(tmp_path):
    """A bundle and a snapshot directory, wired into a live test client."""
    snapshots = tmp_path / "snapshots"
    dist = tmp_path / "dist"
    snapshots.mkdir()
    dist.mkdir()
    (dist / "index.html").write_text("<title>smite</title>", encoding="utf-8")
    (dist / "app.js").write_text("console.log(1)", encoding="utf-8")

    def write(name, document):
        with open(snapshots / name, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

    return snapshots, dist, write


async def client_for(snapshots, dist):
    app = serve.build_app(directory=str(snapshots), dist=str(dist))
    client = aiohttp_test.TestClient(aiohttp_test.TestServer(app))
    await client.start_server()
    return client


class TestApiNeverFallsThroughToTheShell:
    async def test_unknown_api_path_is_json_404(self, site):
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/api/nope")

            assert response.status == 404
            assert response.content_type == "application/json"
            assert "error" in await response.json()
        finally:
            await client.close()

    async def test_a_deep_unknown_api_path_is_still_json(self, site):
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/api/v2/builds/anubis")

            assert response.status == 404
            assert response.content_type == "application/json"
        finally:
            await client.close()

    async def test_a_client_route_does_get_the_shell(self, site):
        # The mirror image: /players/foo is the SPA's own routing and must
        # render, not 404.
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/players/foo")

            assert response.status == 200
            assert "smite" in await response.text()
        finally:
            await client.close()

    async def test_a_real_asset_is_served_as_itself(self, site):
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/app.js")

            assert response.status == 200
            assert "console.log" in await response.text()
        finally:
            await client.close()

    async def test_traversal_out_of_the_bundle_is_refused(self, site):
        snapshots, dist, write = site
        write(snapshot.STATUS_FILE, {"generated_at": time.time()})
        secret = dist.parent / "snapshots" / snapshot.STATUS_FILE
        assert secret.exists()

        client = await client_for(snapshots, dist)
        try:
            response = await client.get(f"/../snapshots/{snapshot.STATUS_FILE}")

            # Either normalised away by the client or refused here; what must
            # not happen is the file's contents coming back.
            assert "generated_at" not in await response.text()
        finally:
            await client.close()


class TestCaching:
    """The shell must revalidate; fingerprinted assets need not.

    A response carrying only `Last-Modified` is heuristically cacheable, and
    browsers apply that to error statuses too — Firefox held a 404 from before
    this host's tunnel route existed and kept serving it long after the site was
    up. Saying it out loud is the fix.
    """

    async def test_the_shell_must_revalidate(self, site):
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            for path in ("/", "/players/foo"):
                response = await client.get(path)
                header = response.headers["Cache-Control"]
                # The shell names hashed asset filenames. A cached copy from
                # before a deploy points at files that no longer exist.
                assert "no-cache" in header, path
        finally:
            await client.close()

    async def test_hashed_assets_are_immutable(self, site):
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/app.js")

            assert "immutable" in response.headers["Cache-Control"]
        finally:
            await client.close()

    async def test_a_missing_page_is_not_cacheable_forever(self, site):
        # The shell is what an unknown path returns, so it inherits no-cache —
        # which is exactly what stops a stale 404 outliving the fix.
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/no/such/page")

            assert "no-cache" in response.headers["Cache-Control"]
        finally:
            await client.close()


class TestReadiness:
    async def test_ready_without_any_snapshot(self, site):
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/healthz")

            # No snapshot at all, and the pod is still ready. An SMB blip must
            # not empty the Service.
            assert response.status == 200
            assert (await response.json())["ready"] is True
        finally:
            await client.close()

    async def test_not_ready_without_a_bundle(self, tmp_path):
        snapshots = tmp_path / "snapshots"
        snapshots.mkdir()
        client = await client_for(snapshots, tmp_path / "no-dist")
        try:
            response = await client.get("/healthz")

            assert response.status == 503
        finally:
            await client.close()


class TestSnapshotRoutes:
    async def test_missing_status_is_503(self, site):
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/api/status")

            assert response.status == 503
            assert "error" in await response.json()
        finally:
            await client.close()

    async def test_status_is_served_with_its_age(self, site):
        snapshots, dist, write = site
        write(
            snapshot.STATUS_FILE,
            {"version": 1, "generated_at": time.time() - 90, "games": {}},
        )
        client = await client_for(snapshots, dist)
        try:
            body = await (await client.get("/api/status")).json()

            assert body["version"] == 1
            # Staleness is computed at read time — the file cannot know when it
            # will be looked at, and "fine" vs "nothing since Tuesday" is the
            # distinction the page exists to make.
            assert 85 < body["stale_seconds"] < 200
        finally:
            await client.close()

    async def test_a_rewritten_snapshot_is_picked_up(self, site):
        snapshots, dist, write = site
        write(snapshot.STATUS_FILE, {"generated_at": time.time(), "games": {"a": 1}})
        client = await client_for(snapshots, dist)
        try:
            first = await (await client.get("/api/status")).json()
            assert first["games"] == {"a": 1}

            # The cache is keyed on mtime, so a CronJob write must be visible
            # without restarting the pod.
            os.utime(
                snapshots / snapshot.STATUS_FILE,
                (time.time() + 10, time.time() + 10),
            )
            write(snapshot.STATUS_FILE, {"generated_at": time.time(), "games": {"b": 2}})

            second = await (await client.get("/api/status")).json()
            assert second["games"] == {"b": 2}
        finally:
            await client.close()

    async def test_corrupt_snapshot_is_503_not_a_500(self, site):
        snapshots, dist, _ = site
        (snapshots / snapshot.STATUS_FILE).write_text("{half a fi", encoding="utf-8")
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/api/status")

            assert response.status == 503
        finally:
            await client.close()

    async def test_api_responses_are_edge_cacheable(self, site):
        snapshots, dist, write = site
        write(snapshot.STATUS_FILE, {"generated_at": time.time()})
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/api/status")

            # Cloudflare fronts this. The max-age must stay under the snapshot
            # cadence, or the edge serves an answer older than its source.
            assert f"max-age={serve.API_MAX_AGE}" in response.headers["Cache-Control"]
            assert serve.API_MAX_AGE <= 15 * 60
        finally:
            await client.close()


class TestPlayers:
    async def test_one_player_by_name_case_insensitively(self, site):
        snapshots, dist, write = site
        write(
            snapshot.PLAYERS_FILE,
            {
                "generated_at": time.time(),
                "players": [{"name": "zachjak", "found": True}],
            },
        )
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/api/players/ZachJak")

            assert response.status == 200
            assert (await response.json())["name"] == "zachjak"
        finally:
            await client.close()

    async def test_someone_not_on_the_roster_is_404(self, site):
        snapshots, dist, write = site
        write(snapshot.PLAYERS_FILE, {"generated_at": time.time(), "players": []})
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/api/players/nobody")

            assert response.status == 404
        finally:
            await client.close()

    async def test_the_two_snapshots_are_independent(self, site):
        # The players file being absent must not take the liveness page with
        # it; they are written by different jobs on different schedules.
        snapshots, dist, write = site
        write(snapshot.STATUS_FILE, {"generated_at": time.time(), "games": {}})
        client = await client_for(snapshots, dist)
        try:
            assert (await client.get("/api/status")).status == 200
            assert (await client.get("/api/players")).status == 503
        finally:
            await client.close()


class TestStatsEndpoint:
    async def test_missing_stats_is_503(self, site):
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            assert (await client.get("/api/stats")).status == 503
        finally:
            await client.close()

    async def test_three_snapshots_are_independent(self, site):
        # Three jobs, three cadences, three files. None may take the others down.
        snapshots, dist, write = site
        write(snapshot.STATS_FILE, {"generated_at": time.time(), "games": {}})
        client = await client_for(snapshots, dist)
        try:
            assert (await client.get("/api/stats")).status == 200
            assert (await client.get("/api/status")).status == 503
            assert (await client.get("/api/players")).status == 503
        finally:
            await client.close()

    async def test_stats_is_served_with_its_age(self, site):
        snapshots, dist, write = site
        write(
            snapshot.STATS_FILE,
            {"generated_at": time.time() - 120, "games": {"smite": {"built": True}}},
        )
        client = await client_for(snapshots, dist)
        try:
            body = await (await client.get("/api/stats")).json()

            assert body["games"]["smite"]["built"] is True
            assert 110 < body["stale_seconds"] < 300
        finally:
            await client.close()


class TestMeta:
    async def test_reports_both_ages_without_either_file(self, site):
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            body = await (await client.get("/api/meta")).json()

            assert body["status_age_seconds"] is None
            assert body["players_age_seconds"] is None
            assert body["snapshot_dir"] == str(snapshots)
        finally:
            await client.close()
