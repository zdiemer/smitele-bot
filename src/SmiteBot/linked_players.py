"""Which game account a Discord user plays on.

Without this, every command that needs a player makes the user type one, and a
build cannot look at the lobby they are actually in. `roster.py` already answers
this for fourteen people, by being a dict in source — which works, and does not
scale past the people who can open a pull request.

So this is the same answer, stored per user, with the roster kept underneath it
as a fallback rather than replaced. Anyone already in `roster.py` keeps working
without linking anything; anyone else runs `/link` once.

A file on the bot's own `/data` PVC, following `guild_settings.py` exactly: read
once at startup, served from memory, written through a temporary file so a crash
mid-write cannot truncate everyone's link. There is no database here and adding
one to remember a handle per user would be absurd.

One thing deliberately not done: this holds Discord ids, so it stays out of
anything the web pods read. `roster.py` says why — nothing outside Discord
should ever publish a Discord id — and that constraint does not weaken because
the mapping moved from source to disk.
"""

from __future__ import annotations

import json
import os
from json import JSONDecodeError
from typing import Dict, Optional

import paths
import roster
from game import Game

LINKS_FILE = "linked_players.json"


class LinkedPlayers:
    """Discord user -> the handle they play under, per game.

    Per game rather than one handle, because the two do not share an identity:
    Smite 1 has a global player name, Smite 2 has a platform and a handle, and
    `roster.py` already keeps two maps for that reason rather than trying to
    transform one into the other.
    """

    def __init__(self, path: Optional[str] = None):
        self.__path = path or paths.data_file(LINKS_FILE)
        self.__links: Dict[str, Dict[str, str]] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.__path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, JSONDecodeError):
            # No file yet, or one written by a crashed process. Either way the
            # right behaviour is everyone falling back to the roster, not a
            # dead bot.
            self.__links = {}
            return
        self.__links = loaded if isinstance(loaded, dict) else {}

    def __save(self) -> None:
        partial = f"{self.__path}.partial"
        try:
            os.makedirs(os.path.dirname(self.__path) or ".", exist_ok=True)
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump(self.__links, handle)
            os.replace(partial, self.__path)
        except OSError as error:
            print(f"linked_players: could not save: {error}", flush=True)

    def handle_for(self, user_id: Optional[int], game: Game) -> Optional[str]:
        """The handle to look this user up by, or None if we do not know one.

        Explicit link first, then the roster. That order matters: someone in
        the roster who links a different account meant the one they typed.
        """
        if user_id is None:
            return None
        linked = self.__links.get(str(user_id), {}).get(game.value)
        if linked:
            return linked
        return roster.for_game(game).get(user_id)

    def link(self, user_id: int, game: Game, handle: str) -> None:
        self.__links.setdefault(str(user_id), {})[game.value] = handle.strip()
        self.__save()

    def unlink(self, user_id: int, game: Optional[Game] = None) -> bool:
        """Forget one game's handle, or every game's. True if anything went.

        Unlinking has to be possible without naming a game: someone asking to
        be forgotten means all of it, and making them run the command twice to
        achieve that is the wrong answer to that request.
        """
        key = str(user_id)
        if key not in self.__links:
            return False
        if game is None:
            del self.__links[key]
            self.__save()
            return True
        if self.__links[key].pop(game.value, None) is None:
            return False
        if not self.__links[key]:
            del self.__links[key]
        self.__save()
        return True

    def is_linked(self, user_id: Optional[int], game: Game) -> bool:
        """Whether *this user* chose a handle, ignoring the roster.

        `/unlink` needs to tell "you never linked" from "you are in the
        roster", because the second is not something unlinking can undo.
        """
        if user_id is None:
            return False
        return bool(self.__links.get(str(user_id), {}).get(game.value))
