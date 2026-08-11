"""The role-shaped score vector, on Smite 2's stat model.

`role_score` scores a Smite 1 build off `BuildStatCalculator` and the
per-second combat sim. Smite 2 needs the same vector — it is the only absolute
metric there is for grading `smite2_optimizer`, whose builds the corpus cannot
score — but off a different stat model and, for now, a simpler combat model.

This is the first-order port. Kill speed is steady-state DPS against a target's
protections rather than a tick-by-tick sim: Smite 2 exposes attack damage and
attack speed directly on `Smite2Stats`, so a per-second sim would have little
more to work from on the basic-attack side than this does. The carry, solo and
support axes need only DPS, effective HP and cooldown rate, all of which the
stat model gives exactly.

Mid and jungle lean on burst, which is abilities. `smite2_stats` notes that
ability scaling is not machine-readable for *its* purposes, and that is true of
the stat totals — but the god catalogue does carry each ability's damage and
scaling in its rank tables, which `smite2_ability_kit` parses. So mid and
jungle use real rotation burst here, not the basic-attack proxy they first
shipped with.

Opponents are derived from the corpus, not hand-built: the median health and
protections of each role's real builds. The corpus cannot score the optimizer's
builds, but it states precisely what they will face — a frontline (the solo
laner's median durability) and a backline (the carry's).

One thing measured on the first real run and worth knowing: in the current
Smite 2 corpus the solo and carry medians come out nearly equal in durability
(health ~2210, physical protection ~71 for both), where Smite 1's frontline and
backline were far apart. That is the meta, not a defect — Smite 2 itemises less
hard tankiness than Smite 1 did — but it means a carry's two kill-speed axes
presently carry less independent information than they will if the metas
diverge. Left as medians per the agreed design rather than forcing a spread.

The vector shape and the Pareto machinery are shared with `role_score`; only the
stat model underneath differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from god import God
from god_types import GodType
from item import Item, ItemAttribute
from role_score import RoleVector
from smite2_stats import (
    CRITICAL_MULTIPLIER,
    _damage_stat_for,
    build_stats,
    damage_taken_multiplier,
    effective_health,
    penetrated,
)

try:
    from smite2_ability_kit import parse_kit
except ImportError:  # pragma: no cover - always present in-tree
    parse_kit = None


@dataclass(frozen=True)
class Defender:
    """A target's durability, the only thing kill speed needs to know of it."""

    health: float
    physical_protection: float
    magical_protection: float

    def protection(self, magical: bool) -> float:
        return self.magical_protection if magical else self.physical_protection

    def effective_health(self, magical: bool) -> float:
        return effective_health(self.health, self.protection(magical))


def defender_of(stats) -> Defender:
    return Defender(
        health=stats.get(ItemAttribute.HEALTH),
        physical_protection=stats.get(ItemAttribute.PHYSICAL_PROTECTION),
        magical_protection=stats.get(ItemAttribute.MAGICAL_PROTECTION),
    )


def median_defender(defenders: Sequence[Defender]) -> Optional[Defender]:
    """The component-wise median of several defenders.

    Per-component rather than picking a whole build, because the point is the
    typical durability a role presents, and health and protections are bought
    on separate items — the median of each is the honest centre.
    """
    if not defenders:
        return None

    def med(values: List[float]) -> float:
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return 0.5 * (ordered[mid - 1] + ordered[mid])

    return Defender(
        health=med([d.health for d in defenders]),
        physical_protection=med([d.physical_protection for d in defenders]),
        magical_protection=med([d.magical_protection for d in defenders]),
    )


def _damage_is_magical(god: God) -> bool:
    return _damage_stat_for(god) == ItemAttribute.INTELLIGENCE


def basic_dps(stats, god: God, defender: Defender) -> float:
    """Steady-state basic-attack damage a second into `defender`.

    Attack damage times swings a second times the average crit multiplier,
    scaled by what gets through the target's protection after penetration.
    """
    magical = _damage_is_magical(god)
    crit_chance = min(stats.get_percent(ItemAttribute.CRITICAL_CHANCE), 1.0)
    crit_factor = 1.0 + crit_chance * (CRITICAL_MULTIPLIER - 1.0)
    after = penetrated(
        defender.protection(magical),
        stats.get(ItemAttribute.PENETRATION),
        stats.get_percent(ItemAttribute.PENETRATION),
    )
    per_hit = stats.attack_damage * crit_factor * damage_taken_multiplier(after)
    return per_hit * stats.attacks_per_second


