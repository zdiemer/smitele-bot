"""What the other nine gods in the match imply about your build.

A build that ignores the lobby is wrong in a specific, expensive way: it splits
its protections evenly against a team that deals one kind of damage, buys no
anti-heal against a healer, and buys tenacity against a team with no crowd
control. None of that is visible in an item's stat line — it is a fact about
who you are playing against.

Both games are read through the same four questions, because both answer them,
just in different vocabularies. Smite 1 has `GodPro` ("high crowd control",
"high sustain") and a class per god; Smite 2 has the wiki's spec words
("Lockdown", "Healing", "Tank") and no classes at all. Neither is precise —
a god tagged Lockdown is not necessarily the reason you needed tenacity — but
both are the game's own description of what a god does, which beats an opinion.

Deliberately coarse. This produces a handful of ratios, not a simulation: the
honest resolution of "three of their five deal magical damage" is "build more
magical protection", not a number to three decimal places.
"""

from __future__ import annotations

from typing import Iterable, List, NamedTuple, Optional

from god import God
from god_types import GodPro, GodRole, GodType

# Smite 2 spec words, lowercased. The wiki writes them in title case and the
# vocabulary is small and stable — 22 words across the whole roster.
_HEALING_SPECS = frozenset({"healing", "sustain"})
_CROWD_CONTROL_SPECS = frozenset({"lockdown", "crowd control", "area control"})
_TANK_SPECS = frozenset({"tank", "brawler"})

_CROWD_CONTROL_PROS = frozenset(
    {GodPro.HIGH_CROWD_CONTROL, GodPro.MEDIUM_CROWD_CONTROL}
)


def _specs(god: God) -> frozenset:
    return frozenset(spec.strip().lower() for spec in getattr(god, "specs", None) or [])


def _pros(god: God) -> frozenset:
    return frozenset(getattr(god, "pros", None) or [])


def heals(god: God) -> bool:
    """Whether this god brings healing worth buying anti-heal for."""
    if _specs(god) & _HEALING_SPECS:
        return True
    return GodPro.HIGH_SUSTAIN in _pros(god)


def crowd_controls(god: God) -> bool:
    if _specs(god) & _CROWD_CONTROL_SPECS:
        return True
    return bool(_pros(god) & _CROWD_CONTROL_PROS)


def tanks(god: God) -> bool:
    """Whether this god is likely to be the team's front line."""
    if _specs(god) & _TANK_SPECS:
        return True
    if GodPro.HIGH_DEFENSE in _pros(god):
        return True
    return getattr(god, "role", None) is GodRole.GUARDIAN


class TeamContext(NamedTuple):
    """The lobby, reduced to what a build can act on.

    `physical_share` is the fraction of the enemy team dealing physical damage,
    and is 0.5 — an even split, which is what an uninformed build assumes —
    whenever nothing is known. Everything else is a count, so zero means "no
    reason to change anything" rather than "unknown".
    """

    physical_share: float = 0.5
    enemy_healers: int = 0
    enemy_crowd_control: int = 0
    enemy_count: int = 0
    allied_tanks: int = 0
    allied_healers: int = 0

    @property
    def known(self) -> bool:
        return self.enemy_count > 0 or self.allied_tanks > 0 or self.allied_healers > 0

    @property
    def crowd_control_share(self) -> float:
        if not self.enemy_count:
            return 0.0
        return self.enemy_crowd_control / self.enemy_count

    @property
    def wants_anti_heal(self) -> bool:
        return self.enemy_healers > 0

    def describe(self) -> str:
        """One line for the build embed, or nothing if there was no lobby."""
        if not self.known:
            return ""
        parts: List[str] = []
        if self.enemy_count:
            physical = round(self.physical_share * self.enemy_count)
            magical = self.enemy_count - physical
            parts.append(f"{physical} physical and {magical} magical")
        if self.enemy_healers:
            parts.append(
                f"{self.enemy_healers} healer{'s' if self.enemy_healers > 1 else ''}"
            )
        if self.enemy_crowd_control:
            parts.append(f"{self.enemy_crowd_control} bringing crowd control")
        line = ""
        if parts:
            line = f"Built against {', '.join(parts)}."
        if self.allied_tanks:
            line += (
                f" Your team already has {self.allied_tanks} front"
                f"line{'s' if self.allied_tanks > 1 else ''}."
            )
        return line.strip()


def read(
    enemies: Iterable[God] = None, allies: Iterable[God] = None
) -> TeamContext:
    """Reduce a lobby to a `TeamContext`.

    Gods that could not be resolved are simply absent; a partial enemy team is
    still worth reading, because three known enemies all dealing physical damage
    says as much as five would.
    """
    enemies = [god for god in (enemies or []) if god is not None]
    allies = [god for god in (allies or []) if god is not None]

    typed = [god for god in enemies if getattr(god, "type", None) is not None]
    physical = sum(1 for god in typed if god.type is GodType.PHYSICAL)
    physical_share = (physical / len(typed)) if typed else 0.5

    return TeamContext(
        physical_share=physical_share,
        enemy_healers=sum(1 for god in enemies if heals(god)),
        enemy_crowd_control=sum(1 for god in enemies if crowd_controls(god)),
        enemy_count=len(enemies),
        allied_tanks=sum(1 for god in allies if tanks(god)),
        allied_healers=sum(1 for god in allies if heals(god)),
    )


# How far the protection split may be pushed. A team of five physical gods is
# still worth *some* magical protection — a single magical ultimate does not
# care that the rest of the team is physical — so the split is compressed toward
# the middle rather than taken literally.
MAX_PROTECTION_SKEW = 0.6


def protection_scales(context: TeamContext) -> tuple:
    """`(physical, magical)` multipliers for a build's protection targets.

    Both are 1.0 against an even team, and the pair always averages 1.0, so
    tilting the split never changes how much total protection a build buys —
    only which kind. That matters: this is meant to aim the same defensive
    budget, not quietly turn every build into a tank.
    """
    if not context.enemy_count:
        return (1.0, 1.0)
    skew = (context.physical_share - 0.5) * 2 * MAX_PROTECTION_SKEW
    return (1.0 + skew, 1.0 - skew)
