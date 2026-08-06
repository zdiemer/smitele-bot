"""Which game a command, a corpus file or a provider is talking about.

Everything in this repo used to mean Smite 1 implicitly. Smite 2 is a different
game with different gods, items, queue ids and stat model, sourced from entirely
different places — tracker.gg for matches, wiki.smite2.com for static data — so
the two cannot share a provider, a corpus, or an aggregate. What they can share
is every line of code that does not care which it is holding, which is most of
it, and this enum is what lets that code stay single.

The enum value doubles as the on-disk path segment, so `paths.game_model_dir`
and friends never need a second mapping.
"""

from __future__ import annotations

from enum import Enum


class Game(Enum):
    SMITE = "smite"
    SMITE_2 = "smite2"

    @property
    def display_name(self) -> str:
        """What a Discord user sees in a `game:` choice."""
        return "Smite 1" if self is Game.SMITE else "Smite 2"

    @staticmethod
    def from_display_name(value: str) -> "Game":
        """Parse a `game:` option back to the enum.

        Discord choices are plain strings throughout this codebase, so every
        choice-backed option has to round-trip by hand. Doing it here rather than
        at each call site is the difference between one place to be wrong and
        one per command.
        """
        wanted = (value or "").strip().lower().replace(" ", "").replace("-", "")
        for game in Game:
            if wanted in (
                game.value,
                game.display_name.lower().replace(" ", ""),
            ):
                return game
        raise ValueError(f"not a game: {value!r}")


def queues_for(game: Game):
    """That game's queue enum.

    `QueueId` and `Smite2QueueId` deliberately share method names and
    signatures — `is_normal`, `is_ranked`, `display_name` — so callers that only
    classify a queue can take the enum as a parameter rather than branching.
    """
    if game is Game.SMITE:
        from HirezAPI import QueueId  # noqa: PLC0415

        return QueueId
    from smite2.queues import Smite2QueueId  # noqa: PLC0415

    return Smite2QueueId


def id_value(identifier) -> int:
    """The integer behind a god id, whichever game issued it.

    Smite 1's ids are `GodId` members and carry a `.value`; Smite 2's are plain
    integers derived from the god's slug, because they are already deterministic
    and an enum would only add a file to regenerate every time a god ships.
    """
    return identifier.value if hasattr(identifier, "value") else int(identifier)


# THE ONE PLACE THE DEFAULT GAME IS DECIDED.
#
# Smite 1 while the Smite 2 corpus is too thin to rank builds from. Flipping
# this to Game.SMITE_2 changes what every command does when the user passes no
# `game:` and their guild has set no default — which is the point, and why it is
# a single constant rather than a literal scattered across the cogs.
DEFAULT_GAME: Game = Game.SMITE
