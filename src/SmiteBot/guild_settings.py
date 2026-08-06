"""Per-guild preferences, currently just which game commands default to.

A file on the bot's own `/data` PVC, following the same pattern `scores.json`
already uses — there is no database here and adding one to remember a single
enum per server would be absurd.

Two differences from that precedent, both because this is read on the hot path
of every command rather than once at the end of a trivia round: the file is read
once at startup and served from memory, and writes go through a temporary file
so a crash mid-write cannot leave every guild's setting truncated.
"""

from __future__ import annotations

import json
import os
from json import JSONDecodeError
from typing import Dict, Optional

import paths
from game import DEFAULT_GAME, Game

SETTINGS_FILE = "guild_settings.json"


class GuildSettings:
    """Which game each guild has chosen, with the global default as a fallback."""

    def __init__(self, path: Optional[str] = None):
        self.__path = path or paths.data_file(SETTINGS_FILE)
        self.__settings: Dict[str, Dict[str, str]] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.__path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, JSONDecodeError):
            # No file yet, or one written by a crashed process. Either way the
            # right behaviour is every guild on the default, not a dead bot.
            self.__settings = {}
            return
        self.__settings = loaded if isinstance(loaded, dict) else {}

    def __save(self) -> None:
        partial = f"{self.__path}.partial"
        try:
            os.makedirs(os.path.dirname(self.__path) or ".", exist_ok=True)
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump(self.__settings, handle)
            os.replace(partial, self.__path)
        except OSError as error:
            print(f"guild_settings: could not save: {error}", flush=True)

    def game_for(self, guild_id: Optional[int]) -> Game:
        """The game a guild has chosen.

        `guild_id` is None in a DM, where there is no guild to have a preference
        and the global default is the only sensible answer.
        """
        if guild_id is None:
            return DEFAULT_GAME
        stored = self.__settings.get(str(guild_id), {}).get("game")
        if not stored:
            return DEFAULT_GAME
        try:
            return Game(stored)
        except ValueError:
            # A value written by a newer version, or hand-edited. Fall back
            # rather than failing the command.
            return DEFAULT_GAME

    def set_game(self, guild_id: int, game: Game) -> None:
        self.__settings.setdefault(str(guild_id), {})["game"] = game.value
        self.__save()

    def resolve(self, option: Optional[str], guild_id: Optional[int]) -> Game:
        """The game a single interaction is about.

        The precedence is what makes the `game:` option optional everywhere:
        an explicit choice, else the guild's default, else the global one.

        Note that Discord omits an option the user never touched — it does not
        send its default — so `option` being None is the normal case and means
        "they did not say", not "they chose Smite 1".
        """
        if option:
            try:
                return Game.from_display_name(option)
            except ValueError:
                pass
        return self.game_for(guild_id)
