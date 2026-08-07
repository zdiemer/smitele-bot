"""Smite 2's stat model: what a build actually gives you.

Smite 1's `BuildStatCalculator` cannot be pointed at this game. It is written
around Physical/Magical Power, a 325 protection cap, per-item id special cases
for a hundred Smite 1 items, and a `god_type` split that decides which half of
an item's stats a god is allowed to benefit from. Smite 2 replaced the power
pair with Strength and Intelligence — which every god scales off to some degree
— dropped the type split entirely, and caps different stats at different values.
Running Smite 1's formulas over Smite 2's catalogue produces confident nonsense,
which is why the two live side by side rather than one growing a `game` branch.

Every number below is from wiki.smite2.com/w/Stats, the same wiki the item and
god catalogues are read from, unless the comment says otherwise. Stats the wiki
states no cap for are left uncapped here rather than inheriting Smite 1's — an
invented cap is indistinguishable from a real one once it is in the code.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from god import God
from god_types import GodType
from item import Item, ItemAttribute

# Protections, Plating and Dampening all answer the same question — what
# fraction of an incoming hit lands — and all use this curve. 50 protection
# takes a third off, 100 halves it, 150 takes 60%. The wiki's "1 Protection
# lets you withstand 1% of the damage" is the shorthand for it, not a second
# formula: it is only true near zero.
def damage_taken_multiplier(protection: float) -> float:
    """The fraction of a hit that gets through `protection`."""
    return 100.0 / (100.0 + max(0.0, protection))


def effective_health(health: float, protection: float) -> float:
    """Health scaled by what protection buys, i.e. damage needed to kill.

    The inverse of `damage_taken_multiplier`, and the honest way to compare a
    health item against a protection item — 450 health and 40 protection are
    the same purchase at different points on the curve.
    """
    return health * (100.0 + max(0.0, protection)) / 100.0


def cooldown_reduction(cooldown_rate: float) -> float:
    """Cooldown Rate points as the reduction they actually produce.

    Cooldown Rate is not a percentage, though it is printed as a bare number
    that looks like one: a cooldown becomes `base * 100 / (100 + rate)`, so 25
    Rate is 20% off, not 25%, and the stat has diminishing returns built into
    its definition. Showing the raw points next to the percentage is the only
    way a build's cooldowns read correctly.
    """
    rate = max(0.0, cooldown_rate)
    return rate / (100.0 + rate)


def penetrated(protection: float, flat: float, percent: float) -> float:
    """A target's protection after penetration, floored at zero.

    Percent before flat, which is the order the game applies them in and is not
    commutative — 20% then 30 flat off 100 protection leaves 50, the other way
    round leaves 56.
    """
    return max(0.0, protection * (1.0 - percent) - flat)


# Attacks scale off both damage stats for every god in the game, which is why
# neither is "the" damage stat here the way Physical/Magical Power was in Smite
# 1. Abilities scale per-ability and the wiki does not publish the ratios in a
# machine-readable form, so only the attack is modelled.
STRENGTH_ATTACK_SCALING = 1.0
INTELLIGENCE_ATTACK_SCALING = 0.20

# Critical Strikes deal 1.65x, per the Deathbringer article — whose passive
# raises it to 2.0x, which is the arithmetic that pins the base value down. The
# Stats page's "increases your damage by 150%" is the older number.
CRITICAL_MULTIPLIER = 1.65

# Gods publish `AttackSpeedPercent`, a bonus over one swing a second, so a god
# sitting at 28 at level 20 swings 1.28 times a second before items.
BASE_ATTACKS_PER_SECOND = 1.0

# Caps that exist. Anything absent from both dicts is uncapped as far as the
# wiki says — notably Attack Speed, which Smite 1 caps at 2.5 and Smite 2
# publishes no figure for at all.
FLAT_CAPS: Dict[ItemAttribute, float] = {
    ItemAttribute.PENETRATION: 50.0,
    ItemAttribute.PLATED: 35.0,
    ItemAttribute.DAMPENING: 35.0,
    # Printed as a bare number but denominated in percentage points, so its
    # "50%" cap is 50 here.
    ItemAttribute.TENACITY: 50.0,
}

PERCENT_CAPS: Dict[ItemAttribute, float] = {
    ItemAttribute.PENETRATION: 0.40,
    ItemAttribute.CRITICAL_CHANCE: 1.0,
    # Anti-heal from items. It also does not stack with itself, which no build
    # of six distinct items can violate, so only the cap is modelled.
    ItemAttribute.HEAL_REDUCTION: 0.25,
}

# `Adaptive Stat: +30 Strength or +45 Intelligence (based on highest item stat).`
# 29 items in the catalogue carry their damage stat this way — a quarter of
# everything with Strength or Intelligence on it — and none of it reaches the
# infobox's `statN` parameters, so a build's damage stat reads far too low
# without this. Tolerant of the spacing and of "hightest", which The Executioner
# has said since the item shipped.
_ADAPTIVE = re.compile(
    r"Adaptive\s+Stat:\s*\+?\s*([\d.]+)\s*Strength\s*or\s*\+?\s*([\d.]+)\s*Intelligence",
    re.IGNORECASE,
)


# The regens reach us under two names: a god's curve is `HealthPerTime`, which
# the god loader maps onto Smite 1's HP5, while an item's is the wiki's `hpr`
# template, which maps onto HEALTH_REGEN. They are one stat — per second in
# both games' data, whatever HP5 is called — so items are folded onto the god's
# name for it, otherwise a build reports its regen twice and neither total
# includes the god.
_ALIASES: Dict[ItemAttribute, ItemAttribute] = {
    ItemAttribute.HEALTH_REGEN: ItemAttribute.HP5,
    ItemAttribute.MANA_REGEN: ItemAttribute.MP5,
}


def adaptive_stat(item: Item) -> Optional[Tuple[float, float]]:
    """An item's `(strength, intelligence)` adaptive pair, if it has one."""
    match = _ADAPTIVE.search(item.passive or "")
    if match is None:
        return None
    return (float(match.group(1)), float(match.group(2)))