def kill_speed(stats, god: God, defender: Defender) -> float:
    """1 / time-to-kill, so faster is a larger number."""
    dps = basic_dps(stats, god, defender)
    if dps <= 0:
        return 0.0
    ttk = defender.health / dps
    return 1.0 / ttk if ttk > 0 else 0.0


def penetration_efficiency(stats, defender: Defender, magical: bool) -> float:
    """Share of the build's penetration that actually reduces protection."""
    prot = defender.protection(magical)
    nominal = prot * stats.get_percent(ItemAttribute.PENETRATION) + stats.get(
        ItemAttribute.PENETRATION
    )
    if nominal <= 0:
        return 1.0
    return min(nominal, prot) / nominal


def mean_effective_health(stats) -> float:
    health = stats.get(ItemAttribute.HEALTH)
    phys = effective_health(health, stats.get(ItemAttribute.PHYSICAL_PROTECTION))
    mag = effective_health(health, stats.get(ItemAttribute.MAGICAL_PROTECTION))
    return 0.5 * (phys + mag)


def rotation_burst(stats, god: God, defender: Defender) -> float:
    """Damage one cast of each ability deals to `defender`, after protections.

    The one-rotation number for burst roles, from the god's parsed kit. An
    Intelligence-scaling ability deals magical damage and meets magical
    protection; a Strength one, physical. Basics are excluded — a rotation is
    abilities — and an ability the kit could not parse contributes nothing,
    so this understates rather than invents.
    """
    if parse_kit is None:
        return 0.0
    kit = parse_kit(god)
    total = 0.0
    pen_flat = stats.get(ItemAttribute.PENETRATION)
    pen_pct = stats.get_percent(ItemAttribute.PENETRATION)
    for ability in kit.damaging:
        magical = ability.scaling_stat == "intelligence"
        power = stats.get(
            ItemAttribute.INTELLIGENCE if magical else ItemAttribute.STRENGTH
        )
        raw = ability.total_base + ability.total_scaling * power
        after = penetrated(
            defender.protection(magical), pen_flat, pen_pct
        )
        total += raw * damage_taken_multiplier(after)
    return total


def role_vector(
    role: str,
    god: God,
    items: List[Item],
    frontline: Defender,
    backline: Defender,
    level: int = 20,
) -> RoleVector:
    """The Smite 2 score vector for one build in one role.

    `role` is a `PlayerRole.value` string (carry/jungle/mid/solo/support). The
    axes mirror `role_score.role_vector`; see that module for why each belongs.
    """
    stats = build_stats(god, items, level)
    magical = _damage_is_magical(god)
    own_ehp = mean_effective_health(stats)
    role = (role or "").lower()

    if role == "carry":
        return RoleVector(
            axes=(
                kill_speed(stats, god, frontline),
                kill_speed(stats, god, backline),
                own_ehp,
            ),
            labels=("kill_speed_front", "kill_speed_back", "ehp"),
        )
    if role == "solo":
        damage = kill_speed(stats, god, frontline)
        return RoleVector(axes=(own_ehp * damage,), labels=("ehp_x_damage",))
    if role == "support":
        cooldown_rate = stats.get(ItemAttribute.COOLDOWN_RATE)
        return RoleVector(axes=(own_ehp, cooldown_rate), labels=("ehp", "cooldown_rate"))

    # Mid and jungle are burst roles, and Smite 2 does expose ability damage
    # after all — parsed by smite2_ability_kit — so these use real rotation
    # burst rather than the basic-attack proxy they first shipped with. Jungle
    # is burst over a backliner's effective HP plus its own survival; mid is
    # burst into both ends plus penetration efficiency.
    if role == "jungle":
        back_ehp = backline.effective_health(magical)
        burst = rotation_burst(stats, god, backline)
        return RoleVector(
            axes=(burst / back_ehp if back_ehp > 0 else 0.0, own_ehp),
            labels=("burst_ratio_back", "ehp"),
        )
    if role == "mid":
        return RoleVector(
            axes=(
                rotation_burst(stats, god, frontline),
                rotation_burst(stats, god, backline),
                penetration_efficiency(stats, backline, magical),
            ),
            labels=("burst_front", "burst_back", "pen_efficiency"),
        )

    return RoleVector(
        axes=(
            kill_speed(stats, god, frontline),
            kill_speed(stats, god, backline),
            own_ehp,
        ),
        labels=("kill_speed_front", "kill_speed_back", "ehp"),
    )
