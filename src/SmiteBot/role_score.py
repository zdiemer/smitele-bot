"""A role-shaped score vector for a build, not a single number.

The TTK sim answers one question — how fast does this build kill — and
`ttk_validate` shows it answers it well for carries and progressively less well
as a role's job drifts from sustained damage toward survival. A solo laner that
kills fast but folds is a bad solo laner, and no single scalar ranks it
correctly against one that trades kill speed for staying alive.

So each role is scored as a *vector* of the things that role is actually for,
and builds are ordered by Pareto dominance over that vector rather than by a
weighted sum of it. The distinction is the whole point. A weighted sum re-imports
the hand-tuned-constant problem the archetype tables already have — pick the
weights and you have picked the answer. Pareto dominance asks only the
un-weighted question: is build A at least as good as B on *every* axis its role
cares about, and strictly better on one? That is a partial order — some builds
are simply incomparable, and the ranking says so rather than inventing a
tiebreak — but where it does order two builds it does so without a single tuned
coefficient.

Every axis here is oriented so that **more is better**, which is what lets
`dominates` be a plain component-wise `>=`. Kill speed is `1 / ttk` rather than
ttk; effective HP, burst and penetration efficiency are already more-is-better.

The axes per role, and why each belongs:

- carry: kill speed vs a frontline and vs a backline, plus own effective HP.
  A carry's job is sustained damage into both, and the EHP axis is the floor
  that stops "glass that never gets to attack" from dominating.
- assassin (jungle): sustained kill speed into a backliner times own effective
  HP, one axis. The first version scored one-rotation burst on the theory that
  an assassin deletes on contact, but the corpus rejected it — Smite 1 junglers
  weave to a kill, and sustained kill speed orders their builds where a single
  rotation barely beats a coin. See `role_validate` for the numbers.
- mid: burst into a frontline and into a backline, plus penetration
  efficiency. Penetration past a target's protection is wasted, so two builds
  of equal burst are not equal if one is spending its pen into the floor.
- solo: effective HP *times* damage, as one axis. A product, so a build that is
  all bruise and no bite, or all bite and no bruise, scores low at either
  extreme — which is exactly the solo failure mode a sum would hide.
- support: effective HP and cooldown rate. A support wins by being present and
  casting often, not by killing; damage is deliberately absent.

This scores what a build *is*, from the stat model and the game's numbers. What
a build is *worth in a given match* — the cleanse against a stun, the blink
onto a backline — is a utility question no stat line answers, and it stays in
the optimizer rather than being smuggled into an axis here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from god_types import GodType
from item import ItemAttribute
from stat_calculator import (
    BuildStatCalculator,
    DamageCalculator,
    GodBuild,
    _Penetration,
)

try:
    from ability_kit import STEROIDS, AbilityKit, parse_kit
except ImportError:  # pragma: no cover - ability_kit is always present in-tree
    STEROIDS, AbilityKit, parse_kit = {}, None, None


def _stat(stats, attribute: ItemAttribute, default: float = 0.0) -> float:
    return stats.get_stat(attribute) if stats.has_stat(attribute) else default


def effective_health(stats, magical: bool) -> float:
    """Damage of the given type needed to kill, i.e. health scaled by protection.

    The same curve `smite2_stats.effective_health` uses — 450 health and 40
    protection are the same purchase at different points on it — computed here
    from the Smite 1 stat bag.
    """
    health = _stat(stats, ItemAttribute.HEALTH)
    prot = _stat(
        stats,
        ItemAttribute.MAGICAL_PROTECTION if magical else ItemAttribute.PHYSICAL_PROTECTION,
    )
    return health * (100.0 + max(0.0, prot)) / 100.0


def mean_effective_health(stats) -> float:
    """Effective HP averaged over both damage types.

    A build faces physical and magical damage both, and averaging is the
    honest one-number summary of how much it takes to kill — a build stacked
    against only one type is correctly rated between its two EHPs.
    """
    return 0.5 * (effective_health(stats, False) + effective_health(stats, True))


def penetration_efficiency(attacker_stats, defender_stats, physical: bool) -> float:
    """How much of the attacker's penetration lands, in [0, 1].

    Penetration removes protection but cannot drive it below zero, so a build
    with more penetration than the target has protection is paying for a stat
    that does nothing past the floor. Efficiency is the share of the
    penetration's nominal effect that actually reduces protection.
    """
    pen = attacker_stats.get_stat(
        ItemAttribute.PHYSICAL_PENETRATION if physical else ItemAttribute.MAGICAL_PENETRATION
    ) if attacker_stats.has_stat(
        ItemAttribute.PHYSICAL_PENETRATION if physical else ItemAttribute.MAGICAL_PENETRATION
    ) else _Penetration(0, 0)
    prot = _stat(
        defender_stats,
        ItemAttribute.PHYSICAL_PROTECTION if physical else ItemAttribute.MAGICAL_PROTECTION,
    )
    nominal = prot * pen.percent + pen.flat
    if nominal <= 0:
        # No penetration to waste; not a build that is spending badly, so it is
        # perfectly efficient by definition rather than undefined.
        return 1.0
    used = min(nominal, prot)
    return used / nominal


def _kill_speed(
    attacker: GodBuild,
    defender: GodBuild,
    kit,
    steroid,
    weave: float,
) -> float:
    """1 / ttk, so that faster is a larger number. Zero if the kill never lands."""
    ttk = DamageCalculator.calculate_basic_ttk(
        attacker,
        defender,
        assume_item_passives_stacked=True,
        kit=kit,
        steroid=steroid,
        weave=weave,
    )
    return 1.0 / ttk if ttk > 0 else 0.0


def _rotation_burst(attacker: GodBuild, attacker_stats, defender_stats) -> float:
    """Damage one cast of each ability deals to the defender, after protections.

    A one-rotation number for burst roles. Basics are excluded on purpose: this
    is the "delete on contact" question, and a rotation is abilities. Uses the
    same protection formula the sim's cast loop uses.
    """
    if parse_kit is None:
        return 0.0
    kit = parse_kit(attacker.god)
    physical = attacker.god.type == GodType.PHYSICAL
    power = _stat(
        attacker_stats,
        ItemAttribute.PHYSICAL_POWER if physical else ItemAttribute.MAGICAL_POWER,
    )
    total = 0.0
    for ability in kit.damaging:
        magical_ability = ability.scaling_stat == "magical"
        ability_power = _stat(
            attacker_stats,
            ItemAttribute.MAGICAL_POWER if magical_ability else ItemAttribute.PHYSICAL_POWER,
        )
        raw = ability.total_base + ability.total_scaling * ability_power
        pen = attacker_stats.get_stat(
            ItemAttribute.MAGICAL_PENETRATION if magical_ability else ItemAttribute.PHYSICAL_PENETRATION
        ) if attacker_stats.has_stat(
            ItemAttribute.MAGICAL_PENETRATION if magical_ability else ItemAttribute.PHYSICAL_PENETRATION
        ) else _Penetration(0, 0)
        prot = _stat(
            defender_stats,
            ItemAttribute.MAGICAL_PROTECTION if magical_ability else ItemAttribute.PHYSICAL_PROTECTION,
        )
        after = max(0.0, prot * (1 - min(pen.percent, 0.40)) - pen.flat)
        total += raw * 100.0 / (after + 100.0)
    return total


@dataclass
class RoleVector:
    """A build's role-relevant axes, all oriented so more is better."""

    axes: Tuple[float, ...]
    labels: Tuple[str, ...]

    def dominates(self, other: "RoleVector") -> bool:
        """Pareto: at least as good on every axis, strictly better on one."""
        ge = all(a >= b for a, b in zip(self.axes, other.axes))
        gt = any(a > b for a, b in zip(self.axes, other.axes))
        return ge and gt


