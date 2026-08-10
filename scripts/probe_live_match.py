#!/usr/bin/env python3
"""Reading a live Smite 2 lobby off tracker.gg.

`/live_match` refused Smite 2 on the grounds recorded in `player_stats.py` —
"tracker.gg's sessions route returns 'not implemented', and the profile carries
only a `liveMatch` boolean". Both halves of that are still true and the
conclusion drawn from them is not: the lobby is reachable, in two requests.

    GET /api/v2/smite2/standard/matches/{platform}/{handle}/live
        -> {} when the player is not in a match.
        -> one `overview` segment for *that player only* when they are, whose
           `attributes.id` is the live match id. This is the piece nothing else
           exposes: the profile has the boolean but no id, the match list holds
           only closed matches, and the profile page is an empty SPA shell.

    GET /api/v2/smite2/standard/matches/{id}
        -> the full lobby: twelve segments, ten of them players carrying
           `godName` and a team of `order` or `chaos`, with `isLive: true`,
           `state: "pending"`, `isSnapshot: true` and no `winningTeamId` while
           the match is still going.

Measured against a real live ranked Conquest match on 2026-08-10, and against a
second player who was not playing, which is the case that has to be cheap and
unambiguous — it is, an empty body.

    python scripts/probe_live_match.py --player steam:76561198018789884

Run this before assuming any of the above still holds. It is an undocumented
endpoint behind a WAF and the pacing warning on `probe_tracker.py` applies here
too: do not raise the interval to see what happens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "HirezAPI"
    ),
)

from smite2.clearance import ClearanceManager, ClearanceStore  # noqa: E402
from smite2.tracker_client import (  # noqa: E402
    GAME_SLUG,
    TrackerBlocked,
    TrackerClient,
    TrackerServerError,
)

LIVE_PATH = "/api/v2/{game}/standard/matches/{platform}/{handle}/live"
MATCH_PATH = "/api/v2/{game}/standard/matches/{match_id}"


async def live_match_id(
    client: TrackerClient, platform: str, handle: str
) -> Optional[str]:
    """The id of the match this player is in, or None if they are not in one."""
    body = await client.get_json(
        LIVE_PATH.format(game=GAME_SLUG, platform=platform, handle=handle)
    )
    data = (body or {}).get("data")
    if not data:
        return None
    return ((data.get("attributes") or {}).get("id")) or None


async def lobby(client: TrackerClient, match_id: str) -> Dict[str, Any]:
    body = await client.get_json(
        MATCH_PATH.format(game=GAME_SLUG, match_id=match_id)
    )
    return (body or {}).get("data") or {}


def players(match: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(team, god, handle) for every player in the lobby.

    Teams are named `order` and `chaos` rather than numbered, which is the one
    place this differs from the Smite 1 payload's `taskForce`.
    """
    found = []
    for segment in match.get("segments") or []:
        metadata = segment.get("metadata") or {}
        attributes = segment.get("attributes") or {}
        god = metadata.get("godName")
        if not god:
            continue
        platform_info = metadata.get("platformInfo")
        name = ""
        if isinstance(platform_info, dict):
            name = platform_info.get("platformUserHandle") or ""
        found.append(
            (str(attributes.get("teamId") or metadata.get("teamId") or ""), god, name)
        )
    return found


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player", default="steam:76561198018789884")
    parser.add_argument("--store", default="/matchdata/smite2/clearance.json")
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--dump", default=None)
    args = parser.parse_args()

    platform, _, handle = args.player.partition(":")
    manager = ClearanceManager(ClearanceStore(args.store))

    async with TrackerClient(manager, interval=args.interval) as client:
        try:
            match_id = await live_match_id(client, platform, handle)
        except (TrackerBlocked, TrackerServerError) as error:
            print(f"refused: {error}")
            return 1

        if match_id is None:
            print(f"{args.player} is not in a match.")
            return 0

        print(f"live match: {match_id}")
        match = await lobby(client, match_id)
        metadata = match.get("metadata") or {}
        attributes = match.get("attributes") or {}
        print(f"  mode:  {attributes.get('gamemode')}  ranked={metadata.get('isRanked')}")
        print(
            f"  state: {metadata.get('state')}  isLive={metadata.get('isLive')}  "
            f"winner={metadata.get('winningTeamId')}"
        )
        print(f"  {metadata.get('duration')}s in, snapshot {metadata.get('snapshotTimestamp')}")
        for team, god, name in players(match):
            print(f"    {team:<6} {god:<18} {name}")
        print(f"\n{client.requests} requests, {client.bytes / 1e6:.2f} MB")

        if args.dump:
            with open(args.dump, "w", encoding="utf-8") as out:
                json.dump(match, out, indent=2, default=str)
            print(f"wrote {args.dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
