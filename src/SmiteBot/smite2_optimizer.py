"""Choosing a Smite 2 build, and choosing a random one that still makes sense.

Smite 1's `BuildOptimizer` enumerates combinations of a pre-trimmed item list
and keeps the ones that clear hand-written stat targets per archetype, where an
archetype is a (class, lane) pair — `MID_MAGE`, `SUPPORT_GUARDIAN`. Smite 2 has
no classes to hang that off: a god has a lane or two, a damage type, and a
handful of descriptive specs, and the same god legitimately builds Strength or
Intelligence depending on which of its abilities you intend to use. So this
scores rather than filters.

What it scores against is measured rather than invented: the mean stat totals
of winning Conquest builds in the corpus, per lane and per damage stat. The
optimizer's job is to fill that shape as well as six items can.

The scoring is saturating on purpose. Each profile is a *target* amount of each
stat, and a stat is worth its weight up to that target and nothing after — so
the second Penetration item is worth much less than the first, the seventh
protection item is worth nothing, and a build lands spread across the stats
that lane actually wants without any rule saying "at most two of these". Caps
the game itself enforces are folded in as ceilings on the targets, which is why
nothing here ever recommends 60% Penetration.

Selection is greedy on marginal gain, then a swap pass until no single
substitution improves the build. On 133 tier-3 items that is a few thousand
score evaluations rather than the 2.3 million combinations Smite 1's optimizer
walks, and the swap pass is what buys back most of the difference between
greedy and exhaustive.

How good is it? Against the corpus, the six items this picks share a mean of
1.95 of 6 with the six most-won items for the same god in the same lane, over
the 56 gods with enough recorded Conquest wins to compare — median 2, and only
4 gods with nothing in common. `src/tools/smite2_accuracy.py` re-measures it.
That is a long way from reproducing the meta, and the reason is known rather
than mysterious: most of what separates the corpus's favourite items from their
neighbours is passive text this does not read. See `score`.
"""

from __future__ import annotations

import random
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import smite2_stats
from god import God
from god_types import GodType
from item import Item, ItemAttribute, ItemType
from HirezAPI import PlayerRole

# What a winning build in each lane is made of.
#
# Measured, not guessed: these are the mean stat totals of winning Conquest and
# Ranked Conquest builds in the corpus, per lane and per damage stat. Arena and
# Assault are excluded and are over three quarters of the corpus — they have no
# lanes, tracker.gg labels a role anyway, and an "Arena support" is a full
# damage build, so including them describes no lane at all.
#
# Split by damage stat because the two are different builds, not the same build
# with a different number in one slot: a Strength carry averages 81% attack
# speed and 47 attack damage, an Intelligence carry averages 13% and none, and
# the average of the two is a build nobody plays.
#
# Read as targets, in each stat's own units. An item is worth the fraction of a
# target it fills and nothing beyond it, so this shape is what the optimizer
# converges on without any rule about how many of each kind of item to take.
_FLAT = "flat"
_PERCENT = "percent"

