#!/usr/bin/env python3
"""Regenerate `roster.DISCORD_TO_SMITE2_UUID` from the Steam roster.

A RallyHere player uuid is a deterministic v5 hash of the platform identity, so
resolving a Steam id to one is a fixed, one-time answer worth baking into
`roster.py` rather than doing at runtime. This prints a ready-to-paste map;
rerun it whenever `DISCORD_TO_SMITE2` changes, and drop the output in over the
existing `DISCORD_TO_SMITE2_UUID`.

Needs a RallyHere capture (the same state file the bot uses) to do the lookup —
pass its path, or set ``SMITELE_RH_STATE``:

    python scripts/resolve_roster_uuids.py --state rh_capture.json

Read-only: one batched `/users/v1/player` lookup, nothing account-bound written.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# The bot's modules resolve off this path in the image; mirror it for a checkout.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "HirezAPI")
)

import roster  # noqa: E402
from smite2 import rallyhere as rh  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        default=os.environ.get(rh.STATE_ENV),
        help="RallyHere capture/state JSON (or set $SMITELE_RH_STATE)",
    )
    args = parser.parse_args()
    if not args.state:
        raise SystemExit("need a RallyHere capture: --state <path> or $SMITELE_RH_STATE")

    # discord id -> steam handle, for the Steam entries of the Smite 2 roster.
    steam = {}
    for discord_id, ident in roster.DISCORD_TO_SMITE2.items():
        platform, _, handle = ident.partition(":")
        if platform == "steam":
            steam[discord_id] = handle

    auth = rh.RallyHereAuth.load(args.state)
    async with rh.RallyHereClient(auth) as client:
        resolved = await client.uuids_by_steam(steam.values())

    print("DISCORD_TO_SMITE2_UUID: Dict[int, str] = {")
    missing = []
    for discord_id, steam_id in steam.items():
        uuid = resolved.get(steam_id)
        name = roster.DISCORD_TO_SMITE.get(discord_id, "?")
        if uuid:
            print(f'    {discord_id}: "{uuid}",  # {name}')
        else:
            missing.append((discord_id, name, steam_id))
    print("}")
    if missing:
        print("\n# NOT in the RallyHere directory (left out of the map above):")
        for discord_id, name, steam_id in missing:
            print(f"#   {discord_id}  {name}  steam:{steam_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
