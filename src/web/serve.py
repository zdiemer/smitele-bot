#!/usr/bin/env python3
"""smite.diemer.codes — the public read side.

Serves two things: the SPA bundle, and the JSON `snapshot.py` last wrote. That
is the entire job. There is no Hi-Rez client here, no tracker.gg client, no
credentials in the environment and no third-party socket — which is the point,
because this process is the one anonymous traffic can reach and both upstreams
are metered. If a route here ever needs to *fetch* something, that is a signal
the fetching belongs in the CronJob instead.

Deliberately its own Deployment rather than more routes on the bot's
`status_server.py`. The bot is one replica by necessity (Discord allows a single
gateway connection per token), rolls with `Recreate`, holds a ~2.4Gi aggregate
under a 5Gi limit, and `upgrade.sh` refuses to restart it while a game is in
progress. A public web page should not inherit any of that, and nothing here
needs it: the only state is a file on a read-only mount, so this scales
horizontally and rolls without a deploy guard.

    SMITELE_WEB_SNAPSHOT_DIR=/matchdata/web python src/web/serve.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import snapshot as snapshot_module  # noqa: E402

DEFAULT_PORT: int = 8080

# The built SPA. Baked into the image next to this file by Dockerfile.web;
# overridable so `npm run dev` can proxy here without a build.
DIST_ENV = "SMITELE_WEB_DIST"

# Cloudflare fronts this and will honour it, so a link that gets shared is
# absorbed at the edge rather than turning into a stat() storm on an SMB mount.
# Kept under the fifteen-minute snapshot cadence: a cached answer should never
# be older than the thing it is caching.
API_MAX_AGE = 60

# The shell names fingerprinted asset filenames, so a stale one points at files
# that no longer exist. `no-cache` still lets the browser keep a copy — it just
# has to revalidate before using it.
SHELL_CACHE_CONTROL = "no-cache, must-revalidate"

# Vite puts a content hash in every asset filename, so these bytes are immutable
# by construction.
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"

# The preview card. Matched to the snapshot cadence — a chat client that cached
# it for a day would show a stand-down that lifted hours ago.
OG_MAX_AGE = 900


def dist_dir() -> str:
    return os.environ.get(DIST_ENV) or os.path.join(HERE, "dist")


class Snapshots:
    """The snapshot files, re-read only when they actually change.

    A stat() per request and a parse per *write*. The alternative — parsing on
    every request — turns a 15-minute-old document into per-request JSON work
    on a network mount, and caching without the stat() means a pod serves
    yesterday until something restarts it.

    Nothing here coordinates with the writer, and nothing needs to: the writer
    renames into place, so a reader either sees the whole old file or the whole
    new one.
    """

    def __init__(self, directory: str):
        self.directory = directory
        self.__cache: Dict[str, Tuple[float, Any]] = {}

    def load(self, name: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(self.directory, name)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return None

        cached = self.__cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1]

        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"serve: could not read {path}: {error}", flush=True)
            return None

        self.__cache[name] = (mtime, document)
        return document


def _aged(document: Dict[str, Any]) -> Dict[str, Any]:
    """The document plus how old it is.

    Computed at read time rather than stored, because staleness is a fact about
    now and the file cannot know when it will be looked at. The page needs it
    to tell "the crawl is fine" from "nothing has refreshed this since Tuesday",
    which look identical otherwise.
    """
    out = dict(document)
    generated = document.get("generated_at")
    out["stale_seconds"] = (
        max(time.time() - float(generated), 0.0) if generated else None
    )
    return out


def _json(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": f"public, max-age={API_MAX_AGE}"},
    )


def _unavailable(what: str) -> web.Response:
    # 503, not 404 and not an empty 200. The snapshot being absent means the
    # CronJob has not run yet or cannot write, which is a server-side problem
    # the page should say out loud rather than render as "no data".
    return _json(
        {
            "error": f"no {what} snapshot yet",
            "detail": "the snapshot job has not written one, or cannot reach "
            "the share it writes to",
        },
        status=503,
    )


def build_app(directory: Optional[str] = None, dist: Optional[str] = None):
    snapshots = Snapshots(directory or snapshot_module.snapshot_dir())
    static_root = dist or dist_dir()
    index = os.path.join(static_root, "index.html")

    async def healthz(_request: web.Request) -> web.Response:
        """Readiness for this pod, and nothing else.

        Deliberately independent of snapshot freshness. Tying readiness to the
        data would mean an SMB blip cycles every replica out of the Service and
        takes the site down entirely, when what it should do is show a stale
        badge on an otherwise working page.
        """
        ready = os.path.exists(index)
        return web.json_response(
            {"ready": ready, "dist": static_root},
            status=200 if ready else 503,
        )

    async def status(_request: web.Request) -> web.Response:
        document = snapshots.load(snapshot_module.STATUS_FILE)
        if document is None:
            return _unavailable("status")
        return _json(_aged(document))

    async def players(_request: web.Request) -> web.Response:
        document = snapshots.load(snapshot_module.PLAYERS_FILE)
        if document is None:
            return _unavailable("players")
        return _json(_aged(document))

    async def player(request: web.Request) -> web.Response:
        document = snapshots.load(snapshot_module.PLAYERS_FILE)
        if document is None:
            return _unavailable("players")

        wanted = request.match_info["name"].lower()
        for entry in document.get("players", []):
            if str(entry.get("name", "")).lower() == wanted:
                return _json(entry)
        return _json({"error": "no such player on the roster"}, status=404)

    async def stats(_request: web.Request) -> web.Response:
        document = snapshots.load(snapshot_module.STATS_FILE)
        if document is None:
            return _unavailable("stats")
        return _json(_aged(document))

    async def meta(_request: web.Request) -> web.Response:
        status_doc = snapshots.load(snapshot_module.STATUS_FILE)
        players_doc = snapshots.load(snapshot_module.PLAYERS_FILE)

        def age(document):
            if not document or not document.get("generated_at"):
                return None
            return max(time.time() - float(document["generated_at"]), 0.0)

        return _json(
            {
                "version": os.environ.get("SMITELE_WEB_VERSION", "dev"),
                "snapshot_dir": snapshots.directory,
                "status_age_seconds": age(status_doc),
                "players_age_seconds": age(players_doc),
            }
        )

    # Rendered card, keyed on the snapshots it was drawn from. `None` until the
    # first request asks for one — a crawler is the only thing that ever will,
    # and rendering at startup would cost every rolling pod a PNG nobody reads.
    card: Dict[str, Any] = {"key": None, "png": None}

    async def og_image(_request: web.Request) -> web.Response:
        """The link preview, drawn live rather than checked in.

        Keyed on both snapshot mtimes, so it re-renders when the data moves and
        not once more — a thousand crawler fetches cost one render. A static
        card would be a photograph of the numbers on the day it was exported,
        which for a site whose entire subject is freshness is worse than none.
        """
        status_doc = snapshots.load(snapshot_module.STATUS_FILE) or {}
        stats_doc = snapshots.load(snapshot_module.STATS_FILE) or {}
        key = (
            status_doc.get("generated_at"),
            stats_doc.get("generated_at"),
        )

        if card["key"] != key or card["png"] is None:
            try:
                import og  # noqa: PLC0415

                card["png"] = og.render(status_doc, stats_doc)
                card["key"] = key
            except Exception as error:  # noqa: BLE001
                # A preview that cannot be drawn must not take a page down with
                # it; the crawler simply gets no image.
                print(f"serve: could not render og.png: {error}", flush=True)
                return _json({"error": "preview unavailable"}, status=503)

        return web.Response(
            body=card["png"],
            content_type="image/png",
            headers={"Cache-Control": f"public, max-age={OG_MAX_AGE}"},
        )

    async def touch_icon(_request: web.Request) -> web.Response:
        """The iOS home-screen icon, drawn from the same polygon as the card.

        Rendered rather than checked in so there is one shape to change instead
        of a binary to remember to regenerate. It never varies, so it is drawn
        once per process and cached hard.
        """
        if card.get("icon") is None:
            try:
                import og  # noqa: PLC0415

                card["icon"] = og.icon(180)
            except Exception as error:  # noqa: BLE001
                print(f"serve: could not render icon: {error}", flush=True)
                return _json({"error": "icon unavailable"}, status=503)
        return web.Response(
            body=card["icon"],
            content_type="image/png",
            headers={"Cache-Control": ASSET_CACHE_CONTROL},
        )

    async def api_not_found(_request: web.Request) -> web.Response:
        # An unknown /api path must never fall through to the SPA shell. A
        # client asking for JSON and getting 200 text/html debugs badly.
        return _json({"error": "no such endpoint"}, status=404)

    async def spa(request: web.Request) -> web.StreamResponse:
        """A file from the bundle, or the shell so client routing can take over.

        The shell is served for any unmatched path *except* /api, which is
        handled above. Rejecting traversal explicitly rather than trusting the
        join: `dist` is a directory of public assets, but "public" is a fact
        about what is in it, not a licence to serve whatever is above it.
        """
        relative = request.match_info.get("path", "").lstrip("/")
        if relative:
            candidate = os.path.normpath(os.path.join(static_root, relative))
            if (
                os.path.commonpath([os.path.abspath(candidate), os.path.abspath(static_root)])
                == os.path.abspath(static_root)
                and os.path.isfile(candidate)
            ):
                # Vite fingerprints every asset filename, so a given URL's bytes
                # never change and a year is as good as forever.
                return web.FileResponse(
                    candidate, headers={"Cache-Control": ASSET_CACHE_CONTROL}
                )

        if not os.path.exists(index):
            return web.Response(text="no SPA bundle built", status=503)
        # The shell must revalidate on every load. It names the fingerprinted
        # assets, so a cached copy from before a deploy points at filenames that
        # no longer exist — a blank page nobody can fix from their end.
        #
        # It also has to be said out loud. A response carrying only
        # Last-Modified is *heuristically* cacheable, which browsers apply to
        # error statuses too: Firefox held on to a 404 from before this host's
        # tunnel route existed and kept serving it long after the site was up.
        return web.FileResponse(index, headers={"Cache-Control": SHELL_CACHE_CONTROL})

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/players", players)
    app.router.add_get("/api/players/{name}", player)
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/meta", meta)
    app.router.add_get("/og.png", og_image)
    app.router.add_get("/icon-180.png", touch_icon)
    app.router.add_route("*", "/api/{tail:.*}", api_not_found)
    app.router.add_get("/", spa)
    app.router.add_get("/{path:.*}", spa)
    return app


def main() -> int:
    port = int(os.environ.get("SMITELE_WEB_PORT", DEFAULT_PORT))
    app = build_app()
    print(
        f"smite.diemer.codes listening on :{port} · "
        f"snapshots {snapshot_module.snapshot_dir()} · bundle {dist_dir()}",
        flush=True,
    )
    web.run_app(app, host="0.0.0.0", port=port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