_PROFILES: Dict[Tuple[PlayerRole, ItemAttribute], Dict[str, Dict[ItemAttribute, float]]] = {
    (PlayerRole.CARRY, ItemAttribute.INTELLIGENCE): {
        _FLAT: {
            ItemAttribute.STRENGTH: 4.85,
            ItemAttribute.INTELLIGENCE: 380,
            ItemAttribute.BASIC_ATTACK_POWER: 1.19,
            ItemAttribute.PENETRATION: 15.9,
            ItemAttribute.ECHO: 1.45,
            ItemAttribute.HEALTH: 2.09,
            ItemAttribute.MANA: 760,
            ItemAttribute.MP5: 8.05,
            ItemAttribute.COOLDOWN_RATE: 17.7,
        },
        _PERCENT: {
            ItemAttribute.PENETRATION: 0.231,
            ItemAttribute.ATTACK_SPEED: 0.129,
            ItemAttribute.LIFESTEAL: 0.0577,
        },
    },  # 93 builds
    (PlayerRole.CARRY, ItemAttribute.STRENGTH): {
        _FLAT: {
            ItemAttribute.STRENGTH: 144,
            ItemAttribute.INTELLIGENCE: 4.44,
            ItemAttribute.BASIC_ATTACK_POWER: 47.1,
            ItemAttribute.PENETRATION: 1.69,
            ItemAttribute.ECHO: 1.85,
            ItemAttribute.HEALTH: 46.2,
            ItemAttribute.PHYSICAL_PROTECTION: 1.31,
            ItemAttribute.MAGICAL_PROTECTION: 1.31,
            ItemAttribute.MANA: 125,
            ItemAttribute.MP5: 1.41,
            ItemAttribute.COOLDOWN_RATE: 3.62,
        },
        _PERCENT: {
            ItemAttribute.PENETRATION: 0.197,
            ItemAttribute.ATTACK_SPEED: 0.809,
            ItemAttribute.CRITICAL_CHANCE: 0.184,
            ItemAttribute.LIFESTEAL: 0.063,
        },
    },  # 290 builds
    (PlayerRole.JUNGLE, ItemAttribute.INTELLIGENCE): {
        _FLAT: {
            ItemAttribute.STRENGTH: 3.46,
            ItemAttribute.INTELLIGENCE: 409,
            ItemAttribute.PENETRATION: 19.4,
            ItemAttribute.ECHO: 3.8,
            ItemAttribute.HEALTH: 3.69,
            ItemAttribute.PHYSICAL_PROTECTION: 0.534,
            ItemAttribute.MAGICAL_PROTECTION: 0.534,
            ItemAttribute.MANA: 740,
            ItemAttribute.MP5: 7.9,
            ItemAttribute.COOLDOWN_RATE: 22.3,
        },
        _PERCENT: {
            ItemAttribute.PENETRATION: 0.215,
            ItemAttribute.ATTACK_SPEED: 0.0206,
            ItemAttribute.LIFESTEAL: 0.0425,
        },
    },  # 68 builds
    (PlayerRole.JUNGLE, ItemAttribute.STRENGTH): {
        _FLAT: {
            ItemAttribute.STRENGTH: 218,
            ItemAttribute.INTELLIGENCE: 1.91,
            ItemAttribute.BASIC_ATTACK_POWER: 6.84,
            ItemAttribute.PENETRATION: 11.4,
            ItemAttribute.ECHO: 10.9,
            ItemAttribute.HEALTH: 91.5,
            ItemAttribute.PHYSICAL_PROTECTION: 14,
            ItemAttribute.MAGICAL_PROTECTION: 12.5,
            ItemAttribute.HP5: 0.96,
            ItemAttribute.MANA: 618,
            ItemAttribute.MP5: 6.11,
            ItemAttribute.COOLDOWN_RATE: 28.8,
        },
        _PERCENT: {
            ItemAttribute.PENETRATION: 0.25,
            ItemAttribute.ATTACK_SPEED: 0.122,
            ItemAttribute.CRITICAL_CHANCE: 0.0688,
            ItemAttribute.LIFESTEAL: 0.0274,
        },
    },  # 195 builds
    (PlayerRole.MID, ItemAttribute.INTELLIGENCE): {
        _FLAT: {
            ItemAttribute.STRENGTH: 0.505,
            ItemAttribute.INTELLIGENCE: 405,
            ItemAttribute.PENETRATION: 18.4,
            ItemAttribute.ECHO: 3.31,
            ItemAttribute.HEALTH: 9.3,
            ItemAttribute.MAGICAL_PROTECTION: 0.765,
            ItemAttribute.MANA: 723,
            ItemAttribute.MP5: 7.61,
            ItemAttribute.COOLDOWN_RATE: 24.4,
        },
        _PERCENT: {
            ItemAttribute.PENETRATION: 0.222,
            ItemAttribute.ATTACK_SPEED: 0.0222,
            ItemAttribute.LIFESTEAL: 0.0494,
        },
    },  # 470 builds
    (PlayerRole.SOLO, ItemAttribute.INTELLIGENCE): {
        _FLAT: {
            ItemAttribute.INTELLIGENCE: 352,
            ItemAttribute.BASIC_ATTACK_POWER: 1.4,
            ItemAttribute.PENETRATION: 14,
            ItemAttribute.ECHO: 1.61,
            ItemAttribute.HEALTH: 142,
            ItemAttribute.PHYSICAL_PROTECTION: 19.9,
            ItemAttribute.MAGICAL_PROTECTION: 21.1,
            ItemAttribute.TENACITY: 2.04,
            ItemAttribute.HP5: 1.12,
            ItemAttribute.MANA: 660,
            ItemAttribute.MP5: 7.44,
            ItemAttribute.COOLDOWN_RATE: 24.6,
        },
        _PERCENT: {
            ItemAttribute.PENETRATION: 0.163,
            ItemAttribute.ATTACK_SPEED: 0.0481,
            ItemAttribute.LIFESTEAL: 0.0542,
        },
    },  # 77 builds
    (PlayerRole.SOLO, ItemAttribute.STRENGTH): {
        _FLAT: {
            ItemAttribute.STRENGTH: 44.8,
            ItemAttribute.BASIC_ATTACK_POWER: 10.6,
            ItemAttribute.PENETRATION: 1.41,
            ItemAttribute.ECHO: 1.1,
            ItemAttribute.HEALTH: 727,
            ItemAttribute.PHYSICAL_PROTECTION: 113,
            ItemAttribute.MAGICAL_PROTECTION: 104,
            ItemAttribute.PLATED: 1.72,
            ItemAttribute.DAMPENING: 0.841,
            ItemAttribute.TENACITY: 2.93,
            ItemAttribute.HP5: 4.17,
            ItemAttribute.MANA: 237,
            ItemAttribute.MP5: 4.37,
            ItemAttribute.COOLDOWN_RATE: 29.1,
        },
        _PERCENT: {
            ItemAttribute.PENETRATION: 0.038,
            ItemAttribute.ATTACK_SPEED: 0.199,
            ItemAttribute.CRITICAL_CHANCE: 0.0148,
            ItemAttribute.LIFESTEAL: 0.0105,
        },
    },  # 84 builds
    (PlayerRole.SUPPORT, ItemAttribute.INTELLIGENCE): {
        _FLAT: {
            ItemAttribute.INTELLIGENCE: 196,
            ItemAttribute.PENETRATION: 8.58,
            ItemAttribute.ECHO: 1.54,
            ItemAttribute.HEALTH: 422,
            ItemAttribute.PHYSICAL_PROTECTION: 67.4,
            ItemAttribute.MAGICAL_PROTECTION: 59.3,
            ItemAttribute.PLATED: 0.796,
            ItemAttribute.DAMPENING: 0.54,
            ItemAttribute.TENACITY: 3.97,
            ItemAttribute.HP5: 2.63,
            ItemAttribute.MANA: 587,
            ItemAttribute.MP5: 6.44,
            ItemAttribute.COOLDOWN_RATE: 34.8,
        },
        _PERCENT: {
            ItemAttribute.PENETRATION: 0.0719,
        },
    },  # 31 builds
    (PlayerRole.SUPPORT, ItemAttribute.STRENGTH): {
        _FLAT: {
            ItemAttribute.STRENGTH: 23.9,
            ItemAttribute.BASIC_ATTACK_POWER: 2.48,
            ItemAttribute.PENETRATION: 0.504,
            ItemAttribute.ECHO: 1.01,
            ItemAttribute.HEALTH: 697,
            ItemAttribute.PHYSICAL_PROTECTION: 120,
            ItemAttribute.MAGICAL_PROTECTION: 104,
            ItemAttribute.PLATED: 0.858,
            ItemAttribute.DAMPENING: 2.39,
            ItemAttribute.TENACITY: 8.06,
            ItemAttribute.HP5: 4.62,
            ItemAttribute.MANA: 241,
            ItemAttribute.MP5: 3.97,
            ItemAttribute.COOLDOWN_RATE: 32.8,
        },
        _PERCENT: {
            ItemAttribute.PENETRATION: 0.0155,
            ItemAttribute.ATTACK_SPEED: 0.0587,
            ItemAttribute.CRITICAL_CHANCE: 0.0137,
            ItemAttribute.LIFESTEAL: 0.0108,
        },
    },  # 40 builds
}

