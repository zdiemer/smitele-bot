"""Smite 2 game modes, in the shape the aggregate already understands.

`match_queue_id` is an integer column throughout the corpus, the aggregate and
the model, so Smite 2's modes need integers too. They are allocated well above
every Smite 1 queue id — the largest is 10210 — so a frame that somehow mixed
the two games would produce ids belonging to neither game's enum rather than
silently reading a Smite 2 Assault as a Smite 1 one.

The classifier statics mirror `QueueId`'s by name and signature. That is what
lets `build_aggregate`, `god_builder.validate` and the queue choice lists take a
game's queue type as a parameter instead of forking on it.

The vocabulary was observed, not assumed: these are the mode strings that came
back across 2,744 sampled matches, plus the ones the wiki's Game Modes page
lists that the sample did not happen to reach.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Optional

# Above every Smite 1 id, with room to grow.
_BASE = 2_100_000


class Smite2QueueId(Enum):
    UNKNOWN = _BASE

    CONQUEST = _BASE + 1
    RANKED_CONQUEST = _BASE + 2
    ARENA = _BASE + 3
    JOUST = _BASE + 4
    RANKED_JOUST = _BASE + 5
    DUEL = _BASE + 6
    RANKED_DUEL = _BASE + 7
    ASSAULT = _BASE + 8
    SLASH = _BASE + 9

    # Ten-minute bot-filled variants, new to Smite 2 and easy to miss.
    QUICKPLAY_ARENA = _BASE + 20
    QUICKPLAY_JOUST = _BASE + 21

    # Against bots.
    CONQUEST_BOTS = _BASE + 40
    ARENA_BOTS = _BASE + 41
    JOUST_BOTS = _BASE + 42
    ASSAULT_BOTS = _BASE + 43

    TUTORIAL = _BASE + 60
    PRACTICE = _BASE + 61
    CUSTOM = _BASE + 62

    @property
    def display_name(self) -> str:
        return self.name.replace("_", " ").title().replace("Bots", "(vs. AI)")

    @staticmethod
    def is_normal(queue) -> bool:
        return queue in (
            Smite2QueueId.CONQUEST,
            Smite2QueueId.ARENA,
            Smite2QueueId.JOUST,
            Smite2QueueId.DUEL,
            Smite2QueueId.ASSAULT,
            Smite2QueueId.SLASH,
            Smite2QueueId.QUICKPLAY_ARENA,
            Smite2QueueId.QUICKPLAY_JOUST,
        )

    @staticmethod
    def is_ranked(queue) -> bool:
        return queue in (
            Smite2QueueId.RANKED_CONQUEST,
            Smite2QueueId.RANKED_JOUST,
            Smite2QueueId.RANKED_DUEL,
        )

    @staticmethod
    def is_duel(queue) -> bool:
        return queue in (Smite2QueueId.DUEL, Smite2QueueId.RANKED_DUEL)

    @staticmethod
    def is_vs_ai(queue) -> bool:
        return queue in (
            Smite2QueueId.CONQUEST_BOTS,
            Smite2QueueId.ARENA_BOTS,
            Smite2QueueId.JOUST_BOTS,
            Smite2QueueId.ASSAULT_BOTS,
        )

    @staticmethod
    def is_custom(queue) -> bool:
        return queue is Smite2QueueId.CUSTOM

    @staticmethod
    def is_tutorial(queue) -> bool:
        return queue is Smite2QueueId.TUTORIAL

    @staticmethod
    def is_practice(queue) -> bool:
        return queue is Smite2QueueId.PRACTICE

    @staticmethod
    def is_deprecated(queue) -> bool:
        return False

    @staticmethod
    def is_adventure(queue) -> bool:
        return False


# tracker.gg's `attributes.gamemode`, verbatim. Everything on the left was seen
# in the sample except where noted; unmapped strings become UNKNOWN and are
# logged so the enum can be extended rather than silently swallowing a mode.
MODE_STRINGS: Dict[str, Smite2QueueId] = {
    "conquest": Smite2QueueId.CONQUEST,
    "conquest-ranked": Smite2QueueId.RANKED_CONQUEST,
    "arena": Smite2QueueId.ARENA,
    "arena-ranked": Smite2QueueId.ARENA,
    "joust": Smite2QueueId.JOUST,
    "joust-ranked": Smite2QueueId.RANKED_JOUST,
    "duel": Smite2QueueId.DUEL,
    "duel-ranked": Smite2QueueId.RANKED_DUEL,
    "assault": Smite2QueueId.ASSAULT,
    "slash": Smite2QueueId.SLASH,
    "quickplay-arena": Smite2QueueId.QUICKPLAY_ARENA,
    "quickplay-joust": Smite2QueueId.QUICKPLAY_JOUST,
    "conquest-bots": Smite2QueueId.CONQUEST_BOTS,
    "arena-bots": Smite2QueueId.ARENA_BOTS,
    "joust-bots": Smite2QueueId.JOUST_BOTS,
    "assault-bots": Smite2QueueId.ASSAULT_BOTS,
    "tutorial": Smite2QueueId.TUTORIAL,
    "practice": Smite2QueueId.PRACTICE,
    "custom": Smite2QueueId.CUSTOM,
}


def from_mode_string(
    mode: Optional[str], ranked: bool = False
) -> Smite2QueueId:
    """Map tracker.gg's mode string onto a queue id.

    `ranked` is the match's own `isRanked` flag, used only when the mode string
    does not already say so — `conquest-ranked` is self-describing, but a future
    mode might carry the distinction only in the flag.
    """
    key = (mode or "").strip().lower()
    if not key:
        return Smite2QueueId.UNKNOWN
    found = MODE_STRINGS.get(key)
    if found is not None:
        return found
    if ranked:
        found = MODE_STRINGS.get(f"{key}-ranked")
        if found is not None:
            return found
    return Smite2QueueId.UNKNOWN
