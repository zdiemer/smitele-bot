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

import io
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


class TestPreviewCard:
    """/og.png is rendered per snapshot, not per request and not at build time."""

    async def test_it_serves_a_png(self, site):
        snapshots, dist, write = site
        write(snapshot.STATUS_FILE, {"generated_at": time.time()})
        write(snapshot.STATS_FILE, {"generated_at": time.time(), "games": {}})
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/og.png")

            assert response.status == 200
            assert response.content_type == "image/png"
            assert (await response.read())[:8] == b"\x89PNG\r\n\x1a\n"
        finally:
            await client.close()

    async def test_it_renders_even_with_no_snapshots(self, site):
        # A crawler hitting a cold deploy must get a card, not a 503 that gets
        # cached as "this site has no preview".
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            assert (await client.get("/og.png")).status == 200
        finally:
            await client.close()

    async def test_it_is_cached_against_the_snapshot_not_the_request(self, site):
        snapshots, dist, write = site
        write(snapshot.STATUS_FILE, {"generated_at": 1000.0})
        write(snapshot.STATS_FILE, {"generated_at": 1000.0, "games": {}})
        client = await client_for(snapshots, dist)
        try:
            first = await (await client.get("/og.png")).read()
            second = await (await client.get("/og.png")).read()

            # Same snapshot, same bytes — the second request did no work.
            assert first == second
        finally:
            await client.close()

    async def test_its_cache_header_matches_the_snapshot_cadence(self, site):
        snapshots, dist, write = site
        write(snapshot.STATUS_FILE, {"generated_at": time.time()})
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/og.png")

            assert f"max-age={serve.OG_MAX_AGE}" in response.headers["Cache-Control"]
            # A chat client caching it for a day would show a stand-down that
            # lifted hours ago.
            assert serve.OG_MAX_AGE <= 3600
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


class TestHomeScreenIcons:
    """The four icons the manifest names, and the one rule Android imposes.

    These are rendered rather than checked in, so nothing else would notice if
    a size stopped being served — the symptom is a home-screen icon quietly
    becoming a screenshot crop on somebody else's phone.
    """

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/icon-180.png", 180),
            ("/icon-192.png", 192),
            ("/icon-512.png", 512),
            ("/icon-maskable-512.png", 512),
        ],
    )
    async def test_every_size_the_manifest_names_is_served(self, site, path, expected):
        Image = pytest.importorskip("PIL.Image")
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get(path)

            assert response.status == 200
            assert response.headers["Content-Type"] == "image/png"
            body = await response.read()
            assert Image.open(io.BytesIO(body)).size == (expected, expected)
        finally:
            await client.close()

    async def test_the_maskable_one_keeps_out_of_the_crop(self, site):
        """Android crops to a circle at 80% of the width.

        The uncropped mark runs its motion lines to within 6 of a 64-unit edge,
        so the check that matters is that the maskable variant's corners are
        bare tile — if the artwork reaches them, it is reaching past the circle
        too and the outer strokes are being sliced off on real phones.
        """
        Image = pytest.importorskip("PIL.Image")
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            body = await (await client.get("/icon-maskable-512.png")).read()
            image = Image.open(io.BytesIO(body)).convert("RGB")
            ink = image.getpixel((2, 2))

            # A band comfortably outside the safe circle, on all four sides.
            for x, y in [(20, 20), (491, 20), (20, 491), (491, 491),
                         (256, 12), (256, 499), (12, 256), (499, 256)]:
                assert image.getpixel((x, y)) == ink, f"artwork reaches ({x}, {y})"
        finally:
            await client.close()

    async def test_icons_are_not_cached_as_immutable(self, site):
        """They carry no content hash, unlike everything Vite emits.

        `immutable` here would mean a redesigned mark keeps showing the old one
        for a year, which is the bug this replaced.
        """
        snapshots, dist, _ = site
        client = await client_for(snapshots, dist)
        try:
            response = await client.get("/icon-192.png")

            assert "immutable" not in response.headers["Cache-Control"]
            assert "max-age=86400" in response.headers["Cache-Control"]
        finally:
            await client.close()