# What one item's worth of a stat looks like, so a contribution can be compared
# across stats measured in different units. Scoring the raw fraction of a target
# instead does not work: a profile with a 10-health target hands full marks to
# any item with any health at all, and a 405-Intelligence target pays a fifth of
# that for the best Intelligence item in the game.
_FLAT_REFERENCE: Dict[ItemAttribute, float] = {
    ItemAttribute.STRENGTH: 45.0,
    ItemAttribute.INTELLIGENCE: 75.0,
    ItemAttribute.BASIC_ATTACK_POWER: 25.0,
    ItemAttribute.HEALTH: 350.0,
    ItemAttribute.MANA: 250.0,
    ItemAttribute.HP5: 5.0,
    ItemAttribute.MP5: 4.0,
    ItemAttribute.PHYSICAL_PROTECTION: 40.0,
    ItemAttribute.MAGICAL_PROTECTION: 40.0,
    ItemAttribute.PLATED: 8.0,
    ItemAttribute.DAMPENING: 6.0,
    ItemAttribute.COOLDOWN_RATE: 15.0,
    ItemAttribute.TENACITY: 12.0,
    ItemAttribute.ECHO: 15.0,
    ItemAttribute.PENETRATION: 8.0,
    ItemAttribute.MOVEMENT_SPEED: 20.0,
    ItemAttribute.PATHFINDING: 8.0,
}

