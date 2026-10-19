"""Combine observed match records, never add overlapping aggregate totals.

SmiteSource and Prime are queried together; tracker fills a short history or
an outage. Precedence is stable (SmiteSource, Prime, tracker), independent of
request completion order. A duplicate keeps a whole scoreboard from one source;
only absent optional fields are filled. Shared game UUIDs are the join key.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

from smite2.players import CACHE_SECONDS, MatchSummary, PlayerLookups
from smite2 import smitesource, smiteprime

SOURCE_TIMEOUT = 12
FAILURE_BACKOFF = 60


def instant(value: str) -> float:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def summary(*, match_id, mode, timestamp, duration, won, god, god_name,
            role, kills, deaths, assists, rating, starting_rating, source):
    """Validate the join key and required facts before publishing a record."""
    try:
        match_id = str(UUID(str(match_id)))
        if not instant(timestamp) or not god_name or not isinstance(won, bool):
            raise ValueError("missing match facts")
        # Null K/D/A is unknown, not a zero. Reject incomplete ingestion rows.
        kills, deaths, assists = int(kills), int(deaths), int(assists)
        if min(kills, deaths, assists) < 0:
            raise ValueError("negative stats")
        rating = float(rating) if rating is not None else None
        delta = rating - float(starting_rating) if rating is not None and starting_rating is not None else None
        return MatchSummary(
            match_id=match_id, mode=mode, mode_name=smitesource._pretty_mode(mode),
            ranked=mode.startswith("ranked_"), timestamp=timestamp,
            duration=int(duration or 0), won=won, god=god or "", god_name=god_name,
            god_image=None, role=(role or "").lower(), kills=kills, deaths=deaths,
            assists=assists, skill_rating=rating, skill_rating_delta=delta,
            sources=(source,),
        )
    except (TypeError, ValueError) as error:
        raise smitesource.SmiteSourceUnavailable(f"{source}: invalid match row") from error


def merge_histories(histories, limit: int):
    merged = {}
    for history in histories:
        for row in history:
            # Tracker's legacy fixtures may use opaque IDs. Never join those
            # across providers; real upstream game IDs are UUIDs.
            try:
                key = str(UUID(row.match_id))
            except ValueError:
                key = (row.sources[0], row.match_id)
            if not row.match_id:
                continue
            previous = merged.get(key)
            if previous is None:
                merged[key] = replace(row)
                continue
            sources = tuple(dict.fromkeys(previous.sources + row.sources))
            updates = {"sources": sources}
            # Stats, win/loss and duration remain an atomic source snapshot.
            # A zero rating/delta is real; only None is a missing value.
            if previous.god_image is None:
                updates["god_image"] = row.god_image
            if previous.skill_rating is None:
                updates["skill_rating"] = row.skill_rating
                updates["skill_rating_delta"] = row.skill_rating_delta
            elif previous.skill_rating == row.skill_rating and previous.skill_rating_delta is None:
                updates["skill_rating_delta"] = row.skill_rating_delta
            merged[key] = replace(previous, **updates)
    return sorted(merged.values(), key=lambda row: (instant(row.timestamp), row.match_id), reverse=True)[:limit]


async def source_history(platform: str, handle: str, limit: int):
    async with smitesource.SmiteSourceClient() as client:
        uuid = handle if smitesource.looks_like_uuid(handle) else await client.player_uuid(platform, handle)
        if not uuid:
            raise smitesource.SmiteSourceUnavailable("no platform account")
        body = await client.rpc("matches.getPlayerMatches", {
            "playerUuid": uuid, "page": 1, "pageSize": min(limit, 25),
        })
    if not isinstance(body, dict) or not isinstance(body.get("matches"), list):
        raise smitesource.SmiteSourceUnavailable("matches: unexpected envelope")
    return [summary(
        match_id=row.get("hirezMatchId"), mode=row.get("queueType") or "",
        timestamp=row.get("startTimestamp"), duration=row.get("durationSeconds"),
        won=row.get("won"), god=row.get("godSlug"), god_name=row.get("godName"),
        role=row.get("playedRole") or row.get("assignedRole"),
        kills=row.get("kills"), deaths=row.get("deaths"), assists=row.get("assists"),
        rating=row.get("finishingSr"), starting_rating=row.get("startingSr"),
        source=smitesource.SOURCE_NAME,
    ) for row in body["matches"]][:limit]


class MultiSourcePlayerLookups(PlayerLookups):
    """Existing player surface with a composed history and identity bridge.

    Aggregate lookups remain on Tracker: observed alternatives do not share its
    lifetime/season scope. They cannot safely be spliced into its totals.
    """
    def __init__(self, client_factory, silent=False):
        super().__init__(client_factory, silent=silent)
        self._history_cache = {}
        self._identities = {}
        self._identity_pending = {}
        self._pending = {}
        self._retry_at = {}
        self._silent = silent

    async def _prime_identity(self, platform, handle):
        key = (platform, handle)
        entry = self._identities.get(key)
        if entry and time.monotonic() - entry[0] < 900:
            return entry[1]
        if key not in self._identity_pending:
            self._identity_pending[key] = asyncio.create_task(self._resolve_identity(key))
        return await asyncio.shield(self._identity_pending[key])

    async def _resolve_identity(self, key):
        try:
            async with smiteprime.SmitePrimeClient() as client:
                identity = await client.resolve(*key)
            self._identities[key] = (time.monotonic(), identity)
            return identity
        finally:
            self._identity_pending.pop(key, None)

    async def _prime_history(self, platform, handle, limit):
        identity = await self._prime_identity(platform, handle)
        async with smiteprime.SmitePrimeClient() as client:
            return await client.recent_matches(identity, limit)

    async def _source_history(self, platform, handle, limit):
        try:
            return await source_history(platform, handle, limit)
        except smitesource.SmiteSourceUnavailable:
            # Names are not platform IDs. Prime can resolve an exact name to
            # an account; only that account ID crosses into SmiteSource.
            if handle.isdigit() or smitesource.looks_like_uuid(handle):
                raise
            identity = await self._prime_identity(platform, handle)
            if str(identity["platform_id"]) == handle:
                raise
            return await source_history(platform, str(identity["platform_id"]), limit)

    async def _read(self, name, operation, platform, handle, limit):
        # Backoff is per source AND player: an ambiguous name must not disable
        # the service for everyone else. Successful empty histories are valid.
        key = (name, platform, handle)
        if self._retry_at.get(key, 0) > time.monotonic():
            return None
        try:
            return await asyncio.wait_for(operation(platform, handle, limit), SOURCE_TIMEOUT)
        except Exception as error:
            self._retry_at[key] = time.monotonic() + FAILURE_BACKOFF
            if not self._silent:
                print(f"smite2 history: {name} unavailable ({type(error).__name__})", flush=True)
            return None

    async def _combined(self, platform, handle, limit):
        histories = await asyncio.gather(
            self._read("SmiteSource", self._source_history, platform, handle, limit),
            self._read("SmitePrime", self._prime_history, platform, handle, limit),
        )
        rows = merge_histories([h for h in histories if h is not None], limit)
        if len(rows) < limit:
            tracker = await self._read("tracker.gg", super().recent_matches, platform, handle, limit)
            histories.append(tracker)
            rows = merge_histories([h for h in histories if h is not None], limit)
        if all(h is None for h in histories):
            raise smitesource.SmiteSourceUnavailable("all match history sources unavailable")
        return rows

    async def recent_matches(self, platform: str, handle: str, limit: int = 10):
        if limit <= 0:
            return []
        limit = min(limit, 25)  # These adapters intentionally read page one.
        key = (platform, handle, limit)
        entry = self._history_cache.get(key)
        if entry and time.monotonic() - entry[0] < CACHE_SECONDS:
            return entry[1]
        # One upstream lookup for simultaneous requests for the same player.
        if key not in self._pending:
            self._pending[key] = asyncio.create_task(self._load(key))
        return await asyncio.shield(self._pending[key])

    async def _load(self, key):
        try:
            rows = await self._combined(*key)
            self._history_cache[key] = (time.monotonic(), rows)
            return rows
        finally:
            self._pending.pop(key, None)