# Stat grants written into passive text rather than into the infobox's stat
# parameters. Unlike the adaptive pair these are usually conditional — on a
# kill, below 40% health, while a stack is up — so they are kept apart from
# `item_stats`, which has to stay literally true for the stat card, and are only
# consumed by the optimizer, which discounts them.
_PASSIVE_FLAT = re.compile(
    r"\+\s*([\d.]+)\s+(Strength|Intelligence|Physical Protection|Magical Protection|"
    r"Health|Mana|Dampening|Plated|Plating|Tenacity|Cooldown Rate|Protections?)\b",
    re.IGNORECASE,
)

_PASSIVE_PERCENT = re.compile(
    r"\+\s*([\d.]+)%\s+(Penetration|Attack Speed|Lifesteal|Movement Speed|"
    r"Critical Chance|Healing Reduction)\b",
    re.IGNORECASE,
)

_PASSIVE_NAMES: Dict[str, ItemAttribute] = {
    "strength": ItemAttribute.STRENGTH,
    "intelligence": ItemAttribute.INTELLIGENCE,
    "physical protection": ItemAttribute.PHYSICAL_PROTECTION,
    "magical protection": ItemAttribute.MAGICAL_PROTECTION,
    "health": ItemAttribute.HEALTH,
    "mana": ItemAttribute.MANA,
    "dampening": ItemAttribute.DAMPENING,
    "plated": ItemAttribute.PLATED,
    "plating": ItemAttribute.PLATED,
    "tenacity": ItemAttribute.TENACITY,
    "cooldown rate": ItemAttribute.COOLDOWN_RATE,
    "penetration": ItemAttribute.PENETRATION,
    "attack speed": ItemAttribute.ATTACK_SPEED,
    "lifesteal": ItemAttribute.LIFESTEAL,
    "movement speed": ItemAttribute.MOVEMENT_SPEED,
    "critical chance": ItemAttribute.CRITICAL_CHANCE,
    "healing reduction": ItemAttribute.HEAL_REDUCTION,
}