_PERCENT_REFERENCE: Dict[ItemAttribute, float] = {
    ItemAttribute.ATTACK_SPEED: 0.25,
    ItemAttribute.CRITICAL_CHANCE: 0.20,
    ItemAttribute.LIFESTEAL: 0.08,
    ItemAttribute.PENETRATION: 0.15,
    ItemAttribute.HEAL_REDUCTION: 0.25,
}

# Every stat counts for one item's worth of itself, and the profile above says
# how many items' worth a build wants. There is deliberately no table here
# emphasising the damage stat, Penetration and protections over the rest: one
# was written, and scored *worse* than uniform against the corpus — 1.68 mean
# item overlap against 1.95. The profile already encodes what matters, because a
# lane that wants Penetration shows it as a bigger Penetration target, and a
# second opinion on top of the measurement only argues with it.

# The wiki's spec words, as a nudge on top of the measured lane shape. A god is
# tagged with two or three and they are the only published statement of what it
# is meant to do, which is what separates two mid-laners who build nothing
# alike. Applied as emphasis rather than as a change to the targets, so a spec
# can reorder a build's priorities without inventing a shape the corpus never
# shows.
#
# These carry more weight than their size suggests: without them the whole lane
# builds identically and mean item overlap with the corpus falls from 1.95 to
# 1.20, the largest single effect measured on this model.
_SPEC_EMPHASIS: Dict[str, Dict[ItemAttribute, float]] = {
    "sharpshooter": {ItemAttribute.CRITICAL_CHANCE: 0.8, ItemAttribute.BASIC_ATTACK_POWER: 0.6},
    "constant damage": {ItemAttribute.ATTACK_SPEED: 0.8, ItemAttribute.LIFESTEAL: 0.4},
    "burst damage": {ItemAttribute.PENETRATION: 0.6, ItemAttribute.ECHO: 0.5},
    "nuker": {ItemAttribute.PENETRATION: 0.6, ItemAttribute.COOLDOWN_RATE: 0.4},
    "slayer": {ItemAttribute.PENETRATION: 0.6, ItemAttribute.LIFESTEAL: 0.3},
    "execute": {ItemAttribute.PENETRATION: 0.5},
    "sniper": {ItemAttribute.BASIC_ATTACK_POWER: 0.5},
    "tank": {
        ItemAttribute.HEALTH: 0.8,
        ItemAttribute.PHYSICAL_PROTECTION: 0.6,
        ItemAttribute.MAGICAL_PROTECTION: 0.6,
    },
    "brawler": {ItemAttribute.HEALTH: 0.6, ItemAttribute.HP5: 0.4},
    "sustain": {ItemAttribute.HP5: 0.6, ItemAttribute.LIFESTEAL: 0.4},
    "lockdown": {ItemAttribute.COOLDOWN_RATE: 0.4, ItemAttribute.TENACITY: 0.3},
    "crowd control": {ItemAttribute.COOLDOWN_RATE: 0.4, ItemAttribute.TENACITY: 0.3},
    "area control": {ItemAttribute.COOLDOWN_RATE: 0.3},
    "healing": {ItemAttribute.COOLDOWN_RATE: 0.6, ItemAttribute.MANA: 0.4, ItemAttribute.MP5: 0.4},
    "shielding": {ItemAttribute.COOLDOWN_RATE: 0.5, ItemAttribute.MANA: 0.3},
    "buffs": {ItemAttribute.COOLDOWN_RATE: 0.4},
    "mobile": {ItemAttribute.MOVEMENT_SPEED: 0.5, ItemAttribute.PATHFINDING: 0.3},
    "mobility": {ItemAttribute.MOVEMENT_SPEED: 0.5, ItemAttribute.PATHFINDING: 0.3},
    "global": {ItemAttribute.COOLDOWN_RATE: 0.3},
    "pressure": {ItemAttribute.PENETRATION: 0.3},
    "utility": {ItemAttribute.COOLDOWN_RATE: 0.3},
    "stealth": {ItemAttribute.MOVEMENT_SPEED: 0.3},
}

