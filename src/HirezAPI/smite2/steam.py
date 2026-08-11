"""Is this Steam player running Smite 2 right now?

tracker.gg's live snapshots refresh about every ten minutes, so for that long
after a match starts the bot has no way to tell "not in a match" apart from
"tracker.gg has not noticed yet". Steam's presence answers within seconds —
`GetPlayerSummaries` carries `gameid` whenever a public profile is in a game —
which is enough to turn a wrong-sounding flat no into "they're playing, the
lobby just isn't posted yet".

What this deliberately is not: an in-match detector. The Steam Web API exposes
no rich-presence strings (measured 2026-08-11, not assumed — the enhanced rich
presence lives only in the Steamworks client SDK), so running-the-game is the
whole resolution: menus, queue and match all look the same. It is also
Steam-only, needs the target's game details public, and needs a Web API key in
`SMITELE_STEAM_API_KEY` — absent any of those it answers "don't know" and the
caller says what it would have said anyway.
"""

from __future__ import annotations

import os
from typing import Optional

import aiohttp

SMITE2_STEAM_APPID = "2437170"

_SUMMARIES_URL = (
    "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
)


async def running_smite2(steam_id: str) -> Optional[bool]:
    """True/False if Steam will say, None where it cannot.

    None covers every reason there is no answer — no key configured, a handle
    that is not a Steam id, a private profile, a timeout — because they all
    call for the same fallback: answer from tracker.gg alone.
    """
    key = os.environ.get("SMITELE_STEAM_API_KEY", "")
    if not key or not steam_id.isdigit():
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _SUMMARIES_URL,
                params={"key": key, "steamids": steam_id},
                timeout=aiohttp.ClientTimeout(total=4),
            ) as response:
                if response.status != 200:
                    return None
                body = await response.json()
    except Exception:  # noqa: BLE001 — a presence probe must never break a command
        return None

    players = ((body or {}).get("response") or {}).get("players") or []
    if not players:
        return None
    # `gameid` is present only while the profile is visibly in a game; its
    # absence on a public profile genuinely means "not playing", but a private
    # profile also omits it, so absence is only trusted as far as "no".
    return players[0].get("gameid") == SMITE2_STEAM_APPID
