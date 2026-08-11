"""Parsing a god's damaging abilities out of Hi-Rez's display strings.

The API ships ability numbers as UI text — "Damage: 90/155/220/285/350 (+90%
of your Physical Power)", "Cooldown: 13/12.5/12/11.5/11s" — because that is
what the in-game tooltip shows. It is nevertheless the only machine-readable
statement of ability damage there is, and it is the game's own, so the combat
sim parses it rather than hand-maintaining a table of 130 kits.

Parsing display text has known failure modes, so the rules are conservative:

- Only lines whose label is recognisably a damage line are counted, and lines
  that describe damage *taken*, minion-only damage, or self-damage are not.
- Per-tick and per-hit lines are multiplied by the tick count only when a
  sibling line states one; otherwise a single application is assumed. That
  understates channelled abilities rather than inventing a number.
- Everything is read at max rank, which is where a level-20 build evaluation
  lives.

`parse_kit` returns what the combat sim needs and nothing else: base damage,
power scaling fraction and stat, cooldown seconds, per-ability. Buff lines
("Attack Speed: +40%") are deliberately not interpreted here — steroid
semantics vary by god and are supplied by the curated table in the sim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# "90/155/220/285/350" — the by-rank progression; a lone number also matches.
_RANKS = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?(?:\s*/\s*[0-9]+(?:\.[0-9]+)?)*)")
_SCALING = re.compile(
    r"\+\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*(?:of your\s*)?(Physical|Magical)\s*Power",
    re.IGNORECASE,
)

# Labels that state damage an enemy takes from the ability. Matched against
# the lowercased label with punctuation intact.
_DAMAGE_LABELS = (
    "damage",
    "damage per tick",
    "damage per hit",
    "damage per shot",
    "damage per strike",
    "damage per blade",
    "damage per arrow",
    "damage per bounce",
    "initial damage",
    "dash damage",
    "detonation damage",
    "explosion damage",
    "impact damage",
)

# Labels that look like damage but must not be counted as the enemy's.
_EXCLUDED_FRAGMENTS = (
    "taken",
    "self",
    "minion",
    "jungle",
    "structure",
    "shield",
    "mitigat",
    "reduction",
    "heal",
)

_TICKS = re.compile(r"(?:ticks|hits|shots|strikes|arrows|blades)\s*:?\s*", re.IGNORECASE)


@dataclass
class DamagingAbility:
    name: str
    base_damage: float
    scaling: float
    scaling_stat: str  # "physical" | "magical"
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
class AbilityKit:
    abilities: List[DamagingAbility] = field(default_factory=list)

    @property
    def damaging(self) -> List[DamagingAbility]:
        return self.abilities


def _last_rank(text: str) -> Optional[float]:
    match = _RANKS.match(text.strip())
    if not match:
        return None
    return float(match.group(1).split("/")[-1])


def _hit_count(rank_properties) -> int:
    """A stated tick/hit count, or 1.

    Only trusted when a sibling line literally states one — "Ticks: 4" or
    "Hits: 3". A range takes its final rank.
    """
    for entry in rank_properties:
        label = entry.name.strip().lower()
        if label in ("ticks", "hits", "shots", "strikes", "max ticks"):
            count = _last_rank(entry.rank_values or "")
            if count and count > 1:
                return int(count)
    return 1


def _is_damage_label(label: str) -> bool:
    label = label.strip().lower().rstrip(":")
    if any(fragment in label for fragment in _EXCLUDED_FRAGMENTS):
        return False
    if label in _DAMAGE_LABELS:
        return True
    # "Damage (per pillar)" style variants.
    return label.startswith("damage per ") or label.startswith("damage (")


def parse_ability(ability, is_ultimate: bool) -> Optional[DamagingAbility]:
    rank_properties = ability.rank_properties

    per_hit_label = False
    base = scaling = None
    scaling_stat = "physical"
    for entry in rank_properties:
        if not _is_damage_label(entry.name):
            continue
        value = entry.rank_values or ""
        parsed = _last_rank(value)
        if parsed is None:
            continue
        if base is not None:
            # Two damage lines (e.g. initial + detonation): take the pair with
            # the larger base rather than summing, since many second lines are
            # alternatives (per-target vs total) rather than additions.
            if parsed <= base:
                continue
        base = parsed
        per_hit_label = "per" in entry.name.lower()
        match = _SCALING.search(value)
        if match:
            scaling = float(match.group(1)) / 100.0
            scaling_stat = match.group(2).lower()
        else:
            scaling = 0.0

    if base is None:
        return None

    cooldowns = ability.cooldown_by_rank
    if not cooldowns:
        # An ability with damage but no cooldown (stances, toggles) cannot be
        # scheduled; skipping it understates rather than fabricates.
        return None
    cooldown = cooldowns[-1]

    hits = _hit_count(rank_properties) if per_hit_label else 1
    return DamagingAbility(
        name=ability.name,
        base_damage=base,
        scaling=scaling,
        scaling_stat=scaling_stat,
        cooldown=cooldown,
        is_ultimate=is_ultimate,
        hits=hits,
    )


def parse_kit(god) -> AbilityKit:
    kit = AbilityKit()
    abilities = getattr(god, "abilities", None) or []
    for index, ability in enumerate(abilities):
        if getattr(ability, "is_passive", False):
            continue
        parsed = parse_ability(ability, is_ultimate=(index == 3))
        if parsed is not None:
            kit.abilities.append(parsed)
    return kit


@dataclass(frozen=True)
class Steroid:
    """A god's own contribution to their basic attacks, averaged over uptime.

    The numbers come from the god pages (verified 2026-08-10); the *averaging*
    is this module's own approximation. A 5s steroid on a 14s cooldown is
    modelled as its bonus times 5/14, always on — right for sustained TTK,
    an understatement for burst windows. Pet gods (Skadi) and conditional
    passives that need game state the sim lacks (Hou Yi's missing-health mark,
    Artemis' bonus against crowd control) are left out rather than guessed at.
    """

    attack_speed: float = 0.0   # uptime-averaged attack speed increase
    flat_basic: float = 0.0     # flat damage riding every basic
    power_scale_basic: float = 0.0  # % of physical power added to every basic
    power_multiplier: float = 1.0   # steady-state power multiplier
    prot_strip: float = 0.0     # flat protections stripped off the target


# Keyed by god name to stay readable; hunters first, since carries are where
# the sim is validated.
STEROIDS = {
    "Neith": Steroid(attack_speed=0.30 * 4 / 12),
    "Medusa": Steroid(attack_speed=0.80 * 2 / 10),
    "Hachiman": Steroid(attack_speed=0.15 + 0.40 * 6 / 14),
    "Izanami": Steroid(attack_speed=0.65 * 6 / 10),
    "Nut": Steroid(attack_speed=0.25),
    "Artemis": Steroid(attack_speed=0.80 * 5 / 14),
    "Rama": Steroid(attack_speed=0.60 * 5 / 11),
    "Jing Wei": Steroid(attack_speed=0.40 * 7 / 10),
    "Ullr": Steroid(attack_speed=0.40 * 5 / 14),
    "Cupid": Steroid(attack_speed=0.20 + 0.20 * 4 / 12),
    "Apollo": Steroid(attack_speed=0.30),
    "Charybdis": Steroid(attack_speed=0.25),
    "Xbalanque": Steroid(flat_basic=50.0),
    "Cernunnos": Steroid(flat_basic=25.0, power_scale_basic=0.08),
    "Anhur": Steroid(prot_strip=20.0),
    "Heimdallr": Steroid(power_multiplier=1.15),
}