# Everything a build's six core slots can be filled from. Tier is the only
# field that distinguishes them: relics, consumables, curios and god-specific
# items have no tier at all, and starters are tier 1 or 2.
CORE_TIER = 3

# What six items can actually cost. Measured over 8,549 winning builds in the
# corpus: mean 15,971, standard deviation 558, and only a handful above 18,000.
# A game does not hand out enough gold for six of the most expensive items in
# the catalogue, so an optimizer without this recommends builds nobody can
# finish — and it recommends them *preferentially*, because the expensive items
# are exactly the ones carrying the biggest raw numbers.
DEFAULT_BUDGET = 16_500

# How much of a passive's readable stat grant to count. They are conditional
# almost without exception — on a kill, below 40% health, while a stack holds —
# so counting them in full would rank a situational 20% Penetration alongside
# 20% on the stat line. Swept against the corpus: 0.75 is the best of
# 0.25/0.5/0.75/1.0/1.5, and counting them at full value is worse than ignoring
# them entirely.
PASSIVE_DISCOUNT = 0.75

# There is no blanket credit for merely having a passive, though the temptation
# is real: 96% of the item picks in every winning build in the corpus are items
# with a passive. It was tried, at 0.15, 0.3 and 0.6, and every value scored
# worse than none — 1.61, 1.62 and 1.38 against 1.95. The reason is that it
# cannot tell a great passive from a filler one, so it mostly promotes items
# whose passive says nothing, and 102 of 133 tier-3 items have a passive anyway,
# which makes it close to a constant. Reading the passive is the only thing that
# helps; see `smite2_stats.passive_stats`, and the note above `score`.