# `+Intelligence equal to 25% of your Intelligence from items` (Rod of Tahuti)
# and `+Intelligence equal to 5% of Mana from Items` (Book of Thoth) — the two
# highest-picked items in the entire corpus both carry their real value this
# way, so a model that ignores them ranks them below a raw-stat item nobody
# builds.
_PASSIVE_SCALED = re.compile(
    r"\+\s*(Strength|Intelligence)\s+equal to\s+([\d.]+)%\s+of\s+(?:your\s+)?"
    r"(Strength|Intelligence|Mana)",
    re.IGNORECASE,
)

# `+5% of all Stats from Items` (Blinking Abyss, Sundering Echo).
_PASSIVE_ALL_STATS = re.compile(
    r"\+\s*([\d.]+)%\s+of all Stats from Items", re.IGNORECASE
)

# Anti-heal is written as `Apply 25% Healing Reduction for 5s`, with no leading
# plus, so the pattern above misses every one of the four items that have it.
# It gets its own because there is no ambiguity to guard against: nothing in the
# game applies healing reduction to *you*, so an unsigned number here is always
# a gain, which is not true of the stats above — "-8% Attack Speed" is a real
# line, and matching an unsigned percentage there would read a debuff as a buff.
_PASSIVE_ANTI_HEAL = re.compile(r"([\d.]+)%\s+Healing Reduction", re.IGNORECASE)


def carries_anti_heal(item: Item) -> bool:
    """Whether this item brings healing reduction to a build."""
    return _PASSIVE_ANTI_HEAL.search(item.passive or "") is not None


def passive_stats(item: Item, base: "Smite2Stats" = None) -> "Smite2Stats":
    """The stats an item's passive grants, as far as they can be read.

    `base` is the rest of the build, needed for the passives that grant a
    fraction of what you already have. Without it those are skipped rather than
    guessed at.

    The adaptive pair is deliberately not re-read here — `item_stats` already
    counts it, and it is not conditional the way the rest of a passive is.
    """
    stats = Smite2Stats()
    text = item.passive or ""
    if not text:
        return stats

    # Strip the adaptive sentence so its "+30 Strength" is not counted twice.
    text = _ADAPTIVE.sub("", text)

    for amount, name in _PASSIVE_FLAT.findall(text):
        attribute = _PASSIVE_NAMES.get(name.strip().lower())
        if attribute is not None:
            stats.add_flat(attribute, float(amount))
        elif name.strip().lower().startswith("protection"):
            stats.add_flat(ItemAttribute.PHYSICAL_PROTECTION, float(amount))
            stats.add_flat(ItemAttribute.MAGICAL_PROTECTION, float(amount))

    for amount, name in _PASSIVE_PERCENT.findall(text):
        attribute = _PASSIVE_NAMES.get(name.strip().lower())
        if attribute is not None:
            stats.add_percent(attribute, float(amount) / 100.0)

    anti_heal = _PASSIVE_ANTI_HEAL.search(text)
    if anti_heal:
        stats.add_percent(ItemAttribute.HEAL_REDUCTION, float(anti_heal.group(1)) / 100.0)

    if base is not None:
        for granted, ratio, source in _PASSIVE_SCALED.findall(text):
            target = _PASSIVE_NAMES[granted.strip().lower()]
            from_stat = _PASSIVE_NAMES[source.strip().lower()]
            stats.add_flat(target, base.get(from_stat) * float(ratio) / 100.0)

        for ratio in _PASSIVE_ALL_STATS.findall(text):
            share = float(ratio) / 100.0
            for attribute, value in base.flat.items():
                stats.add_flat(attribute, value * share)
            for attribute, value in base.percent.items():
                stats.add_percent(attribute, value * share)

    return stats


