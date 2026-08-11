"""The Smite 2 counterpart to `SmiteProvider`.

Same surface — `gods`, `items`, `build_stats`, name resolution — over an
entirely different substrate. There is no Hi-Rez key, no session, no signed
request; there is a wiki, read once and cached.

Cache invalidation cannot use a patch endpoint, because the wiki has none, so it
hashes the revision ids of everything ingested: `Data:Gods.json` plus every god
and item article. That costs about nine `rvprop=ids` requests, catches a single
item's cooldown being edited, and is the reason the response cache below can be
trusted indefinitely rather than expiring on a timer.

`Data:PatchLogs.json` looked like the obvious marker and is not usable: it
trailed `Data:Gods.json` by several thousand revisions when checked, so keying
on it would miss every edit made between patch notes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Dict, Optional

import paths
from game import Game
from god import God
from item import Item
from smite2 import gods as gods_module
from smite2 import items as items_module
from smite2.clearance import ClearanceManager, ClearanceStore
from smite2.ids import NameIndex
from smite2.players import PlayerLookups
from smite2 import voicelines
from smite2.tracker_client import RateLimiter, TrackerClient
from smite2.wiki_client import WikiClient

VERSION_FILE = "version"
CACHE_FILE = "wiki_cache.json"

# On the shared corpus volume, not the bot's private one, so the collector and
# the bot use the same cookie.
CLEARANCE_FILE = "clearance.json"

# How often to re-check the wiki while running. The static data changes on
# patch days, so this is about noticing one without a restart rather than
# staying current to the minute.
REFRESH_SECONDS = 6 * 60 * 60


class Smite2Provider:
    """Smite 2 gods and items, read from wiki.smite2.com."""

    game: Game = Game.SMITE_2

    def __init__(self, silent: bool = False, user_agent: Optional[str] = None):
        self.gods: Dict[int, God] = {}
        self.items: Dict[int, Item] = {}
        self.build_stats = None
        # No live match frame: there is no time-enumerable source to refresh
        # from, so /build reads the aggregate or nothing.
        self.player_matches = None

        self.__silent = silent
        self.__user_agent = user_agent
        self.__god_index = NameIndex()
        self.__item_index = NameIndex()
        self.__skins: Dict[int, list] = {}
        self.__refresh_running = False
        self.__clearance = None
        self.__limiter: Optional[RateLimiter] = None

        # Per-player reads go to tracker.gg rather than to the corpus. The
        # corpus is a snowball sample and cannot answer "how has this player
        # done on Anubis"; asked per player, the same source answers exactly.
        self.players = PlayerLookups(self.__tracker_client, silent=silent)

    def __log(self, message: str) -> None:
        if not self.__silent:
            print(f"smite2: {message}", flush=True)

    @property
    def version_path(self) -> str:
        return paths.game_data_file(self.game, VERSION_FILE)

    @property
    def cache_path(self) -> str:
        return paths.game_data_file(self.game, CACHE_FILE)

    def __client(self) -> WikiClient:
        kwargs = {"silent": self.__silent, "cache_path": self.cache_path}
        if self.__user_agent:
            kwargs["user_agent"] = self.__user_agent
        return WikiClient(**kwargs)

    def __tracker_client(self) -> TrackerClient:
        """A tracker.gg client sharing the deployment's one clearance cookie.

        The store lives on the corpus volume so the nightly collector and the
        bot see each other's cookie: a measured 6.7-hour lifetime means a crawl
        usually leaves a valid one behind, and the bot then never has to mint.

        The *limiter* is shared for a related reason. A client is built per
        command, and a fresh limiter has never issued a request, so its first
        `wait()` returns immediately — which meant the bot had no effective
        pacing at all and a command reading many pages could spend a hundred
        requests as fast as the network allowed. Holding it here makes the gap
        a property of the address rather than of the command.
        """
        if self.__limiter is None:
            self.__limiter = RateLimiter()
        if self.__clearance is None:
            self.__clearance = ClearanceManager(
                ClearanceStore(
                    os.path.join(
                        paths.game_model_dir(self.game), CLEARANCE_FILE
                    )
                ),
                silent=self.__silent,
            )
        return TrackerClient(
            self.__clearance, silent=self.__silent, limiter=self.__limiter
        )

    async def create(self) -> None:
        """Load the catalogue, from cache when the wiki has not moved."""
        try:
            await self.__load()
        except Exception as error:  # noqa: BLE001
            # A wiki outage must not stop the bot: Smite 1 still works, and the
            # registry only offers a game whose provider has content.
            self.__log(f"could not load static data: {error}")

    async def __load(self) -> None:
        async with self.__client() as client:
            revision = await self.__revision_hash(client)
            if revision and revision != self.__stored_revision():
                self.__log("wiki has changed since the last load; refetching")
                client.clear_cache()

            self.gods, self.__god_index, self.__skins = await gods_module.load(
                client, silent=self.__silent
            )
            self.items, self.__item_index = await items_module.load(
                client, silent=self.__silent
            )
            client.flush_cache()

        if revision:
            self.__store_revision(revision)
        self.__log(f"loaded {len(self.gods)} gods and {len(self.items)} items")

    async def __revision_hash(self, client: WikiClient) -> str:
        """A fingerprint of everything ingested, from revision ids alone.

        Deliberately not cached — this is the check that decides whether the
        cache is stale, so reading it from the cache would make it always agree
        with itself.
        """
        try:
            god_rows = await client.bucket(gods_module.GODS_BUCKET)
            item_rows = await client.bucket(items_module.ITEMS_BUCKET)
            titles = [gods_module.GODS_DATA_PAGE]
            titles += [str(r["page_name"]) for r in god_rows if r.get("page_name")]
            titles += [str(r["page_name"]) for r in item_rows if r.get("page_name")]
            pages = await client.query_pages(titles, content=False)
        except Exception as error:  # noqa: BLE001
            self.__log(f"could not read revisions: {error}")
            return ""

        digest = hashlib.sha1()
        for title in sorted(pages):
            digest.update(f"{title}:{pages[title].get('revid')}".encode())
        return digest.hexdigest()

    def __stored_revision(self) -> str:
        try:
            with open(self.version_path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def __store_revision(self, revision: str) -> None:
        try:
            os.makedirs(os.path.dirname(self.version_path) or ".", exist_ok=True)
            with open(self.version_path, "w", encoding="utf-8") as handle:
                handle.write(revision)
        except OSError as error:
            self.__log(f"could not record revision: {error}")

    async def load_dataframe(self) -> None:
        """Pick up the aggregate, and keep checking for one.

        The Smite 1 provider walks a corpus into memory when no aggregate
        exists. There is no equivalent here — tracker.gg cannot be enumerated by
        time, so the corpus is a sample and only the aggregate is worth serving.
        """
        self.load_build_stats()
        if not self.__refresh_running:
            self.__refresh_running = True
            asyncio.get_running_loop().create_task(self.__refresh_loop())

    async def __refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            try:
                await self.__load()
                self.load_build_stats()
            except Exception as error:  # noqa: BLE001
                self.__log(f"refresh failed, keeping what we have: {error}")

    def load_build_stats(self) -> bool:
        from build_ranker import BuildStats  # noqa: PLC0415  (SmiteBot-side)

        stats = BuildStats.load(paths.game_model_dir(self.game))
        if stats is not None:
            self.build_stats = stats
        return self.build_stats is not None

    def god_by_name(self, name: str) -> Optional[God]:
        """A god from however a user typed their name.

        Goes through the same index that joins tracker.gg's identifiers, so
        "Chang'e", "change", "The Morrigan", "morrigan" and the engine's own
        `Gods.CuChulainn` all land on the right god.
        """
        canonical = self.__god_index.get(name)
        if canonical is None:
            return None
        for god in self.gods.values():
            if god.name == canonical:
                return god
        return None

    def god_id_from_name(self, name: str) -> Optional[int]:
        god = self.god_by_name(name)
        return None if god is None else god.id

    def random_god_id(self) -> Optional[int]:
        import random  # noqa: PLC0415

        return random.choice(list(self.gods.keys())) if self.gods else None

    def item_by_name(self, name: str) -> Optional[Item]:
        canonical = self.__item_index.get(name)
        if canonical is None:
            return None
        for item in self.items.values():
            if item.name == canonical:
                return item
        return None

    # --- Hi-Rez surface the cogs still reach for -------------------------
    #
    # These exist on SmiteProvider because they are Hi-Rez routes. Smite 2 has
    # no counterpart on this source, so they answer emptily rather than raising
    # an AttributeError three layers into a command.

    async def get_god_skins(self, god_id) -> list:
        """This god's skins, already parsed from the wiki.

        Async and returning raw-ish objects to match the Hi-Rez route the
        Smite-le rounds call, so neither the game nor trivia has to know which
        provider it is holding.
        """
        return list(self.__skins.get(god_id, []))

    async def get_god_voicelines(self, god_id) -> list:
        """This god's voice lines, or an empty list if the wiki has no page.

        Fetched on demand rather than at load: 55 of 88 gods have a page, and a
        game uses one line from one of them. Both requests go through the disk
        cache, so a repeat within its lifetime costs nothing.
        """
        god = self.gods.get(god_id)
        if god is None:
            return []
        async with self.__client() as client:
            return await voicelines.load(client, god.name)

    async def get_god_leaderboard(self, _god_id, _queue) -> list:
        return []

    async def get_match_history(self, _player_id) -> list:
        return []
