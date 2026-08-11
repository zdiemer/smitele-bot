"""The trivia leaderboard, kept per guild.

`scores.json` was one flat `{user_id: score}` map, so a point won in one server
counted towards every server's `/scores`. Three communities shared a single
board and none of them could see its shape.

The split cannot be applied backwards — the flat file records who scored, not
where — so those totals become a seed instead. A guild with no board of its own
starts from a copy of them and diverges from there, which means splitting costs
nobody the standing they already had. The seed is kept, so a guild that first
plays trivia a year from now still starts from the same place.

Writes go to a temporary file and are renamed into place, following
`guild_settings`, because the bot and the web pods read this volume without
coordinating and a torn read is worse than a missing one.
"""

from __future__ import annotations

import json
import os
from json import JSONDecodeError
from typing import Dict, List, Optional, Tuple

import paths

SCORES_FILE = "scores.json"

# What a guild id is called in the file when there isn't one. Slash commands are
# guild-scoped, so this is only reachable if that ever changes.
NO_GUILD = "dm"


class TriviaScores:
    """Per-guild trivia totals, seeded from the leaderboard that preceded them."""

    def __init__(self, path: Optional[str] = None):
        self.__path = path or paths.data_file(SCORES_FILE)
        self.__guilds: Dict[str, Dict[str, int]] = {}
        self.__seed: Dict[str, int] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.__path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, JSONDecodeError):
            # No file yet, or one written by a crashed process. An empty board
            # is the right answer either way; a dead command is not.
            loaded = {}
        if not isinstance(loaded, dict):
            loaded = {}

        if "guilds" in loaded:
            self.__guilds = {
                str(guild): _totals(scores)
                for guild, scores in (loaded.get("guilds") or {}).items()
                if isinstance(scores, dict)
            }
            self.__seed = _totals(loaded.get("seed") or {})
            return

        # The flat pre-split file: every total in it, unattributable to a guild.
        self.__guilds = {}
        self.__seed = _totals(loaded)

    def __save(self) -> None:
        partial = f"{self.__path}.partial"
        try:
            os.makedirs(os.path.dirname(self.__path) or ".", exist_ok=True)
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump({"guilds": self.__guilds, "seed": self.__seed}, handle)
            os.replace(partial, self.__path)
        except OSError as error:
            print(f"trivia_scores: could not save: {error}", flush=True)

    def __bucket(self, guild_id: Optional[int]) -> Dict[str, int]:
        """This guild's totals, seeded on first use and never re-seeded."""
        key = str(guild_id) if guild_id is not None else NO_GUILD
        if key not in self.__guilds:
            self.__guilds[key] = dict(self.__seed)
        return self.__guilds[key]

    def board_for(self, guild_id: Optional[int]) -> List[Tuple[int, int]]:
        """`(user_id, score)` for one guild, highest first."""
        return sorted(
            ((int(user), score) for user, score in self.__bucket(guild_id).items()),
            key=lambda entry: entry[1],
            reverse=True,
        )

    def record(self, guild_id: Optional[int], round_scores: Dict[int, int]) -> None:
        """Add a finished round's answers to the guild that played it."""
        if not round_scores:
            return
        bucket = self.__bucket(guild_id)
        for user, points in round_scores.items():
            bucket[str(user)] = bucket.get(str(user), 0) + int(points)
        self.__save()


def _totals(raw: Dict) -> Dict[str, int]:
    """A `{user_id: score}` map with everything unreadable dropped.

    A hand-edited or half-written file should cost one person their score, not
    the whole leaderboard.
    """
    totals: Dict[str, int] = {}
    for user, score in (raw or {}).items():
        try:
            totals[str(int(user))] = int(score)
        except (TypeError, ValueError):
            continue
    return totals