class Smite2Stats:
    """Flat and percentage totals for a god and a set of items.

    Two dicts rather than one because the same attribute legitimately arrives
    both ways: Penetration is flat on ten items and a percentage on twelve, and
    they are capped separately and applied in a fixed order.

    `overcapped` records what a stat would have reached before the cap took it
    back, so a build that wastes a slot on Penetration it cannot use can say so
    instead of silently reporting the cap as if it were the goal.
    """

    def __init__(self):
        self.flat: Dict[ItemAttribute, float] = {}
        self.percent: Dict[ItemAttribute, float] = {}
        self.overcapped: Dict[ItemAttribute, float] = {}

    def add_flat(self, attribute: ItemAttribute, value: float) -> None:
        self.flat[attribute] = self.flat.get(attribute, 0.0) + value

    def add_percent(self, attribute: ItemAttribute, value: float) -> None:
        self.percent[attribute] = self.percent.get(attribute, 0.0) + value

    def get(self, attribute: ItemAttribute) -> float:
        return self.flat.get(attribute, 0.0)

    def get_percent(self, attribute: ItemAttribute) -> float:
        return self.percent.get(attribute, 0.0)

    def apply_caps(self) -> "Smite2Stats":
        """Clamp every capped stat, remembering what was thrown away."""
        for attribute, cap in FLAT_CAPS.items():
            value = self.flat.get(attribute)
            if value is not None and value > cap:
                self.overcapped[attribute] = value
                self.flat[attribute] = cap
        for attribute, cap in PERCENT_CAPS.items():
            value = self.percent.get(attribute)
            if value is not None and value > cap:
                # Flat and percentage overcapping of the same attribute is
                # possible; the percentage is the one worth reporting because
                # it is the one players stack past the cap.
                self.overcapped[attribute] = value
                self.percent[attribute] = cap
        return self

    @property
    def attack_damage(self) -> float:
        """One basic attack, before the target's Plating and protections."""
        return (
            self.get(ItemAttribute.BASIC_ATTACK_POWER)
            + STRENGTH_ATTACK_SCALING * self.get(ItemAttribute.STRENGTH)
            + INTELLIGENCE_ATTACK_SCALING * self.get(ItemAttribute.INTELLIGENCE)
        )

    @property
    def attacks_per_second(self) -> float:
        """Swings a second. The god's own curve is a percentage bonus too."""
        bonus = (
            self.get(ItemAttribute.ATTACK_SPEED) / 100.0
            + self.get_percent(ItemAttribute.ATTACK_SPEED)
        )
        return BASE_ATTACKS_PER_SECOND * (1.0 + bonus)

    @property
    def cooldown_reduction(self) -> float:
        return cooldown_reduction(self.get(ItemAttribute.COOLDOWN_RATE))


def _damage_stat_for(god: God) -> ItemAttribute:
    """Which stat an adaptive item defaults to with nothing to compare against.

    The god's own scaling, and Strength for hybrids — the rule the game states.
    `scaling` rather than `type` because they disagree: damage type is what a
    god's attacks *deal*, and Smite 2 says plainly that building Strength does
    not make a god deal physical damage, nor the reverse.
    """
    if god is not None:
        scaling = getattr(god, "scaling", None)
        if scaling == "int":
            return ItemAttribute.INTELLIGENCE
        if scaling == "str" or scaling == "hybrid":
            return ItemAttribute.STRENGTH
        if god.type is GodType.MAGICAL:
            return ItemAttribute.INTELLIGENCE
    return ItemAttribute.STRENGTH


