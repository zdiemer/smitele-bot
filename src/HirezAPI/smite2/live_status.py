"""What `/livematch` says when tracker.gg has no lobby for a Smite 2 player.

The decision — which of the fast signals to believe, and how to phrase the
answer — lives here rather than in the cog so it can be tested without pulling
in discord. The cog gathers the signals (a RallyHere :class:`PlayerStatus`, and
Steam's coarse "is the game running" boolean) and hands them here.

The ordering is deliberate, most precise first:

1. **RallyHere says in a match.** A live session that is not a party. This is
   the one that beats tracker.gg outright — RallyHere is fresh to the second, so
   it sees a match tracker's ~10-minute snapshot has not caught up to.
2. **RallyHere says online.** Present but not in a match — a menu or a lobby.
   `InLobby` in the presence state sharpens this to "sitting in a lobby".
3. **Steam says the game is running.** Coarser, and Steam-only, but it is the
   backstop for when no RallyHere session is configured or the handle is not on
   a platform RallyHere resolves.
4. **Nothing.** None of the above; the honest "not that I can see".

Every message notes that tracker's lobby may simply be lagging, since that is
true in all four cases and is the thing a user most needs to hear before
concluding a friend is not playing.
"""

from __future__ import annotations

from typing import Optional

from .rallyhere import PlayerStatus


def absence_message(
    player_name: str,
    status: Optional[PlayerStatus],
    steam_running: Optional[bool],
) -> str:
    """The `/livematch` answer for a player tracker.gg has no live lobby for.

    ``status`` is RallyHere's, or None when it could not answer. ``steam_running``
    is consulted only when RallyHere did not place the player in a match or
    online, so the caller may pass None whenever it chose not to ask Steam.
    """
    if status is not None and status.in_match:
        return (
            f"**{player_name}** is in a Smite 2 match right now, but "
            f"tracker.gg hasn't posted the lobby yet — its snapshot lags a "
            f"few minutes behind a match starting, so ask again shortly."
        )
    if status is not None and status.online:
        in_lobby = (status.state or "").lower().startswith("inlobby")
        where = "sitting in a Smite 2 lobby" if in_lobby else "in Smite 2"
        return (
            f"**{player_name}** is {where} right now, but not in a match "
            f"tracker.gg can see. If they just queued, its live status takes "
            f"a few minutes to catch up."
        )
    if steam_running:
        return (
            f"**{player_name}** is in Smite 2 right now, but tracker.gg hasn't "
            f"posted their lobby yet. Its live status lags several minutes "
            f"behind a match starting, so ask again shortly."
        )
    return (
        f"**{player_name}** isn't in a match that tracker.gg can see yet. Its "
        f"live status often lags several minutes behind the start of a match, "
        f"so it's worth retrying if you know they're in one."
    )


def needs_steam_fallback(status: Optional[PlayerStatus]) -> bool:
    """Whether Steam is worth asking, given what RallyHere already said.

    False once RallyHere has placed the player in a match or online — the answer
    is settled and a Steam request would only cost a round trip to be ignored.
    """
    return not (status is not None and (status.in_match or status.online))
