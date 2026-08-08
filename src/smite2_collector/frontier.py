"""Who to crawl tonight, and who not to bother with.

tracker.gg has no time enumeration — matches are reachable only per player — so
coverage is bought by choosing players well. Three things decide that.

**Yield.** A player who has not played since the last visit costs a full request
and returns nothing new. Tracking what each visit actually produced lets the
budget go to players who are active, which is the flaw the feasibility record
names in its own tables and does not fix.

**Rotation.** Querying the same roster every night re-finds the same matches.
Preferring the longest-unqueried spreads the crawl over the population instead.

**Premades.** Querying both halves of a duo is entirely wasted: they return the
same matches. The record estimated the effective independent players per match
at around six and could only infer it from overlap; segments carry `partyId`,
so it is measured — 7.16 of ten — and suppression is exact rather than
heuristic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

import match_storage

FRONTIER_FILE = "frontier.parquet"

# A player with this many barren visits in a row is set aside. Not deleted —
# people come back — but they stop competing for budget.
DEAD_AFTER_BARREN_VISITS = 3

COLUMNS = [
    "player_key",
    "platform",
    "handle",
    "first_seen",
    "last_queried",
    "visits",
    "barren_visits",
    "matches_yielded",
    "new_matches_yielded",
    "party_key",
]


@dataclass
class Player:
    platform: str
    handle: str
    first_seen: str = ""
    last_queried: str = ""
    visits: int = 0
    barren_visits: int = 0
    matches_yielded: int = 0
    new_matches_yielded: int = 0
    party_key: str = ""
    # Which of `select`'s three tiers put this player on tonight's roster.
    # Deliberately absent from COLUMNS: it describes one night's selection, not
    # a standing fact about a player, and persisting it would invite reading
    # last night's reason for tonight's visit.
    tier: str = ""

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.handle}"

    @property
    def dead(self) -> bool:
        return self.barren_visits >= DEAD_AFTER_BARREN_VISITS

    @property
    def yield_rate(self) -> float:
        """New matches per visit. Unvisited players are optimistic on purpose —
        they are the snowball edge and the only way coverage grows."""
        if self.visits == 0:
            return 1.0
        return self.new_matches_yielded / self.visits


class Frontier:
    """The roster, persisted between nightly runs."""

    def __init__(self, directory: str):
        self.path = os.path.join(directory, FRONTIER_FILE)
        self.players: Dict[str, Player] = {}
        # Set by `select`; how many players the last call held back as premades.
        self.suppressed = 0
        # Whether anything has changed since the last write. A run now
        # checkpoints every few minutes rather than only at the end, and without
        # this the roster parquet would be rewritten on every tick of an
        # eighteen-hour run whether or not a single player had moved.
        self.dirty = False
        self.load()

    def load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            frame = match_storage.read_frame(self.path)
        except Exception as error:  # noqa: BLE001
            print(f"frontier: could not read {self.path}: {error}", flush=True)
            return
        for row in frame.to_dict("records"):
            player = Player(
                platform=str(row.get("platform") or ""),
                handle=str(row.get("handle") or ""),
                first_seen=str(row.get("first_seen") or ""),
                last_queried=str(row.get("last_queried") or ""),
                visits=int(row.get("visits") or 0),
                barren_visits=int(row.get("barren_visits") or 0),
                matches_yielded=int(row.get("matches_yielded") or 0),
                new_matches_yielded=int(row.get("new_matches_yielded") or 0),
                party_key=str(row.get("party_key") or ""),
            )
            if player.handle:
                self.players[player.key] = player

    def save(self) -> None:
        if not self.dirty:
            return
        frame = pd.DataFrame(
            [
                {
                    "player_key": p.key,
                    "platform": p.platform,
                    "handle": p.handle,
                    "first_seen": p.first_seen,
                    "last_queried": p.last_queried,
                    "visits": p.visits,
                    "barren_visits": p.barren_visits,
                    "matches_yielded": p.matches_yielded,
                    "new_matches_yielded": p.new_matches_yielded,
                    "party_key": p.party_key,
                }
                for p in self.players.values()
            ],
            columns=COLUMNS,
        )
        partial = f"{self.path}.partial"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        frame.to_parquet(partial, compression="zstd", index=False)
        os.replace(partial, self.path)
        self.dirty = False

    def add(self, platform: str, handle: str, today: str) -> Player:
        key = f"{platform}:{handle}"
        player = self.players.get(key)
        if player is None:
            player = Player(platform=platform, handle=handle, first_seen=today)
            self.players[key] = player
            self.dirty = True
        return player

    def record_visit(
        self, player: Player, today: str, found: int, fresh: int
    ) -> None:
        player.last_queried = today
        player.visits += 1
        player.matches_yielded += found
        player.new_matches_yielded += fresh
        player.barren_visits = 0 if fresh else player.barren_visits + 1
        self.dirty = True

    def note_parties(self, parties: Dict[str, Set[str]]) -> None:
        """Record which party each player queues with.

        A party is stored as its smallest member's key, so every member of a
        duo agrees on the label without needing a second pass.
        """
        for members in parties.values():
            if len(members) < 2:
                continue
            label = min(members)
            for key in members:
                player = self.players.get(key)
                if player is not None and player.party_key != label:
                    player.party_key = label
                    self.dirty = True

    def select(self, budget: int, today: str, revisit: bool = False) -> List[Player]:
        """Tonight's roster.

        Order: never-visited players first, because they are the snowball edge
        and the only way the frontier grows; then everyone else by how long it
        has been weighted by what they have historically produced. One member
        per party, since the others return the same matches.

        Someone already read today is skipped, because a second read of their
        most recent page returns what the first one did. `revisit` lifts that,
        for a backfill: paging deeper reaches matches no same-day run has seen,
        so the guard would otherwise leave it with an empty roster.
        """
        if revisit:
            today = ""
        seen_parties: Set[str] = set()
        chosen: List[Player] = []
        # How many players a party membership kept off the roster. The
        # suppression is the single largest saving the frontier makes — 7.16
        # independent query-units per ten-player match — and until now it left
        # no trace, so there was no way to tell it working from it never firing.
        self.suppressed = 0

        def take(player: Player, tier: str) -> bool:
            if player.party_key:
                if player.party_key in seen_parties:
                    self.suppressed += 1
                    return False
                seen_parties.add(player.party_key)
            player.tier = tier
            chosen.append(player)
            return len(chosen) >= budget

        fresh = [p for p in self.players.values() if p.visits == 0]
        for player in sorted(fresh, key=lambda p: p.first_seen, reverse=True):
            if take(player, "fresh"):
                return chosen

        stale = [
            p
            for p in self.players.values()
            if p.visits > 0 and not p.dead and p.last_queried != today
        ]
        # Longest-unqueried first, then by what a visit has been worth. Sorting
        # on the date alone spends the budget on people who quit playing months
        # ago; sorting on yield alone never revisits anyone new.
        stale.sort(key=lambda p: (p.last_queried, -p.yield_rate))
        for player in stale:
            if take(player, "stale"):
                return chosen

        # Only if there is budget left over: the ones written off. They are the
        # cheapest way to notice somebody started playing again.
        revivable = [p for p in self.players.values() if p.dead and p.last_queried != today]
        revivable.sort(key=lambda p: p.last_queried)
        for player in revivable:
            if take(player, "revivable"):
                return chosen

        return chosen

    def counts(self) -> Dict[str, int]:
        """The roster's shape, as fields. What `summary` says in prose."""
        return {
            "total": len(self.players),
            "unvisited": sum(1 for p in self.players.values() if p.visits == 0),
            "dead": sum(1 for p in self.players.values() if p.dead),
            "partied": sum(1 for p in self.players.values() if p.party_key),
        }

    def summary(self) -> str:
        counts = self.counts()
        return (
            f"{counts['total']:,} players known · "
            f"{counts['unvisited']:,} never queried · "
            f"{counts['dead']:,} written off · "
            f"{counts['partied']:,} in a known party"
        )


def parties_in(match: dict) -> Dict[str, Set[str]]:
    """Party id to the player keys in it, for one match."""
    out: Dict[str, Set[str]] = {}
    for segment in match.get("segments") or []:
        if segment.get("type") != "overview":
            continue
        meta = segment.get("metadata") or {}
        attrs = segment.get("attributes") or {}
        party = meta.get("partyId")
        platform = attrs.get("platformSlug")
        handle = attrs.get("platformUserIdentifier")
        if not (party and platform and handle):
            continue
        out.setdefault(str(party), set()).add(f"{platform}:{handle}")
    return out


def players_in(match: dict) -> List[Tuple[str, str]]:
    out = []
    for segment in match.get("segments") or []:
        if segment.get("type") != "overview":
            continue
        attrs = segment.get("attributes") or {}
        platform = attrs.get("platformSlug")
        handle = attrs.get("platformUserIdentifier")
        if platform and handle:
            out.append((str(platform), str(handle)))
    return out
