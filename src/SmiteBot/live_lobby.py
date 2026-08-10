"""The lobby a player is in right now, in terms a build command can use.

Both games can answer this, by completely different routes, and the difference
is not worth exposing to anything downstream. What a build needs is the same
either way: which gods are on the other side, which are on this one, and what
mode is being played.

    Smite 1  `getplayerstatus` says whether they are in a match and gives the
             match id; `getmatchplayerdetails` gives ten rows bucketed by
             `taskForce`. No row says which lane anyone is in.
    Smite 2  `/matches/{platform}/{handle}/live` gives the match id *and* this
             player's own god and team; `/matches/{id}` gives the other nine.
             Teams are named `order` and `chaos`.

Smite 2 is the better of the two here, which is the reverse of what the code
assumed for a year. Because its first request identifies the requesting player,
allies and enemies fall out directly. Smite 1 has to find the player among ten
rows, and knows no lanes at all — so the direct lane opponent, which is what the
win-probability model actually keys on, is available for neither without
inference.

Everything here is best effort by construction. A lobby makes a build better; it
is never the reason a command fails. Every path returns None on anything
unexpected, and the callers are written to carry on without one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from game import Game

# A lookup is two network calls against someone else's service, on the hot path
# of a command that has three seconds to acknowledge. Past this, the build goes
# out without a matchup rather than late with one.
LOOKUP_TIMEOUT_SECONDS: float = 4.0


@dataclass(frozen=True)
class Lobby:
    """A match in progress, keyed the way the build code wants it.

    God ids rather than names, because that is what `team_context.read` and
    `BuildRecommender.recommend` both take, and resolving names at the edge
    means neither of them has to know which game produced this.
    """

    game: Game
    own_god_id: Optional[Any] = None
    allies: List[Any] = field(default_factory=list)
    enemies: List[Any] = field(default_factory=list)
    queue_id: Optional[int] = None
    mode_name: str = ""
    ranked: bool = False
    # Per-player context the model z-scores. Smite 1 only: tracker.gg's live
    # snapshot carries no account level or rating.
    skill: Dict[str, float] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return bool(self.enemies or self.allies)

    def describe(self) -> str:
        """One clause for the embed, or nothing when there is nothing to say."""
        if not self.known:
            return ""
        mode = self.mode_name or ("ranked" if self.ranked else "")
        where = f" in {mode}" if mode else ""
        return f"read from your live match{where}"


async def lookup(provider, player: str) -> Optional[Lobby]:
    """The lobby `player` is in, for whichever game `provider` speaks.

    `player` is a Smite 1 in-game name, or a Smite 2 `platform:handle`.
    """
    try:
        return await asyncio.wait_for(
            _lookup(provider, player), timeout=LOOKUP_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 — a missing matchup is not a failed build
        return None


async def _lookup(provider, player: str) -> Optional[Lobby]:
    if getattr(provider, "game", None) is Game.SMITE_2:
        return await _smite2(provider, player)
    return await _smite1(provider, player)


async def _smite2(provider, player: str) -> Optional[Lobby]:
    from smite2.players import parse_player  # noqa: PLC0415

    lookups = getattr(provider, "players", None)
    if lookups is None:
        return None

    platform, handle = parse_player(player)
    match = await lookups.live_match(platform, handle)
    if match is None:
        return None

    def ids(names) -> List[Any]:
        found = [provider.god_id_from_name(name) for name in names]
        return [god_id for god_id in found if god_id is not None]

    return Lobby(
        game=Game.SMITE_2,
        own_god_id=provider.god_id_from_name(match.own_god),
        allies=ids(match.allies),
        enemies=ids(match.enemies),
        queue_id=None,
        mode_name=match.mode_name,
        ranked=match.ranked,
    )


# Statuses that mean "there is a lobby to read". Anything else — god select
# included — has no roster yet, and asking for one returns nothing useful.
def _in_a_match(status) -> bool:
    from HirezAPI import StatusId  # noqa: PLC0415

    return status is not None and status.status == StatusId.IN_GAME and bool(
        status.match_id
    )


async def _smite1(provider, player_name: str) -> Optional[Lobby]:
    from player import PlayerId  # noqa: PLC0415

    # PC only, unlike `/live_match`'s resolution, which falls back to a console
    # search. That fallback costs several requests and prompts the user; this
    # runs unasked behind a build, so it takes the cheap path or none.
    player_ids = await provider.get_player_id_by_name(player_name)
    if not player_ids:
        return None
    identity = PlayerId.from_json(player_ids[0], provider)
    if identity.private:
        return None
    player = await identity.get_player()
    if player is None:
        return None

    status = await player.get_player_status()
    if not _in_a_match(status):
        return None

    rows = await provider.get_match_player_details(status.match_id)
    if not rows:
        return None

    # Which of the ten is the player who asked. Hi-Rez hides some names, so a
    # missed match means no lobby rather than a build aimed at the wrong team.
    wanted = (player_name or "").strip().lower()
    mine = next(
        (
            row
            for row in rows
            if str(row.get("playerName", "")).strip().lower() == wanted
        ),
        None,
    )
    if mine is None:
        return None

    own_team = int(mine.get("taskForce", 0))
    allies, enemies = [], []
    for row in rows:
        god_id = _god_id(provider, row)
        if god_id is None or row is mine:
            continue
        (allies if int(row.get("taskForce", 0)) == own_team else enemies).append(god_id)

    return Lobby(
        game=Game.SMITE,
        own_god_id=_god_id(provider, mine),
        allies=allies,
        enemies=enemies,
        queue_id=status.queue_id.value if status.queue_id is not None else None,
        mode_name=status.queue_id.display_name if status.queue_id is not None else "",
        ranked=_ranked(status.queue_id),
        skill=_skill(mine),
    )


def _god_id(provider, row: Dict[str, Any]):
    """A god id from a live row, preferring the id over the name.

    Nothing in this repo read `GodId` off this endpoint before, so the name is
    kept as the fallback: it is the field that has been exercised for years.
    """
    raw = row.get("GodId")
    try:
        if raw is not None and int(raw) != 0:
            from god_types import GodId  # noqa: PLC0415

            return GodId(int(raw))
    except (TypeError, ValueError):
        pass
    return provider.god_id_from_name(str(row.get("GodName", "")))


def _ranked(queue_id) -> bool:
    if queue_id is None:
        return False
    from HirezAPI import QueueId  # noqa: PLC0415

    return bool(QueueId.is_ranked(queue_id))


# The four the model z-scores, under the names it stored them by.
_SKILL_FIELDS = ("Account_Level", "Mastery_Level", "Rank_Stat_Conquest", "Conquest_Tier")


def _skill(row: Dict[str, Any]) -> Dict[str, float]:
    """The requesting player's own skill features, where the live row has them.

    The live payload names two of these differently from the corpus — `Tier`
    and `Rank_Stat` rather than the per-queue columns — so they are mapped here
    rather than at the point the model is called.
    """
    aliases = {"Rank_Stat_Conquest": "Rank_Stat", "Conquest_Tier": "Tier"}
    out: Dict[str, float] = {}
    for name in _SKILL_FIELDS:
        value = row.get(name, row.get(aliases.get(name, ""), None))
        try:
            if value is not None:
                out[name] = float(value)
        except (TypeError, ValueError):
            continue
    return out