def item_stats(items: Iterable[Item], god: God = None) -> Smite2Stats:
    """Totals across `items` alone, with adaptive stats resolved.

    Adaptive items pick the damage stat the *rest* of the build already has
    more of, so they are resolved in a second pass over a first pass that
    ignores them. An all-adaptive build has nothing to compare and falls back
    to the god's own scaling, which is what the game does.
    """
    stats = Smite2Stats()
    items = list(items)

    for item in items:
        for prop in item.item_properties or []:
            attribute = _ALIASES.get(prop.attribute, prop.attribute)
            if prop.flat_value is not None:
                stats.add_flat(attribute, prop.flat_value)
            if prop.percent_value is not None:
                stats.add_percent(attribute, prop.percent_value)

    adaptive = [(item, adaptive_stat(item)) for item in items]
    adaptive = [(item, pair) for item, pair in adaptive if pair is not None]
    if adaptive:
        strength = stats.get(ItemAttribute.STRENGTH)
        intelligence = stats.get(ItemAttribute.INTELLIGENCE)
        if strength > intelligence:
            chosen = ItemAttribute.STRENGTH
        elif intelligence > strength:
            chosen = ItemAttribute.INTELLIGENCE
        else:
            chosen = _damage_stat_for(god)
        for _item, (as_strength, as_intelligence) in adaptive:
            stats.add_flat(
                chosen,
                as_strength if chosen is ItemAttribute.STRENGTH else as_intelligence,
            )

    return stats


def build_stats(god: God, items: Iterable[Item], level: int = 20) -> Smite2Stats:
    """A god at `level` holding `items`, capped.

    The god contributes health, mana, both protections, the regens, movement
    speed, base attack power and base attack speed; it contributes no Strength
    or Intelligence, because in Smite 2 gods have none — every point of both
    comes from the build.
    """
    stats = item_stats(items, god)

    if god is not None:
        for attribute in _GOD_BASE_STATS:
            at_level = god.get_stat_at_level(attribute, level)
            if at_level:
                stats.add_flat(attribute, at_level)

    return stats.apply_caps()


# What a god's published curves cover. Listed rather than iterating every
# ItemAttribute so a stat that only items grant never picks up a phantom base
# value from a curve that happens to exist.
_GOD_BASE_STATS: Tuple[ItemAttribute, ...] = (
    ItemAttribute.HEALTH,
    ItemAttribute.MANA,
    ItemAttribute.HP5,
    ItemAttribute.MP5,
    ItemAttribute.PHYSICAL_PROTECTION,
    ItemAttribute.MAGICAL_PROTECTION,
    ItemAttribute.MOVEMENT_SPEED,
    ItemAttribute.ATTACK_SPEED,
    ItemAttribute.BASIC_ATTACK_POWER,
)


# The regens are per second in Smite 2, not per five, so the Smite 1 display
# names would be wrong by a factor of five. The rest read fine as-is.
_DISPLAY_NAMES: Dict[ItemAttribute, str] = {
    ItemAttribute.HP5: "Health Regen",
    ItemAttribute.MP5: "Mana Regen",
    ItemAttribute.HEALTH_REGEN: "Health Regen",
    ItemAttribute.MANA_REGEN: "Mana Regen",
    ItemAttribute.BASIC_ATTACK_POWER: "Attack Damage",
    ItemAttribute.CRITICAL_CHANCE: "Critical Chance",
    ItemAttribute.COOLDOWN_RATE: "Cooldown Rate",
}


def display_name(attribute: ItemAttribute) -> str:
    return _DISPLAY_NAMES.get(attribute, attribute.display_name)


# Damage first, then defence, then utility — the order the store groups them
# in, so a build reads the way the shop does rather than alphabetically.
_STAT_ORDER: Tuple[ItemAttribute, ...] = (
    ItemAttribute.STRENGTH,
    ItemAttribute.INTELLIGENCE,
    ItemAttribute.BASIC_ATTACK_POWER,
    ItemAttribute.ATTACK_SPEED,
    ItemAttribute.CRITICAL_CHANCE,
    ItemAttribute.PENETRATION,
    ItemAttribute.LIFESTEAL,
    ItemAttribute.ECHO,
    ItemAttribute.HEALTH,
    ItemAttribute.PHYSICAL_PROTECTION,
    ItemAttribute.MAGICAL_PROTECTION,
    ItemAttribute.PLATED,
    ItemAttribute.DAMPENING,
    ItemAttribute.HP5,
    ItemAttribute.MANA,
    ItemAttribute.MP5,
    ItemAttribute.COOLDOWN_RATE,
    ItemAttribute.TENACITY,
    ItemAttribute.MOVEMENT_SPEED,
    ItemAttribute.PATHFINDING,
    ItemAttribute.HEAL_REDUCTION,
)

