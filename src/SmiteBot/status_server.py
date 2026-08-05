"""A small HTTP status endpoint, used as the deploy interlock.

Rolling the Deployment kills whatever games are in flight: a Smite-le round and
a trivia round both live entirely in memory in a coroutine, so a restart drops
them silently mid-question. `upgrade.sh` asks this endpoint whether anyone is
mid-game and refuses to roll if so.

It doubles as a readiness probe. The bot is "ready" once it has a Discord
session and its god/item caches are populated, which is a more useful signal
than the process merely being alive.
"""

from __future__ import annotations

from typing import Callable, Dict

from aiohttp import web

DEFAULT_PORT: int = 8080


class StatusServer:
    """Serves /healthz with the counts the deploy guard reads."""

    def __init__(
        self,
        session_counts: Dict[str, Callable[[], int]],
        ready: Callable[[], bool],
        port: int = DEFAULT_PORT,
    ):
        self.__session_counts = session_counts
        self.__ready = ready
        self.__port = port
        self.__runner: web.AppRunner = None

    async def __healthz(self, _request: web.Request) -> web.Response:
        counts = {name: source() for name, source in self.__session_counts.items()}
        ready = self.__ready()
        return web.json_response(
            {
                "ready": ready,
                "active_sessions": sum(counts.values()),
                "sessions": counts,
            },
            # Readiness probes key off the status code. Active games are a
            # perfectly healthy state, so they must not fail the probe — only
            # a bot that hasn't finished initialising is not ready.
            status=200 if ready else 503,
        )

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/healthz", self.__healthz)
        self.__runner = web.AppRunner(app)
        await self.__runner.setup()
        await web.TCPSite(self.__runner, "0.0.0.0", self.__port).start()
        print(f"Status endpoint listening on :{self.__port}/healthz", flush=True)