def damage_stat(god: God, role: PlayerRole = None) -> ItemAttribute:
    """The damage stat this god builds, in this lane.

    Three signals, best first. The game's own item-store filter for the lane is
    the authority — it is what a player sees when they open the shop, and it is
    per lane precisely because a hybrid god builds differently depending on
    where it is. Failing that the god's scaling keyword, and failing that its
    damage type.

    Damage type is last for a reason: it is wrong often enough to matter.
    Neith and Danzaburou both have physical basic attacks and are both built
    with Intelligence in most of the lanes they are played in, so a model keyed
    on damage type hands them a Strength build the corpus never plays.
    """
    if god is not None:
        by_role = getattr(god, "role_scaling", None) or {}
        if role is not None and by_role.get(role) is not None:
            return by_role[role]

        scaling = getattr(god, "scaling", None)
        if scaling == "int":
            return ItemAttribute.INTELLIGENCE
        if scaling == "str":
            return ItemAttribute.STRENGTH
        if scaling == "hybrid":
            # No lane to disambiguate a hybrid, so fall through to the damage
            # type, which is the better half of a coin flip.
            pass

        if god.type is GodType.MAGICAL:
            return ItemAttribute.INTELLIGENCE
    return ItemAttribute.STRENGTH


def primary_role(god: God, role: PlayerRole = None) -> PlayerRole:
    """Which lane to build for.

    An explicit choice wins; otherwise the god's first published position, and
    mid for a god the wiki lists nowhere — a neutral choice rather than a
    defensive one, since a god with no position is usually one too new to have
    been written up rather than a tank.
    """
    if role is not None:
        return role
    positions = list(getattr(god, "positions", None) or [])
    return positions[0] if positions else PlayerRole.MID