_PERCENT_STATS = (
    ItemAttribute.ATTACK_SPEED,
    ItemAttribute.CRITICAL_CHANCE,
    ItemAttribute.LIFESTEAL,
    ItemAttribute.PENETRATION,
    ItemAttribute.HEAL_REDUCTION,
)


def total_cost(items: Iterable[Item]) -> int:
    """What a build costs, from the wiki's stated totals.

    Smite 2 publishes each item's full cost including its components, so unlike
    Smite 1 there is no recipe walk to do — and no way to do one, since its
    recipes fork and `parent_item_id` only holds the first branch.
    """
    return sum(
        item.total_cost if item.total_cost is not None else (item.price or 0)
        for item in items
    )


def describe_build(god: God, items: List[Item], level: int = 20) -> str:
    """What a build gives a god, in the terms the game uses.

    Two blocks: the stats the items add up to, then what those stats mean —
    the cooldown reduction the Rate buys, what one attack hits for, and how
    much damage the god can absorb. The second block is the point; the first is
    only a ledger, and a ledger is what this used to be on its own.
    """
    build = list(items)
    items_only = item_stats(build, god).apply_caps()
    totals = build_stats(god, build, level)

    lines: List[str] = [f"**Stats** _(Total Cost - {total_cost(build):,})_:\n"]

    for attribute in _STAT_ORDER:
        flat = items_only.flat.get(attribute, 0.0)
        percent = items_only.percent.get(attribute, 0.0)
        if not flat and not percent:
            continue
        name = display_name(attribute)
        # Penetration is the one stat that is genuinely both, so it says which.
        both = bool(flat) and bool(percent)
        if flat:
            label = f"Flat {name}" if both else name
            suffix = _at_level(totals, attribute, level, percent=False)
            lines.append(f"**{label}**: {flat:,.6g}{suffix}")
        if percent:
            label = f"Percent {name}" if both else name
            suffix = _at_level(totals, attribute, level, percent=True)
            lines.append(f"**{label}**: {percent:.0%}{suffix}")

    capped = [
        display_name(attribute)
        for attribute in _STAT_ORDER
        if attribute in items_only.overcapped
    ]
    if capped:
        lines.append(f"\n_Over the cap on {_join(capped)} — those points do nothing._")

    lines.append(f"\n**At level {level}**:")
    health = totals.get(ItemAttribute.HEALTH)
    physical = totals.get(ItemAttribute.PHYSICAL_PROTECTION)
    magical = totals.get(ItemAttribute.MAGICAL_PROTECTION)
    if health:
        lines.append(
            f"Takes **{effective_health(health, physical):,.0f}** physical or "
            f"**{effective_health(health, magical):,.0f}** magical damage to kill "
            f"({health:,.0f} health behind {physical:,.0f}/{magical:,.0f} protections)."
        )
    attack = totals.attack_damage
    if attack:
        crit = items_only.percent.get(ItemAttribute.CRITICAL_CHANCE, 0.0)
        crit_str = (
            f", {crit:.0%} of them for **{attack * CRITICAL_MULTIPLIER:,.0f}**"
            if crit
            else ""
        )
        lines.append(
            f"Attacks hit for **{attack:,.0f}** at "
            f"**{totals.attacks_per_second:.2f}/s**{crit_str}."
        )
    rate = totals.get(ItemAttribute.COOLDOWN_RATE)
    if rate:
        lines.append(
            f"Abilities come back **{cooldown_reduction(rate):.1%}** sooner "
            f"({rate:,.6g} Cooldown Rate)."
        )

    return "\n".join(lines)


def _at_level(
    totals: Smite2Stats, attribute: ItemAttribute, level: int, percent: bool
) -> str:
    """The `(x @ Level 20)` tail, where a god's own base makes it meaningful."""
    if percent or attribute not in _GOD_BASE_STATS:
        return ""
    total = totals.get(attribute)
    if not total:
        return ""
    return f" _({total:,.6g} @ Level {level})_"


def _join(names: List[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"
