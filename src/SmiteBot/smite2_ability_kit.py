"""Parsing a Smite 2 god's damaging abilities out of the wiki's rank tables.

`smite2_stats` states that ability scaling is not published machine-readably,
and for its purposes — a build's *stats* — that is true. But the god catalogue
does carry it: each ability's `rank_properties` hold a `Damage` line
(`85/130/175/220/265`) and a `Damage Scaling` line (`75 % Intelligence`),
populated for 335 of 374 abilities across the roster, with a cooldown alongside.
That is exactly the shape `ability_kit` reads for Smite 1, so the role vector's
burst axes need not stay a basic-attack proxy on this side either.

The parse is deliberately conservative, the same way the Smite 1 one is: only
recognisable damage lines count, only Strength/Intelligence scaling is kept
(a handful of abilities scale off protections or a target's health, which the
combat model here cannot use and which are left at zero rather than guessed),
per-tick lines are multiplied only when a tick count is stated, and everything
is read at max rank. What it cannot parse it drops, so burst is understated
rather than invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# Damage lines the enemy takes. "Damage Per Tick" is a channel; the rest are
# single applications unless a tick count says otherwise.
_DAMAGE_LABELS = {
    "damage",
    "initial damage",
    "detonation damage",
    "impact damage",
    "damage per shot",
    "damage per tick",
}

_TICK_LABELS = {"ticks", "hits", "max ticks", "shots"}


@dataclass
class Smite2Ability:
    name: str
    base_damage: float
    scaling: float
    scaling_stat: str  # "strength" | "intelligence"
    cooldown: float
    is_ultimate: bool
    hits: int = 1

    @property
    def total_base(self) -> float:
        return self.base_damage * self.hits

    @property
    def total_scaling(self) -> float:
        return self.scaling * self.hits


@dataclass
class Smite2Kit:
    abilities: List[Smite2Ability] = field(default_factory=list)

    @property
    def damaging(self) -> List[Smite2Ability]:
        return self.abilities


def _last_rank(text: str) -> Optional[float]:
    """The final rank of a `85/130/175/220/265`-style value, numbers only."""
    if not text:
        return None
    head = text.strip().split()[0] if text.strip() else ""
    parts = [p for p in head.split("/") if p]
    if not parts:
        return None
    try:
        return float(parts[-1])
    except ValueError:
        return None


def _parse_scaling(value: str) -> Optional[tuple]:
    """(fraction, stat) from `75 % Intelligence`, or None if not STR/INT.

    Abilities that scale off protections, a target's health, or basic-attack
    power are real but outside what the first-order combat model reads, so they
    come back None and contribute base damage only.
    """
    if not value or "%" not in value:
        return None
    percent_text, _, tail = value.partition("%")
    try:
        fraction = float(percent_text.strip()) / 100.0
    except ValueError:
        return None
    tail = tail.strip().lower()
    if "intelligence" in tail:
        return fraction, "intelligence"
    if "strength" in tail:
        return fraction, "strength"
    return None


def _hit_count(rank_properties) -> int:
    for prop in rank_properties:
        if prop.name.strip().lower() in _TICK_LABELS:
            count = _last_rank(prop.rank_values or "")
            if count and count > 1:
                return int(count)
    return 1


def parse_ability(ability, is_ultimate: bool) -> Optional[Smite2Ability]:
    base = None
    per_tick = False
    scaling = 0.0
    scaling_stat = "strength"
    for prop in ability.rank_properties:
        name = prop.name.strip().lower()
        if name in _DAMAGE_LABELS:
            parsed = _last_rank(prop.rank_values or "")
            if parsed is None:
                continue
            # Keep the largest damage line; second lines are usually
            # alternatives (per-target vs total) rather than additive.
            if base is None or parsed > base:
                base = parsed
                per_tick = "tick" in name or "shot" in name
        elif name == "damage scaling":
            parsed = _parse_scaling(prop.rank_values or "")
            if parsed is not None:
                scaling, scaling_stat = parsed

    if base is None:
        return None
    cooldowns = ability.cooldown_by_rank
    if not cooldowns:
        return None

    hits = _hit_count(ability.rank_properties) if per_tick else 1
    return Smite2Ability(
        name=ability.name,
        base_damage=base,
        scaling=scaling,
        scaling_stat=scaling_stat,
        cooldown=cooldowns[-1],
        is_ultimate=is_ultimate,
        hits=hits,
    )


def parse_kit(god) -> Smite2Kit:
    kit = Smite2Kit()
    abilities = getattr(god, "abilities", None) or []
    for index, ability in enumerate(abilities):
        if getattr(ability, "is_passive", False):
            continue
        parsed = parse_ability(ability, is_ultimate=(index == 3))
        if parsed is not None:
            kit.abilities.append(parsed)
    return kit