class Smite2BuildOptimizer:
    """Scores and assembles Smite 2 builds for one god in one lane."""

    def __init__(
        self,
        god: God,
        items: Dict[int, Item],
        role: PlayerRole = None,
        budget: int = DEFAULT_BUDGET,
    ):
        self.god = god
        self.role = primary_role(god, role)
        self.damage_stat = damage_stat(god, self.role)
        self.budget = budget
        self.__items = items
        profile = self.__profile()
        self.flat_targets = self.__capped(profile[_FLAT], smite2_stats.FLAT_CAPS)
        self.percent_targets = self.__capped(
            profile[_PERCENT], smite2_stats.PERCENT_CAPS
        )
        self.emphasis = self.__emphasis()

    def __profile(self) -> Dict[str, Dict[ItemAttribute, float]]:
        """The measured shape for this lane and damage stat.

        A lane the corpus has never seen this stat in falls back to the other
        stat's shape for the same lane, which is a much closer relative than
        the same stat in a different lane: a Strength support and an
        Intelligence support are both mostly protections.
        """
        other = (
            ItemAttribute.INTELLIGENCE
            if self.damage_stat is ItemAttribute.STRENGTH
            else ItemAttribute.STRENGTH
        )
        for key in (
            (self.role, self.damage_stat),
            (self.role, other),
            (PlayerRole.MID, ItemAttribute.INTELLIGENCE),
        ):
            if key in _PROFILES:
                return _PROFILES[key]
        raise KeyError(self.role)

    @staticmethod
    def __capped(
        targets: Dict[ItemAttribute, float], caps: Dict[ItemAttribute, float]
    ) -> Dict[ItemAttribute, float]:
        """A profile's targets, never above what the game lets you have.

        The measured means are already below every cap — nobody averages 40%
        Penetration — but a target above a cap would ask the optimizer to buy
        points that do nothing, so the ceiling is enforced rather than assumed.
        """
        out = dict(targets)
        for attribute, cap in caps.items():
            if attribute in out:
                out[attribute] = min(out[attribute], cap)
        return out

    def __emphasis(self) -> Dict[ItemAttribute, float]:
        """One per stat, plus whatever this god's specs ask for."""
        emphasis: Dict[ItemAttribute, float] = {}
        for spec in getattr(self.god, "specs", None) or []:
            for attribute, bonus in _SPEC_EMPHASIS.get(spec.strip().lower(), {}).items():
                emphasis[attribute] = emphasis.get(attribute, 1.0) + bonus
        return emphasis

    def core_items(self) -> List[Item]:
        """The tier-3 pool: everything that may hold one of the six slots.

        An item with neither stats nor an adaptive passive is dropped rather
        than scored at zero, so it can never be picked to fill a slot nothing
        else wanted.
        """
        return [
            item
            for item in self.__items.values()
            if item.tier == CORE_TIER
            and item.active
            and item.type is ItemType.ITEM
            and not item.is_starter
            and (item.item_properties or smite2_stats.adaptive_stat(item))
        ]

    def starters(self) -> List[Item]:
        return [
            item
            for item in self.__items.values()
            if item.is_starter and item.active and item.type is ItemType.ITEM
        ]

    def relics(self) -> List[Item]:
        return [
            item
            for item in self.__items.values()
            if item.type is ItemType.RELIC and item.active
        ]

    def score(self, items: Sequence[Item]) -> float:
        """What a set of items is worth to this god, saturating at the targets.

        Two parts: the stat lines, and the part of each passive that can be read
        as stats, discounted because passives are conditional.

        What this cannot see is the rest of the passive — an execute threshold,
        a slow, a shield, Spear of Desolation refunding cooldowns on a kill.
        Those are a large part of why the corpus's favourite items are its
        favourites, and no amount of weighting the stat lines recovers them. It
        is the biggest known gap in this model rather than a rounding error.
        """
        stats = smite2_stats.item_stats(items, self.god)
        base = self.__stat_score(stats)
        total = base

        for item in items:
            passive = smite2_stats.passive_stats(item, stats)
            if passive.flat or passive.percent:
                # Valued at the margin, on top of what the build already has,
                # so a passive granting Penetration is worth nothing once the
                # build is already at the target.
                gain = self.__stat_score(_merge(stats, passive)) - base
                total += PASSIVE_DISCOUNT * gain

        return total

    def __stat_score(self, stats: smite2_stats.Smite2Stats) -> float:
        """How much of the lane's measured shape these stats fill.

        Each target contributes at most its emphasis, so no single stat can run
        away with the score, and a stat past its target contributes nothing
        further — which is what makes the walk spread a build out.

        Flat and percentage are scored separately because Penetration is
        genuinely both: ten flat and 10% are different purchases against
        different caps, and folding them into one number values flat
        Penetration at fifty times its worth.
        """
        total = 0.0
        for values, targets, reference in (
            (stats.flat, self.flat_targets, _FLAT_REFERENCE),
            (stats.percent, self.percent_targets, _PERCENT_REFERENCE),
        ):
            for attribute, target in targets.items():
                if target <= 0:
                    continue
                value = values.get(attribute, 0.0)
                if not value:
                    continue
                unit = reference.get(attribute)
                if not unit:
                    continue
                # Capped at the target, counted in items' worth. The cap is what
                # stops a build stacking one stat; the unit is what keeps a
                # small target from paying as much as a large one.
                total += self.emphasis.get(attribute, 1.0) * min(value, target) / unit
        return total

    def cost(self, items: Iterable[Item]) -> int:
        return smite2_stats.total_cost(items)

    def marginal(self, build: List[Item], candidate: Item) -> float:
        return self.score(build + [candidate]) - self.score(build)

    def affordable(
        self, build: List[Item], candidate: Item, size: int, pool: List[Item]
    ) -> bool:
        """Whether `candidate` still leaves a build that fits the budget.

        The remaining slots are priced at the cheapest things left in the pool,
        which never rejects a build that could have been afforded — it only
        stops the walk spending its whole budget in the first three slots and
        filling the rest with whatever is left.
        """
        if not self.budget:
            return True
        chosen = build + [candidate]
        remaining = size - len(chosen)
        spent = self.cost(chosen)
        if remaining > 0:
            picked = {item.id for item in chosen}
            cheapest = sorted(
                self.__price(item) for item in pool if item.id not in picked
            )[:remaining]
            spent += sum(cheapest)
        return spent <= self.budget

    def optimize(self, size: int = 6, pool: List[Item] = None) -> List[Item]:
        """The best six items this scoring can find, greedily then by swaps."""
        candidates = list(pool if pool is not None else self.core_items())
        if len(candidates) < size:
            return sorted(candidates, key=self.__sort_key)

        build: List[Item] = []
        for _ in range(size):
            best = max(
                (
                    item
                    for item in candidates
                    if item not in build
                    and self.affordable(build, item, size, candidates)
                ),
                key=lambda item: (self.marginal(build, item), -item.id),
                default=None,
            )
            if best is None:
                break
            build.append(best)

        build = self.__improve(build, candidates)
        return sorted(build, key=self.__sort_key)

    def __improve(self, build: List[Item], candidates: List[Item]) -> List[Item]:
        """Swap one item at a time for as long as it helps.

        Greedy alone is myopic about the targets: an early item can spend most
        of a stat's target and leave a later slot with nothing worth taking.
        One swap pass fixes almost all of that, and passes stop as soon as one
        makes no change, so this is bounded by how wrong greedy was.
        """
        current = self.score(build)
        improved = True
        while improved:
            improved = False
            for index in range(len(build)):
                for candidate in candidates:
                    if candidate in build:
                        continue
                    trial = list(build)
                    trial[index] = candidate
                    if self.budget and self.cost(trial) > self.budget:
                        continue
                    trial_score = self.score(trial)
                    if trial_score > current + 1e-9:
                        build, current, improved = trial, trial_score, True
                        break
                if improved:
                    break
        return build

    def best_starter(self) -> Optional[Item]:
        starters = self.starters()
        if not starters:
            return None
        return max(starters, key=lambda item: (self.score([item]), -item.id))

    def rank(self, items: Iterable[Item] = None) -> List[Tuple[Item, float]]:
        """Every candidate item with its standalone score, best first."""
        pool = list(items if items is not None else self.core_items())
        return sorted(
            ((item, self.score([item])) for item in pool),
            key=lambda pair: (-pair[1], pair[0].id),
        )

    def sample(
        self,
        size: int = 6,
        pool: List[Item] = None,
        candidates_per_slot: int = 8,
        rng: random.Random = None,
    ) -> List[Item]:
        """A random build that still respects the role.

        Same marginal-gain walk as `optimize`, except each slot picks randomly
        among the best few options rather than the single best. That keeps the
        shape of the build — a carry still ends up with attack speed and
        penetration, a support still ends up tanky — while giving a different
        answer every time, which is the whole point of a randomiser.

        Weighted by rank rather than by score, so an item is not effectively
        excluded for being 5% behind the leader, and a slot with one standout
        option still usually takes it.
        """
        rng = rng or random
        candidates = list(pool if pool is not None else self.core_items())
        build: List[Item] = []
        for _ in range(size):
            remaining = [
                item
                for item in candidates
                if item not in build
                and self.affordable(build, item, size, candidates)
            ]
            if not remaining:
                break
            ranked = sorted(
                remaining,
                key=lambda item: (-self.marginal(build, item), item.id),
            )[:candidates_per_slot]
            # 8, 7, 6 … so the best option is eight times as likely as the
            # eighth, and nothing in the shortlist is impossible.
            weights = list(range(len(ranked), 0, -1))
            build.append(rng.choices(ranked, weights=weights, k=1)[0])
        return sorted(build, key=self.__sort_key)

    @staticmethod
    def __price(item: Item) -> int:
        return item.total_cost if item.total_cost is not None else (item.price or 0)

    def __sort_key(self, item: Item) -> Tuple[int, str]:
        """Cheapest first, which is the order a build is actually bought in."""
        return (self.__price(item), item.name)


def _merge(
    first: smite2_stats.Smite2Stats, second: smite2_stats.Smite2Stats
) -> smite2_stats.Smite2Stats:
    """Both sets of totals added together, without touching either."""
    merged = smite2_stats.Smite2Stats()
    for source in (first, second):
        for attribute, value in source.flat.items():
            merged.add_flat(attribute, value)
        for attribute, value in source.percent.items():
            merged.add_percent(attribute, value)
    return merged