# The five roles, and which axis-builder each uses. Role names match the
# corpus `Role` column (Carry/Jungle/Mid/Solo/Support).
def role_vector(
    role: str,
    attacker: GodBuild,
    frontline: GodBuild,
    backline: GodBuild,
    weave: float = 1.0,
) -> RoleVector:
    """The score vector for one build in one role.

    `frontline` and `backline` are corpus-derived opponents — the tanky and
    squishy ends a build has to deal with — so the axes are grounded in what
    the build will actually face rather than a hand-built dummy.
    """
    stats = BuildStatCalculator(attacker).calculate_god_build_stats()
    front_stats = BuildStatCalculator(frontline).calculate_god_build_stats()
    back_stats = BuildStatCalculator(backline).calculate_god_build_stats()
    physical = attacker.god.type == GodType.PHYSICAL
    kit = parse_kit(attacker.god) if parse_kit is not None else None
    steroid = STEROIDS.get(attacker.god.name)

    own_ehp = mean_effective_health(stats)

    if role == "Carry":
        return RoleVector(
            axes=(
                _kill_speed(attacker, frontline, kit, steroid, weave),
                _kill_speed(attacker, backline, kit, steroid, weave),
                own_ehp,
            ),
            labels=("kill_speed_front", "kill_speed_back", "ehp"),
        )
    if role == "Jungle":
        # Sustained kill speed into the backline times survival, as one axis —
        # the solo shape pointed at the backline instead of the frontline. Two
        # measured facts drove this away from the original burst vector. First,
        # burst is the wrong model for Smite 1 junglers: per-cell concordance
        # against win rate is 0.554 for sustained kill speed and 0.517 — barely
        # a coin — for a single ability rotation, because they weave to a kill
        # rather than one-shotting. Second, kill speed and survival do not want
        # to be *separate* Pareto axes here: split, the two-axis vector scores
        # 0.528, below kill speed alone, because requiring both to agree throws
        # away the pairs where they trade. Their product (0.557) keeps those
        # pairs and is the best single number the corpus offers for the role.
        kill_speed = _kill_speed(attacker, backline, kit, steroid, weave)
        return RoleVector(axes=(kill_speed * own_ehp,), labels=("killspeed_x_ehp",))
    if role == "Mid":
        return RoleVector(
            axes=(
                _rotation_burst(attacker, stats, front_stats),
                _rotation_burst(attacker, stats, back_stats),
                penetration_efficiency(stats, back_stats, physical),
            ),
            labels=("burst_front", "burst_back", "pen_efficiency"),
        )
    if role == "Solo":
        # Effective HP times damage as one axis: the product collapses a build
        # that is all bruise or all bite, which is the solo failure a sum hides.
        damage = _kill_speed(attacker, frontline, kit, steroid, weave)
        return RoleVector(axes=(own_ehp * damage,), labels=("ehp_x_damage",))
    if role == "Support":
        cooldown_rate = _stat(stats, ItemAttribute.COOLDOWN_REDUCTION)
        return RoleVector(axes=(own_ehp, cooldown_rate), labels=("ehp", "cooldown_rate"))

    # An unknown role falls back to the carry shape rather than raising: a new
    # corpus role should degrade to "kill things and survive", not crash.
    return RoleVector(
        axes=(
            _kill_speed(attacker, frontline, kit, steroid, weave),
            _kill_speed(attacker, backline, kit, steroid, weave),
            own_ehp,
        ),
        labels=("kill_speed_front", "kill_speed_back", "ehp"),
    )


def pareto_layers(vectors: Sequence[RoleVector]) -> List[int]:
    """Each vector's Pareto layer: 0 is non-dominated, higher is worse.

    A total-ish ranking recovered from the partial order — a build's layer is
    how many rounds of stripping the current Pareto front it survives — for
    callers that need an ordering rather than a dominance test.
    """
    n = len(vectors)
    remaining = set(range(n))
    layer = [0] * n
    current = 0
    while remaining:
        front = {
            i
            for i in remaining
            if not any(j != i and vectors[j].dominates(vectors[i]) for j in remaining)
        }
        if not front:
            # Cyclic incomparabilities cannot arise from a partial order, but
            # guard against an infinite loop rather than trust that blindly.
            front = set(remaining)
        for i in front:
            layer[i] = current
        remaining -= front
        current += 1
    return layer
