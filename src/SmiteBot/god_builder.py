from __future__ import annotations
import math
import random
import sys
from enum import Enum
from typing import Dict, List, NamedTuple, Set, Tuple

import pandas as pd

import build_engine
import build_path
import build_ranker
import smite2_stats
import team_context
from build_optimizer import BuildOptimizer
from smite2_optimizer import Smite2BuildOptimizer
from god import God
from god_types import GodId, GodRole, GodType
from item import Item, ItemAttribute, ItemType
from player_stats import PlayerStats
from player import Player
from stat_calculator import DamageCalculator, GodBuild
from SmiteProvider import SmiteProvider
from game import Game, id_value, queues_for
from HirezAPI import PlayerRole, QueueId, TierId


class InvalidOptionError(Exception):
    pass


class BuildFailedError(Exception):
    pass


class BuildCommandType(Enum):
    OPTIMIZE = "optimize"
    RANDOM = "random"
    TOP = "top"
    ML = "ml"


class BuildPrioritization(Enum):
    POWER = "power"
    DEFENSE = "defense"


def _tier_or_none(avg_tier) -> TierId | None:
    """The ranked tier an average rounds down to, or None if there isn't one.

    `TierId` starts at BRONZE_V = 1, so 0 is not "the lowest tier" — it is the
    absence of one, which is what an aggregate reports when nothing that fed it
    carried a tier. Smite 2 is entirely in that state.
    """
    try:
        return TierId(math.floor(float(avg_tier)))
    except (TypeError, ValueError):
        return None


class BuildBalance(Enum):
    """How a build splits its slots between surviving and killing.

    Both games take the same three points and mean the same thing by them: the
    share of the build's value spent on defence. What differs is what they are
    applied to — Smite 1 tilts its archetype's weights and relaxes its stat
    targets, Smite 2 tilts the measured profile for the lane — and in both a
    god that was already building correctly is left alone unless asked.
    """

    TANK = "tank"
    BRUISER = "bruiser"
    DAMAGE = "damage"

    @property
    def ratio(self) -> float:
        return {"tank": 0.85, "bruiser": 0.5, "damage": 0.15}[self.value]


class GeneratedBuild(NamedTuple):
    """One generated build, whatever produced it.

    `relics` carries the starter alongside the relic in Smite 2, because both
    sit outside the six core slots and both are rendered in the same strip; the
    embed labels it accordingly.

    `aspect` is Smite 2 only and usually None — 17 gods have no Aspect at all,
    and a random build rolls whether to use one. It is carried here rather than
    folded into the description because the embed does two things with it: the
    words go in the description, and the icon is composited onto the god's.
    """

    build: List[Item]
    relics: List[Item]
    description: str
    aspect: object = None
    # Only `/optimize` produces one. It is carried rather than folded into the
    # description because the embed draws it instead of describing it.
    path: object = None


# Which stats count as offence and which as defence, for `--prioritize`. Smite 1
# answers this from its own item attributes; Smite 2's vocabulary is different
# enough — no Physical/Magical Power, and Plated and Dampening are new — that it
# needs saying separately.
_POWER_STATS = frozenset(
    {
        ItemAttribute.STRENGTH,
        ItemAttribute.INTELLIGENCE,
        ItemAttribute.BASIC_ATTACK_POWER,
        ItemAttribute.ATTACK_SPEED,
        ItemAttribute.CRITICAL_CHANCE,
        ItemAttribute.PENETRATION,
        ItemAttribute.LIFESTEAL,
        ItemAttribute.ECHO,
    }
)

_DEFENSE_STATS = frozenset(
    {
        ItemAttribute.HEALTH,
        ItemAttribute.PHYSICAL_PROTECTION,
        ItemAttribute.MAGICAL_PROTECTION,
        ItemAttribute.PLATED,
        ItemAttribute.DAMPENING,
        ItemAttribute.TENACITY,
        ItemAttribute.HP5,
        ItemAttribute.HEALTH_REGEN,
    }
)


def _prioritized(items: List[Item], prioritization: BuildPrioritization) -> List[Item]:
    """Items carrying at least one stat of the requested kind."""
    wanted = (
        _POWER_STATS
        if prioritization is BuildPrioritization.POWER
        else _DEFENSE_STATS
    )
    return [
        item
        for item in items
        if any(p.attribute in wanted for p in item.item_properties or [])
        # An adaptive item is a damage item whose damage is written in prose.
        or (
            prioritization is BuildPrioritization.POWER
            and smite2_stats.adaptive_stat(item) is not None
        )
    ]


def valid_items_for_god(god: God, items: Dict[int, Item]) -> List[Item]:
    """Every Smite 1 item this god is allowed to build.

    Module-level rather than a `GodBuilder` method so the accuracy harness can
    score the optimizer over exactly the pool the bot gives it. A harness that
    assembles its own pool is measuring a different optimizer, and would go
    quietly wrong the first time one of these rules changed.
    """
    return list(
        filter(
            lambda item: item.type == ItemType.ITEM and item.active and
            # Filter out acorns from non-Ratatoskr gods
            (item.root_item_id != 18703 or god.id == GodId.RATATOSKR) and
            # Filter out Odysseus' Bow from non-physical gods
            (item.id != 10482 or god.type == GodType.PHYSICAL) and
            # Filter out any items that have restricted roles that intersect
            # with the current god's role
            (not any(item.restricted_roles) or god.role not in item.restricted_roles)
            and
            # Elucidate god type from item properties and check intersection
            (
                any(p.attribute.god_type == god.type for p in item.item_properties)
                or all(p.attribute.god_type is None for p in item.item_properties)
            ),
            items.values(),
        )
    )


def _context_note(context) -> str:
    """What the lobby changed about the build, if a lobby was given."""
    described = context.describe() if context is not None else ""
    return f"_{described}_\n\n" if described else ""


def _aspect_string(god: God, aspect) -> str:
    """The Aspect paragraph, or a note that the roll came up without one.

    Said either way on purpose: an Aspect changes how a god plays substantially
    enough to change its role, so "no Aspect" is information about the build
    rather than the absence of it.
    """
    if aspect is None:
        if getattr(god, "aspect", None) is None:
            return ""
        return "No Aspect.\n\n"

    # Every Aspect is named "Aspect of …", so saying "the X Aspect" around it
    # reads as a stutter. The list of changed abilities is dropped: it ran to a
    # line of its own and the Aspect's own description already says as much.
    detail = (aspect.description or "").strip()
    return f"**{aspect.name}**" + (f": _{detail}_" if detail else "") + "\n\n"


def summarise_item_properties(build: List[Item]) -> str:
    """Total flat and percentage stats across a build.

    Plain arithmetic, correct in both games, and kept as the fallback for a
    build whose god is not known — `smite2_stats.describe_build` needs the god
    to resolve adaptive items and to add its base curves.
    """
    flat: Dict[ItemAttribute, float] = {}
    percent: Dict[ItemAttribute, float] = {}
    for item in build:
        for prop in item.item_properties or []:
            if prop.flat_value is not None:
                flat[prop.attribute] = flat.get(prop.attribute, 0.0) + prop.flat_value
            if prop.percent_value is not None:
                percent[prop.attribute] = (
                    percent.get(prop.attribute, 0.0) + prop.percent_value
                )

    lines = [
        f"**{attribute.display_name}**: {value:,.0f}"
        for attribute, value in sorted(flat.items(), key=lambda kv: kv[0].value)
    ]
    lines += [
        f"**{attribute.display_name}**: {value:.0%}"
        for attribute, value in sorted(percent.items(), key=lambda kv: kv[0].value)
    ]
    if not lines:
        return ""
    return "This build provides:\n" + "\n".join(lines)


class BuildOptions:
    build_type: BuildCommandType
    god_id: GodId | None
    prioritization: BuildPrioritization | None
    queue_id: QueueId | None
    role: PlayerRole | None
    stat: ItemAttribute | None
    balance: BuildBalance | None
    allies: List[GodId] | None
    enemies: List[GodId] | None
    high_mmr: bool
    __random_god: bool = False

    def __init__(
        self,
        god_id: GodId = None,
        build_type: BuildCommandType = BuildCommandType.RANDOM,
        prioritization: BuildPrioritization = None,
        queue_id: QueueId = None,
        role: PlayerRole = None,
        stat: ItemAttribute = None,
        balance: BuildBalance = None,
        enemies: List[GodId] = None,
        allies: List[GodId] = None,
        high_mmr: bool = False,
        provider=None,
    ):
        # Which game's roster names resolve against. Optional so the Smite 1
        # call sites that predate multi-game support keep working, but every
        # command passes one — without it a Smite 2 god name cannot be looked
        # up, and the random god is drawn from Smite 1's enum.
        self.__provider = provider
        if god_id is not None:
            self.god_id = god_id
        else:
            self.god_id = (
                provider.random_god_id()
                if provider is not None
                else random.choice(list(GodId))
            )
            self.__random_god = True
        self.build_type = build_type
        self.prioritization = prioritization
        self.queue_id = queue_id
        self.role = role
        self.stat = stat
        self.balance = balance
        self.enemies = enemies
        self.allies = allies
        self.high_mmr = high_mmr

    def __god_id(self, value: str) -> GodId:
        """Resolve one god name, through the provider when there is one.

        The fallback is the old enum-key mangling, kept so call sites without a
        provider behave exactly as they did.
        """
        if self.__provider is not None:
            god_id = self.__provider.god_id_from_name(value)
            if god_id is None:
                raise InvalidOptionError
            return god_id
        # handles Chang'e case
        return GodId[value.strip().upper().replace(" ", "_").replace("'", "")]

    def set_option(self, option: str, value: str):
        try:
            if option in ("-g", "--god"):
                self.god_id = self.__god_id(value)
                self.__random_god = False
            elif option in ("-p", "--prioritize"):
                self.prioritization = BuildPrioritization(value.lower())
            elif option in ("-q", "--queue"):
                queues = queues_for(
                    self.__provider.game if self.__provider else Game.SMITE
                )
                self.queue_id = queues[
                    value.upper()
                    .replace("(", "")
                    .replace(")", "")
                    .replace(".", "")
                    .replace(" ", "_")
                ]
            elif option in ("-r", "--role"):
                self.role = PlayerRole(value.lower())
            elif option in ("-s", "--stat"):
                self.stat = ItemAttribute(value.lower())
            elif option in ("-b", "--balance"):
                self.balance = BuildBalance(value.lower())
            elif option in ("-t", "--type"):
                self.build_type = BuildCommandType(value.lower())
            elif option in ("-e", "--enemies"):
                self.enemies = [self.__god_id(g) for g in value.split(",")]
            elif option in ("-a", "--allies"):
                self.allies = [self.__god_id(g) for g in value.split(",")]
            elif option in ("-mmr", "--high_mmr"):
                if value is not None:
                    raise InvalidOptionError
                self.high_mmr = True
            else:
                raise InvalidOptionError
        except KeyError as exc:
            raise InvalidOptionError from exc

    def validate(self) -> str | None:
        if (
            self.build_type
            not in (BuildCommandType.RANDOM, BuildCommandType.OPTIMIZE)
            and self.prioritization is not None
        ):
            return (
                "The prioritize option can only be used with the "
                "random and optimize build types."
            )
        if (
            self.build_type
            not in (BuildCommandType.TOP, BuildCommandType.ML, BuildCommandType.OPTIMIZE)
            and self.role is not None
        ):
            return (
                "The role option can only be used with the top, ML "
                "or optimize build types."
            )
        if self.role is not None and self.queue_id is not None:
            if "CONQUEST" not in getattr(self.queue_id, "name", ""):
                return (
                    "Cannot specify both role and queue for a non-Conquest game mode!"
                )
        if self.stat is not None and self.build_type in (
            BuildCommandType.TOP,
            BuildCommandType.ML,
        ):
            return (
                "Cannot prioritize a specific stat when pulling "
                "a top player's build or querying match data."
            )
        queues = queues_for(self.__provider.game if self.__provider else Game.SMITE)
        if (
            self.queue_id is not None
            and not queues.is_normal(self.queue_id)
            and not queues.is_ranked(self.queue_id)
            and self.build_type == BuildCommandType.ML
        ):
            return "ML mode only supports Normal and Ranked modes."
        if self.high_mmr and not queues.is_ranked(self.queue_id):
            return "Cannot filter to high MMR for non-Ranked modes."
        if self.allies is not None and self.__random_god:
            return "Cannot filter by allies without also specifying a God."
        return None

    def was_random_god(self) -> bool:
        return self.__random_god


class GodBuilder:
    __gods: Dict[GodId, God]
    __items: Dict[int, Item]
    __provider: SmiteProvider

    def __init__(
        self,
        gods: Dict[GodId, God],
        items: Dict[int, Item],
        provider: SmiteProvider,
    ):
        self.__gods = gods
        self.__items = items
        self.__provider = provider

    def __ml_from_aggregate(
        self, build_options: BuildOptions, stats
    ) -> GeneratedBuild:
        """Pick a build from the precomputed per-build win counts."""
        god = self.__gods[build_options.god_id]
        queue_id = (
            build_options.queue_id.value if build_options.queue_id is not None else None
        )
        role = (
            build_options.role.value.capitalize()
            if build_options.role is not None
            else None
        )

        starters = tuple(item.id for item in self.__items.values() if item.is_starter)
        query = dict(
            god_id=id_value(build_options.god_id),
            queue_id=queue_id,
            role=role,
            high_mmr=build_options.high_mmr,
            require_starter=True,
            starter_ids=starters,
        )

        # Ranked once and reused: the description, the build shown, and the
        # tree's branches all come out of the same ordering, so they cannot
        # disagree about which build is best.
        candidates = stats.ranked_builds(**query)
        context = self.__team_context(build_options)
        if context is not None and context.known:
            candidates = build_engine.for_lobby(
                candidates, self.__resolve_items, context, self.__carries_anti_heal
            )

        best = candidates[0] if candidates else stats.best_build(**query)
        if best is None or not any(best["items"]):
            raise BuildFailedError

        build = [self.__items[i] for i in best["items"] if i in self.__items]
        if len(build) != len(build_ranker.ITEM_COLUMNS):
            # An item the build was recorded with no longer exists in the
            # catalogue; the build cannot be shown faithfully.
            raise BuildFailedError

        relic_ids = stats.best_relics(
            god_id=id_value(build_options.god_id),
            queue_id=queue_id,
            role=role,
            high_mmr=build_options.high_mmr,
        )
        relics = (
            [self.__items[i] for i in relic_ids if i in self.__items]
            if relic_ids
            else None
        )
        relics = self.__with_starter(god, build, relics)

        god_plays, god_wins = stats.god_totals(
            god_id=id_value(build_options.god_id),
            queue_id=queue_id,
            role=role,
            high_mmr=build_options.high_mmr,
        )

        optimizer = BuildOptimizer(
            god, self.get_valid_items_for_god(god), self.__items
        )

        role_str = f"**{role}** " if role else ""
        in_high_mmr = " higher MMR (2000+)" if build_options.high_mmr else ""
        in_queue = (
            f" in{in_high_mmr} {build_options.queue_id.display_name}"
            if build_options.queue_id is not None
            else ""
        )

        common_role = stats.common_role(id_value(build_options.god_id))
        common_role_str = (
            f"{god.name}'s most common role is **{common_role}**. "
            if common_role and (role or build_options.queue_id is not None)
            else ""
        )

        mmr_str = ""
        if best["avg_rating"] > 0:
            # A rating without a tier is the normal Smite 2 case, not a bug in
            # the data: tracker.gg publishes an MMR-like number and no Hi-Rez
            # tier ordinal at all, so `sum_tier` is 0 across every rated row
            # while `sum_rating` is not. The old guard tested `avg_rating` and
            # then indexed the enum with `avg_tier`, and TierId starts at 1 —
            # so every Smite 2 god with ranked data raised `0 is not a valid
            # TierId` and the command died after saying it was working on it.
            tier = _tier_or_none(best["avg_tier"])
            mmr_str = (
                "These winners average a rank of "
                f"**{PlayerStats.get_tier_string(tier, best['avg_rating'])}**."
                if tier is not None
                else "These winners average a rating of "
                f"**{int(round(best['avg_rating'])):,}**."
            )

        win_rate = (float(god_wins) / god_plays) if god_plays else 0.0

        desc = (
            f"here's your {role_str}build, chosen from **{best['unique_builds']:,}**"
            f" distinct winning{' ' + god.name} {role_str}build"
            f"{'s' if best['unique_builds'] > 1 else ''}{in_queue}. "
            f"This exact build was played **{best['plays']:,}** times and won "
            f"**{(best['win_rate']*100):,.2f}%** of them. "
            f"{mmr_str}\n\n{common_role_str}"
            f"{god.name}'s {'overall ' if not role else role_str}win percentage"
            f"{in_queue} is **{(win_rate*100):,.2f}%**. "
            f"{god.name}'s average winning K/D/A with this build is "
            f"**{int(best['avg_kills'])}/{int(best['avg_deaths'])}/"
            f"{int(best['avg_assists'])}**, dealing an average "
            f"**{int(best['avg_damage']):,}** player damage.\n\n"
            f"{self.__build_stats_string(optimizer, build, god)}"
        )

        return GeneratedBuild(
            build,
            relics,
            desc,
            path=self.__aggregate_path(
                stats, build_options, queue_id, role, starters, optimizer
            ),
        )

    def __with_starter(self, god, build, relics):
        """Put a starter in front of Smite 2's extras, which the corpus omits.

        Smite 1 counts a starter as filling a core slot, so a corpus build
        already contains one. Smite 2 keeps it in its own `StarterId` column —
        and `build_features.SMITE2` aggregates only `ActiveId1`, so the starter
        is not in the aggregate at all. The embed then labelled a lone relic
        "Starter & Relic" and every Smite 2 build came back without one.

        Aggregating `StarterId` is the real fix and needs a corpus rebuild. In
        the meantime the stat model picks one, scored against the build it is
        opening into, which is the same call `/optimize` always made.
        """
        if self.__provider is None or self.__provider.game is not Game.SMITE_2:
            return relics
        try:
            from smite2_optimizer import Smite2BuildOptimizer  # noqa: PLC0415

            starter = Smite2BuildOptimizer(god, self.__items).best_starter(build)
        except Exception as error:  # noqa: BLE001 — a build beats a starter
            print(f"Could not pick a starter: {error}", flush=True)
            return relics
        if starter is None:
            return relics
        return [starter] + list(relics or [])

    def __resolve_items(self, item_ids):
        """Recorded item ids as catalogue items, or None if any has gone.

        A build referencing an item removed since the corpus recorded it cannot
        be shown faithfully, so it drops out rather than being shown short.
        """
        items = [self.__items[i] for i in item_ids if i in self.__items]
        return items if len(items) == len(item_ids) else None

    def __carries_anti_heal(self, items) -> bool:
        """Whether a build brings anti-heal, in whichever game's terms.

        The two express it completely differently — a passive attribute on the
        item in Smite 1, text on the wiki page in Smite 2 — so this is the seam
        rather than a shared notion.
        """
        if self.__provider is not None and self.__provider.game is Game.SMITE_2:
            import smite2_stats  # noqa: PLC0415

            return any(smite2_stats.carries_anti_heal(item) for item in items)

        from passive_parser import PassiveAttribute  # noqa: PLC0415

        return any(
            PassiveAttribute.ANTIHEAL in (item.passive_properties or [])
            for item in items
        )

    def __aggregate_path(
        self, stats, build_options, queue_id, role, starters, optimizer
    ):
        """The conditional tree for a corpus build, drawn from the same ranking.

        `/build` used to show a flat grid because the fork was the optimizer's
        trick of re-scoring at two other balances, and there is no balance to
        re-score at here. `build_engine` finds the branches further down the
        ranking instead, so the good presentation stops being tied to the
        weaker algorithm.

        Never fatal: a build with no tree is drawn as the grid it always was,
        which is exactly what `__send_generated_build` already does with a
        `path` of None.
        """
        try:
            candidates = stats.ranked_builds(
                god_id=id_value(build_options.god_id),
                queue_id=queue_id,
                role=role,
                high_mmr=build_options.high_mmr,
                require_starter=True,
                starter_ids=starters,
            )
            if len(candidates) < 2:
                return None

            def resolve(item_ids):
                items = [self.__items[i] for i in item_ids if i in self.__items]
                return items if len(items) == len(item_ids) else None

            # Ordering a branch needs a scorer, and the two games' are not
            # interchangeable — running Smite 1's caps over Smite 2's stats
            # produces confident nonsense, which is why there are two models at
            # all. The tree only uses it to decide what to buy first, so the
            # cheap Smite 2 optimizer is built here rather than threaded in.
            if self.__provider is not None and self.__provider.game is Game.SMITE_2:
                from smite2_optimizer import Smite2BuildOptimizer  # noqa: PLC0415

                god = self.__gods[build_options.god_id]
                s2 = Smite2BuildOptimizer(god, self.__items)
                score, price, opens = s2.score, s2.price, None
            else:
                score = optimizer.score_build
                price = optimizer.compute_item_price
                opens = optimizer.is_completed_starter

            return build_engine.path_for(
                candidates, resolve, score, price, opens=opens
            )
        except Exception as error:  # noqa: BLE001
            print(f"Could not work out a build path: {error}", flush=True)
            return None

    def __build_stats_string(
        self, optimizer: BuildOptimizer, build: List[Item], god: God = None
    ) -> str:
        """What a build gives you, in whichever terms the game uses.

        Each game has its own model, because they are different games: Smite 1
        has Physical/Magical Power and a 325 protection cap, Smite 2 has
        Strength and Intelligence, per-stat caps and a different mitigation
        curve. Running either game's formulas over the other's items produces
        confident nonsense.
        """
        if self.__provider.game is Game.SMITE:
            return optimizer.get_build_stats_string(build)
        if god is None:
            return summarise_item_properties(build)
        return smite2_stats.describe_build(god, build)

    def get_valid_items_for_god(self, god: God) -> List[Item]:
        return valid_items_for_god(god, self.__items)

    def __random_smite2(self, build_options: BuildOptions) -> GeneratedBuild:
        """A random Smite 2 build: six core items, a starter, a relic, an Aspect.

        The Smite 1 path cannot be reused. It selects relics by
        `root_item_id == 23795` and `tier == 4 and price == 500`, excludes two
        items by literal id, and special-cases Ratatoskr's acorn — every one of
        those is a Smite 1 fact, none matches anything in Smite 2's catalogue,
        and it raised on an empty sequence.

        Actually random, which is the point of the command. It draws uniformly
        from everything the game would let you build and applies no judgement at
        all — the same contract as Smite 1's randomiser, which picks six items
        out of a hat and only ever enforces what the store enforces.

        An earlier version sampled the optimizer's shortlist for a rolled lane.
        That produced sensible builds, which is the one thing a randomiser must
        not do: it is `/optimize` with extra steps, and the joke of a random
        build is that it might be terrible. The rules below are the store's, not
        an opinion about what is good.

        Smite 2's rules are simply shorter than Smite 1's. Six distinct tier-3
        items, one starter, one relic. There are no glyphs to keep one of per
        parent, no acorn to special-case, and god-specific items carry no tier
        at all, so the tier filter already keeps another god's item out of your
        build.
        """
        god = self.__gods[build_options.god_id]

        # Only for the pools, not for any scoring — nothing here ranks.
        optimizer = Smite2BuildOptimizer(god, self.__items)

        pool = optimizer.core_items()
        if build_options.prioritization is not None:
            pool = _prioritized(pool, build_options.prioritization)
        if build_options.stat is not None:
            pool = [
                item
                for item in pool
                if any(
                    p.attribute is build_options.stat
                    for p in item.item_properties or []
                )
            ]
        if len(pool) < 6:
            raise BuildFailedError

        build = random.sample(pool, 6)

        # Nor is there a gold budget. A random build is allowed to be one no
        # game would ever pay for; `/optimize` is where affordability lives.
        extras: List[Item] = []
        starters = optimizer.starters()
        if starters:
            extras.append(random.choice(starters))
        relics = optimizer.relics()
        if relics:
            extras.append(random.choice(relics))

        # The Aspect is a coin flip because that is the choice the game offers:
        # it is picked once at god select and cannot be changed, and playing
        # without one is a real option rather than a worse one.
        aspect = None
        if getattr(god, "aspect", None) is not None and random.random() < 0.5:
            aspect = god.aspect

        stat_line = ""
        if build_options.stat is not None:
            stat_line = f", built around {build_options.stat.display_name}"
        elif build_options.prioritization is not None:
            stat_line = f", leaning {build_options.prioritization.value}"

        # No lane is claimed. An earlier version rolled one and said so, which
        # was true when the items were chosen for that lane and would be a
        # straight lie now that they are drawn at random.
        desc = (
            f"here's your random build{stat_line}!\n\n"
            f"{_aspect_string(god, aspect)}"
            f"{smite2_stats.describe_build(god, build + extras)}"
        )
        return GeneratedBuild(build, extras, desc, aspect)

    def __team_context(self, build_options: BuildOptions) -> team_context.TeamContext:
        """The lobby the caller named, as gods rather than ids.

        A god id that no longer resolves is dropped rather than failing the
        build: a partial enemy team still says something useful, and three
        known enemies all dealing physical damage is worth acting on.
        """
        return team_context.read(
            enemies=[
                self.__gods.get(god_id) for god_id in (build_options.enemies or [])
            ],
            allies=[
                self.__gods.get(god_id) for god_id in (build_options.allies or [])
            ],
        )

    def random(self, build_options: BuildOptions) -> GeneratedBuild:
        if self.__provider.game is not Game.SMITE:
            return self.__random_smite2(build_options)

        god = self.__gods[build_options.god_id]
        items_for_god = self.get_valid_items_for_god(god)
        optimizer = BuildOptimizer(god, items_for_god, self.__items)

        if build_options.queue_id is not None:
            items_for_god = optimizer.filter_queue_items(
                items_for_god, build_options.queue_id
            )
        build: List[Item] = []

        unfiltered_items = items_for_god  # Needed for Ratatoskr
        if build_options.prioritization is not None:
            items_for_god = optimizer.filter_prioritize(
                items_for_god, build_options.prioritization.value
            )
        if build_options.stat is not None:
            items_for_god = optimizer.filter_by_stat(items_for_god, build_options.stat)
        # Filter to just tier 3 items
        items = optimizer.filter_evolution_parents(
            optimizer.filter_acorns(optimizer.filter_tiers(items_for_god))
        )

        # Ratatoskr always has his acorn!
        should_include_starter = int(
            not QueueId.is_duel(build_options.queue_id) and bool(random.randint(0, 1))
        )
        is_ratatoskr = god.id == GodId.RATATOSKR
        build_size = 6 - should_include_starter - int(is_ratatoskr)

        starters = optimizer.get_starters(items_for_god)

        if bool(should_include_starter) and not any(starters):
            build_size += 1
            should_include_starter = 0

        if len(items) < build_size:
            raise BuildFailedError

        # Add build_size random items from our tier 3 items, then shuffle the build order
        build.extend(random.sample(items, build_size))

        build = sorted(build, key=lambda i: i.glyph, reverse=True)

        glyph_parents: Set[Item] = set()
        indexes_to_remove: List[int] = []

        for idx, item in enumerate(build):
            if item.glyph:
                parent_item = self.__items[item.parent_item_id]
                if parent_item in glyph_parents:
                    indexes_to_remove.append(idx)
                    continue
                glyph_parents.add(self.__items[item.parent_item_id])
            elif item in glyph_parents:
                indexes_to_remove.append(idx)

        if any(indexes_to_remove):
            for idx in indexes_to_remove:
                del build[idx]

        if len(build) < build_size:
            items_without_invalid = list(
                filter(
                    lambda i: i not in glyph_parents
                    or (
                        i.glyph and self.__items[i.parent_item_id] not in glyph_parents
                    ),
                    items,
                )
            )

            if len(items_without_invalid) < build_size - len(build):
                raise BuildFailedError

            build.extend(random.sample(items_without_invalid, build_size - len(build)))

        random.shuffle(build)

        # Special casing Ratatoskr's acorn. Gotta have it!
        if is_ratatoskr:
            acorns = optimizer.filter_tiers(
                optimizer.get_ratatoskr_acorn(unfiltered_items)
            )
            build.insert(0, random.choice(acorns))

        # Adding a starter to the beginning of the build if random demands it
        if bool(should_include_starter):
            build.insert(0 + int(is_ratatoskr), random.choice(starters))

        # Upgrade all potential glyphs to random glyphs
        parent_idx, glyph = optimizer.get_glyph_parent_if_no_glyphs(build)

        while glyph is not None:
            build[parent_idx] = glyph
            parent_idx, glyph = optimizer.get_glyph_parent_if_no_glyphs(build)

        shard = random.choice(
            list(
                filter(
                    lambda i: i.type == ItemType.RELIC
                    and i.active
                    and i.tier == 2
                    and i.root_item_id == 23795,  # Shard Relic
                    self.__items.values(),
                )
            )
        )

        relics = random.sample(
            list(
                filter(
                    lambda i: i.type == ItemType.RELIC
                    and i.active
                    and i.tier == 4
                    and i.price == 500
                    and i.id not in (21478, 21492),  # incorrectly listed as active
                    self.__items.values(),
                )
            ),
            2,
        )

        for relic in relics:
            if relic.id == 25890:  # Blessed Barrier
                # Smite API's link is broken
                relic.icon_url = (
                    "https://www.smitefire.com/images/item/blessed-barrier.png"
                )

        relics.insert(0, shard)

        prioritize_str = (
            f", with only {build_options.prioritization.value} items"
            if build_options.prioritization is not None
            else ""
        )
        desc = (
            f"here's your random build{prioritize_str}!\n\n"
            f"{optimizer.get_build_stats_string(build)}"
        )

        return GeneratedBuild(build, relics, desc)

    def __find_team_in_frame(
        self,
        god_matches: pd.DataFrame,
        winner_matches: pd.DataFrame,
        team: List[GodId],
        team_type: str = "Enemy",
    ) -> Tuple[pd.DataFrame, pd.DataFrame, bool, bool, bool]:
        found_team_match: bool = False
        found_role_match: bool = False
        found_type_match: bool = False

        team_str = ",".join(sorted([str(id.value) for id in team]))

        team_matches = god_matches.loc[god_matches[f"{team_type}GodIds"] == team_str]

        team_winner_matches = winner_matches.loc[
            winner_matches[f"{team_type}GodIds"] == team_str
        ]

        if team_winner_matches.shape[0] > 0:
            found_team_match = True
            god_matches = team_matches
            winner_matches = team_winner_matches

        if not found_team_match:
            role_str = ",".join(sorted(self.__gods[g].role.value[0] for g in team))

            team_role_matches = god_matches.loc[
                god_matches[f"{team_type}GodRoles"] == role_str
            ]

            team_winner_role_matches = winner_matches.loc[
                winner_matches[f"{team_type}GodRoles"] == role_str
            ]

            if team_winner_role_matches.shape[0] > 0:
                found_role_match = True
                god_matches = team_role_matches
                winner_matches = team_winner_role_matches

        if not found_team_match and not found_role_match:
            type_str = ",".join(sorted(self.__gods[g].type.value[0] for g in team))

            team_type_matches = god_matches.loc[
                god_matches[f"{team_type}GodTypes"] == type_str
            ]

            team_winner_type_matches = winner_matches.loc[
                winner_matches[f"{team_type}GodTypes"] == type_str
            ]

            if team_winner_type_matches.shape[0] > 0:
                found_type_match = True
                god_matches = team_type_matches
                winner_matches = team_winner_type_matches

        return (
            god_matches,
            winner_matches,
            found_team_match,
            found_role_match,
            found_type_match,
        )

    def ml(self, build_options: BuildOptions) -> GeneratedBuild:
        # The aggregate is the intended path: it covers the whole corpus, which
        # is far too large to hold as rows. The raw-frame version below remains
        # only for installs where no aggregate has been built yet.
        #
        # A lobby used to divert here too, on the reasoning that filtering by
        # team composition needs per-match detail the aggregate does not keep.
        # True, and the wrong conclusion: Smite 2 has no raw frame at all, so
        # that branch raised BuildFailedError for it — /build broke precisely
        # when it learned the most about what the player was up against. The
        # aggregate now takes the matchup as a *scoring* input instead, which
        # composes rather than filters and works for both games.
        stats = self.__provider.build_stats
        if stats is not None and not build_options.was_random_god():
            return self.__ml_from_aggregate(build_options, stats)

        if self.__provider.player_matches is None:
            print("player_matches not initialized")
            raise BuildFailedError

        pm = self.__provider.player_matches

        god_matches: pd.DataFrame = (
            pm.loc[pm["GodId"] == id_value(build_options.god_id)]
            if not build_options.was_random_god()
            else pm
        )

        if build_options.queue_id is not None:
            god_matches = god_matches.loc[
                god_matches["match_queue_id"].astype(int)
                == build_options.queue_id.value
            ]

        winner_matches: pd.DataFrame = god_matches.loc[god_matches["Win_Status"]]

        rank_stat_name = None
        tier_name = None

        if build_options.queue_id is not None and QueueId.is_ranked(
            build_options.queue_id
        ):
            rank_stat_name, tier_name = (
                ("Rank_Stat_Conquest", "Conquest_Tier")
                if build_options.queue_id == QueueId.RANKED_CONQUEST
                else (
                    ("Rank_Stat_Duel", "Duel_Tier")
                    if build_options.queue_id == QueueId.RANKED_DUEL
                    else ("Rank_Stat_Joust", "Joust_Tier")
                )
            )

        if build_options.high_mmr:
            high_mmr = 2000
            god_matches = god_matches.loc[god_matches[rank_stat_name] >= high_mmr]
            winner_matches = winner_matches.loc[
                winner_matches[rank_stat_name] >= high_mmr
            ]

        if build_options.role is not None:
            god_matches = god_matches.loc[
                god_matches["Role"] == build_options.role.value.capitalize()
            ]

            winner_matches = winner_matches.loc[
                winner_matches["Role"] == build_options.role.value.capitalize()
            ]

        found_enemy_team_match: bool = False
        found_enemy_role_match: bool = False
        found_enemy_type_match: bool = False

        if build_options.enemies is not None:
            (
                god_matches,
                winner_matches,
                found_enemy_team_match,
                found_enemy_role_match,
                found_enemy_type_match,
            ) = self.__find_team_in_frame(
                god_matches, winner_matches, build_options.enemies
            )

        found_ally_team_match: bool = False
        found_ally_role_match: bool = False
        found_ally_type_match: bool = False

        if build_options.allies is not None:
            (
                god_matches,
                winner_matches,
                found_ally_team_match,
                found_ally_role_match,
                found_ally_type_match,
            ) = self.__find_team_in_frame(
                god_matches,
                winner_matches,
                build_options.allies + [build_options.god_id],
                "Ally",
            )

        group_by = [
            "BuildHash",
        ]

        if build_options.was_random_god():
            group_by.insert(0, "GodId")

        build_matches = winner_matches.loc[winner_matches["IsFullBuild"]]

        relic_matches = winner_matches.loc[winner_matches["IsFullRelics"]]

        if build_matches.shape[0] == 0:
            raise BuildFailedError

        all_build_matches = god_matches.loc[god_matches["IsFullBuild"]]

        all_grouped_builds = all_build_matches.groupby(group_by).size().reset_index()

        # From: https://stackoverflow.com/questions/3749125/how-should-i-order-these-helpful-scores
        def agresti_coull_lower(n: int, k: int) -> float:
            kappa = 2.24140273  # 95% confidence interval
            kest = k + kappa**2 / 2
            nest = n + kappa**2
            pest = kest / nest
            radius = kappa * math.sqrt(pest * (1 - pest) / nest)
            return max(0, pest - radius)

        def get_build_rank(row: pd.Series) -> Tuple[float, float]:
            build_win_count = row[0]
            total_play_count = (
                all_grouped_builds.loc[
                    all_grouped_builds["BuildHash"] == row["BuildHash"]
                ].iloc[0][0]
                if build_win_count > 1
                else 1
            )

            return (
                agresti_coull_lower(total_play_count, build_win_count),
                (build_win_count / total_play_count),
            )

        grouped_builds = build_matches.groupby(group_by).size().reset_index()

        if grouped_builds.shape[0] >= 1_000:
            # Drop the bottom 75% of builds by frequency if we have a lot of data.
            # This significantly simplifies our build rank calculation, and those builds
            # are not likely to compete for the top spots.
            grouped_builds = grouped_builds.sort_values(by=0, ascending=False).iloc[
                : min((grouped_builds.shape[0] // 10), 5_000)
            ]

        grouped_builds[["BuildRank", "WinPercent"]] = grouped_builds.apply(
            get_build_rank, axis=1, result_type="expand"
        )

        if grouped_builds.shape[0] == 0:
            raise BuildFailedError

        most_freq = grouped_builds.sort_values(by="BuildRank", ascending=False).iloc[0]

        most_freq_relics = None

        if relic_matches.shape[0] != 0:
            most_freq_relics = (
                relic_matches.groupby(["Relics"])
                .size()
                .reset_index()
                .sort_values(by=0, ascending=False)
                .iloc[0]
            )

        god_id = (
            id_value(build_options.god_id)
            if not build_options.was_random_god()
            else most_freq["GodId"]
        )

        best_build_hash = most_freq["BuildHash"]
        build_count = most_freq[0]
        build_win_pct = most_freq["WinPercent"]

        build = []
        relics = None

        best_build_row = winner_matches.loc[
            winner_matches["BuildHash"] == best_build_hash
        ].iloc[0]

        for i in range(1, 7):
            build.append(self.__items[int(best_build_row[f"ItemId{i}"])])

        if build_options.was_random_god():
            build_options.god_id = GodId(god_id)
            winner_matches = winner_matches.loc[winner_matches["GodId"] == god_id]
            god_matches = god_matches.loc[god_matches["GodId"] == god_id]

        god = self.__gods[build_options.god_id]
        items_for_god = self.get_valid_items_for_god(god)
        optimizer = BuildOptimizer(god, items_for_god, self.__items)

        win_count = winner_matches.shape[0]
        god_count = god_matches.shape[0]
        unique_build_count = len(winner_matches["BuildHash"].unique())

        if most_freq_relics is not None:
            relics = []

            for i in most_freq_relics["Relics"].split(","):
                relics.append(self.__items[int(i)])

        common_role_str = ""

        if (
            build_options.queue_id
            in (
                QueueId.CONQUEST,
                QueueId.RANKED_CONQUEST,
                QueueId.UNDER_30_CONQUEST,
            )
            or build_options.role is not None
        ):
            common_roles = (
                pm.loc[
                    (pm["GodId"] == id_value(build_options.god_id))
                    & (pm["Role"] != "Unknown")
                ]["Role"]
                .mode()
                .values
            )

            common_role_str = (
                f"{god.name}'s most common role is **{common_roles[0]}**. "
                if len(common_roles) > 0
                else ""
            )

        role_str = (
            f"**{build_options.role.value.capitalize()}** "
            if build_options.role is not None
            else ""
        )

        mmr_str = ""
        best_build_match = winner_matches.loc[
            winner_matches["BuildHash"] == best_build_hash
        ][
            [
                "Rank_Stat_Conquest",
                "Rank_Stat_Duel",
                "Rank_Stat_Joust",
                "Conquest_Tier",
                "Duel_Tier",
                "Joust_Tier",
                "Kills_Player",
                "Deaths",
                "Assists",
                "Damage_Player",
            ]
        ].median()

        if rank_stat_name is not None:
            median_mmr = best_build_match[rank_stat_name]
            median_tier = best_build_match[tier_name]

            if median_mmr > 0:
                mmr_str = (
                    f"{'These winners have' if build_count > 1 else 'This winner has'} a "
                    f"{'median ' if build_count > 1 else ''}rank of "
                    f"**{PlayerStats.get_tier_string(TierId(math.floor(median_tier)), median_mmr)}**."
                )

        median_kills = best_build_match["Kills_Player"]
        median_deaths = best_build_match["Deaths"]
        median_assists = best_build_match["Assists"]
        median_damage = best_build_match["Damage_Player"]

        win_rate = float(win_count) / god_count

        def get_team_match_strings(
            found_team_match: bool,
            found_role_match: bool,
            found_type_match: bool,
            team_type: str = "enemy",
        ) -> Tuple[str, str]:
            match_str: str = ""
            with_str: str = ""

            if found_team_match:
                match_str = f"*I was able to find the exact {team_type} team composition you requested!*"
                with_str = f" {'against' if team_type == 'enemy' else 'when allied with'} that exact team"
            elif found_role_match:
                match_str = (
                    f"*I couldn't find that exact {team_type} team*, but I found "
                    f"{'some' if winner_matches.shape[0] > 1 else 'one'} "
                    f"that matched their God roles."
                )
                with_str = f" {'against' if team_type == 'enemy' else 'when allied with'} teams matching those roles"
            elif found_type_match:
                match_str = (
                    f"*I couldn't find that exact {team_type} team*, but I found "
                    f"{'some' if winner_matches.shape[0] > 1 else 'one'} "
                    f"that matched their damage types."
                )
                with_str = f" {'against' if team_type == 'enemy' else 'when allied with'} teams matching those damage types"
            else:
                match_str = (
                    f"I couldn't find an {team_type} team matching your request, "
                    "so I fetched overall stats."
                )
            match_str = f"{match_str}\n\n"
            return (match_str, with_str)

        enemy_team_match_str: str = ""
        ally_team_match_str: str = ""
        against_team_str: str = ""
        with_team_str: str = ""

        if build_options.enemies is not None:
            enemy_team_match_str, against_team_str = get_team_match_strings(
                found_enemy_team_match, found_enemy_role_match, found_enemy_type_match
            )

        if build_options.allies is not None:
            ally_team_match_str, with_team_str = get_team_match_strings(
                found_ally_team_match,
                found_ally_role_match,
                found_ally_type_match,
                "allied",
            )

        god_name_str = f" {god.name}" if not build_options.was_random_god() else ""

        in_high_mmr = " higher MMR (2000+)" if build_options.high_mmr else ""

        in_queue = (
            f" in{in_high_mmr} {build_options.queue_id.display_name}"
            if build_options.queue_id is not None
            else ""
        )

        median_kda = (
            f"{god.name}'s median winning K/D/A under these settings is"
            f" **{int(median_kills)}/{int(median_deaths)}/{int(median_assists)}**, "
            f"dealing a median **{int(median_damage):,}** player damage."
        )

        desc = (
            f"here's your {role_str}build, generated from **{unique_build_count:,}**"
            f" {'different ' if unique_build_count > 1 else ''} winning{god_name_str} "
            f"{role_str}build{'s' if unique_build_count > 1 else ''}{in_queue}. "
            f"This exact build won **{build_count:,}** times, with a win "
            f"percentage of **{(build_win_pct*100):,.2f}%**. "
            f"{mmr_str}\n\n{common_role_str}"
            f"Their {'overall ' if build_options.role is None else role_str}"
            f"win percentage{in_queue} is "
            f"**{(win_rate*100):,.2f}%**{against_team_str}"
            f"{' and' if any(against_team_str) and any(with_team_str) else ''}"
            f"{with_team_str}. {median_kda}\n\n"
            f"{enemy_team_match_str}{ally_team_match_str}"
            f"{optimizer.get_build_stats_string(build)}"
        )

        return GeneratedBuild(build, relics, desc)

    async def top(self, build_options: BuildOptions) -> Tuple[List[Item], str]:
        god = self.__gods[build_options.god_id]
        items_for_god = self.get_valid_items_for_god(god)
        optimizer = BuildOptimizer(god, items_for_god, self.__items)
        role = build_options.role
        build = []
        leaderboard_queue = QueueId.RANKED_CONQUEST
        if build_options.queue_id is not None and QueueId.is_ranked(
            build_options.queue_id
        ):
            leaderboard_queue = build_options.queue_id
        god_leaderboard = await self.__provider.get_god_leaderboard(
            god.id, leaderboard_queue
        )

        build_match = None
        match_player_id = None
        while len(build) == 0:
            try:
                # Fetching a random player from the leaderboard
                random_player = random.choice(god_leaderboard)
                god_leaderboard.remove(random_player)
            except IndexError as exc:
                raise BuildFailedError from exc

            # Scraping their recent match history to try and find a current build
            match_history = await self.__provider.get_match_history(
                int(random_player["player_id"])
            )
            for match in match_history:
                if len(build) != 0:
                    break
                if role is not None:
                    match_role = match["Role"]
                    if match_role is not None and match_role.lower() != role.value:
                        continue
                if build_options.queue_id is not None:
                    if int(match["Match_Queue_Id"]) != build_options.queue_id.value:
                        continue
                build_match = match
                match_player_id = int(match["playerId"])
                # Get a full build for this god
                if int(match["GodId"]) == god.id.value and int(match["ItemId6"]) != 0:
                    for i in range(1, 7):
                        # Luckily `getmatchhistory` includes build info!
                        item_id = int(match[f"ItemId{i}"])
                        if item_id == 0:
                            break
                        item = self.__items[item_id]
                        if item.tier < 3 and (
                            item.parent_item_id is None
                            or not self.__items[item.parent_item_id].is_starter
                        ):
                            build = []
                            break
                        build.append(self.__items[item_id])
        playing_str = (
            f'playing {QueueId(int(build_match["Match_Queue_Id"])).display_name}'
        )
        if role is not None and build_options.queue_id is not None:
            playing_str = (
                f"playing {role.value.title()} in "
                f"{build_options.queue_id.display_name}"
            )
        elif role is not None:
            playing_str = f"playing {role.value.title()}"
        elif build_options.queue_id is not None:
            playing_str = f"playing {build_options.queue_id.display_name}"
        if QueueId.is_duel(build_options.queue_id):
            match_details = await self.__provider.get_match_details(
                int(build_match["Match"])
            )
            enemy_player_god = None
            for detail in match_details:
                if int(detail["playerId"]) != match_player_id:
                    enemy_player_god = self.__gods[GodId(int(detail["GodId"]))]
                    break
            if enemy_player_god is not None:
                playing_str += f" against {enemy_player_god.name}"
        rank_str = ""
        if QueueId.is_ranked(leaderboard_queue):
            players = await self.__provider.get_player(match_player_id)
            if any(players):
                player = Player.from_json(players[0])
                if leaderboard_queue in player.ranked_stats:
                    rank_stat = player.ranked_stats[leaderboard_queue]
                    rank_str = (
                        ", who has a rank of "
                        f"{PlayerStats.get_tier_string(rank_stat.tier, rank_stat.mmr)}"
                    )

        desc = (
            f"here's your build, "
            f'courtesy of #{random_player["rank"]} {god.name} '
            f'**{random_player["player_name"]}**{rank_str}! '
            f'{"They won!" if build_match["Win_Status"] == "Winner" else "They lost..."}\n\n'
            f"They were {playing_str} "
            f'and they went {build_match["Kills"]}/'
            f'{build_match["Deaths"]}/{build_match["Assists"]}!\n\n'
            f"{optimizer.get_build_stats_string(build)}"
        )

        return (build, desc)

    async def optimize(self, build_options: BuildOptions) -> GeneratedBuild:
        if self.__provider.game is not Game.SMITE:
            return self.__optimize_smite2(build_options)

        god = self.__gods[build_options.god_id]
        items_for_god = self.get_valid_items_for_god(god)
        optimizer = BuildOptimizer(
            god,
            items_for_god,
            self.__items,
            role=build_options.role,
            context=self.__team_context(build_options),
            balance=(
                build_options.balance.ratio
                if build_options.balance is not None
                else None
            ),
        )
        builds, iterations = await optimizer.optimize()

        if not any(builds):
            raise BuildFailedError

        # A real build finishes its starter and keeps it. Nothing in the search
        # knew that: five items plus a tier-2 starter and six items with none
        # land in the same list, and `score_build` sums per-item scores, so six
        # scored items beat five plus a cheap one every time. Narrowing to the
        # builds that carry a finished starter, when there are any, is what puts
        # Corrupted Bluestone back in a Chaac build.
        with_starter = [
            build
            for build in builds
            if any(optimizer.is_completed_starter(item) for item in build)
        ]
        if with_starter:
            builds = with_starter

        min_ttk = sys.maxsize
        build: List[Item] = None

        team_killed_str = ""
        if god.role == GodRole.HUNTER:
            random_assassin = await self._get_random_god_by_role(
                GodRole.ASSASSIN, build_options.queue_id
            )
            random_guardian = await self._get_random_god_by_role(
                GodRole.GUARDIAN, build_options.queue_id
            )
            random_hunter = await self._get_random_god_by_role(
                GodRole.HUNTER, build_options.queue_id
            )
            random_mage = await self._get_random_god_by_role(
                GodRole.MAGE, build_options.queue_id
            )
            random_warrior = await self._get_random_god_by_role(
                GodRole.WARRIOR, build_options.queue_id
            )
            for bld in builds:
                total_ttk = (
                    self._get_basic_attack_ttk(build_options, bld, random_assassin)
                    + self._get_basic_attack_ttk(build_options, bld, random_guardian)
                    + self._get_basic_attack_ttk(build_options, bld, random_hunter)
                    + self._get_basic_attack_ttk(build_options, bld, random_mage)
                    + self._get_basic_attack_ttk(build_options, bld, random_warrior)
                )
                if total_ttk < min_ttk:
                    min_ttk = total_ttk
                    build = bld
            team_killed_str = ""
        else:
            # Previously `random.choice(builds)`, which is why only hunters
            # were ever really optimized: every other role searched hundreds of
            # thousands of combinations, kept the thousands that cleared its
            # stat targets, and then picked one at random — so a guardian's
            # build was arbitrary among the merely adequate, and asking twice
            # gave two different answers with no sense in which either was
            # better. Ranking by the archetype's own weights is what makes a
            # tank's build the tankiest one available rather than a coin toss.
            build = optimizer.rank_builds(builds)[0]

        ttk_str = ""
        if min_ttk < sys.maxsize:
            ttk_str = f"Kills a full team in **{min_ttk:.2f}s**. "
        viable_str = f"Best of **{len(builds):,}** viable builds. "

        relics = optimizer.conventional_relics(
            build_options.queue_id, build_options.role
        )

        # The same viable set, asked what it would want ahead and behind. One
        # search, three rankings — see `rank_builds`.
        path = build_path.fork(
            build,
            optimizer.rank_builds(builds, balance=BuildBalance.DAMAGE.ratio)[0],
            optimizer.rank_builds(builds, balance=BuildBalance.TANK.ratio)[0],
            optimizer.score_build,
            optimizer.compute_item_price,
            opens=optimizer.is_completed_starter,
        )

        desc = (
            f"here's your {god.name} build. "
            f'{ttk_str if ttk_str != "" else viable_str}'
            f"{team_killed_str}\n\n"
            f"{_context_note(optimizer.context)}"
            f"{optimizer.get_build_stats_string(path.default)}"
        )

        # The starter leads the item list even when the branches disagree about
        # which one: it is bought first either way, and a strip that opens on a
        # tier-3 item reads as though the starter were an afterthought.
        ordered = sorted(
            path.default, key=lambda item: not optimizer.is_completed_starter(item)
        )
        return GeneratedBuild(ordered, relics, desc, path=path)

    def __optimize_smite2(self, build_options: BuildOptions) -> GeneratedBuild:
        """The best Smite 2 build this model can find for a god in a lane.

        Pure stat arithmetic, like the Smite 1 path and unlike `/build`: it
        reads the item catalogue and nothing else. No aggregate, no match
        history, no win rates — which is exactly why it can answer for a god
        nobody has played, and why it keeps working when the corpus does not.

        No search over combinations and no time-to-kill simulation, which is
        what makes the Smite 1 path slow: `Smite2BuildOptimizer` scores a build
        against the stat targets for that lane, so the answer comes from a few
        thousand evaluations rather than millions.

        The Aspect is stated rather than rolled here. `/optimize` is asked for
        one answer, and the honest one for a god with an Aspect is that the
        model has nothing to say about it — an Aspect changes ability behaviour
        the item model never sees.
        """
        god = self.__gods[build_options.god_id]
        role = build_options.role
        optimizer = Smite2BuildOptimizer(
            god,
            self.__items,
            role=role,
            context=self.__team_context(build_options),
            balance=(
                build_options.balance.ratio
                if build_options.balance is not None
                else None
            ),
        )

        pool = optimizer.core_items()
        if build_options.prioritization is not None:
            pool = _prioritized(pool, build_options.prioritization)
        if build_options.stat is not None:
            pool = [
                item
                for item in pool
                if any(
                    p.attribute is build_options.stat
                    for p in item.item_properties or []
                )
            ]
        if len(pool) < 6:
            raise BuildFailedError

        build = optimizer.optimize(6, pool=pool)
        if len(build) < 6:
            raise BuildFailedError

        # The same question at two other balances. Smite 2's optimizer is cheap
        # enough — a few thousand evaluations — to simply ask again, where
        # Smite 1 has to re-rank a search it already paid for.
        def at(ratio: float) -> List[Item]:
            return Smite2BuildOptimizer(
                god,
                self.__items,
                role=role,
                context=optimizer.context,
                balance=ratio,
            ).optimize(6, pool=pool)

        path = build_path.fork(
            build,
            at(BuildBalance.DAMAGE.ratio),
            at(BuildBalance.TANK.ratio),
            optimizer.score,
            optimizer.price,
        )

        extras: List[Item] = []
        starter = optimizer.best_starter(build)
        if starter is not None:
            extras.append(starter)
        relic = optimizer.conventional_relic()
        if relic is not None:
            extras.append(relic)

        # The relic is not part of the stat total: it is chosen by convention
        # rather than scored, so counting its gold against the build would make
        # the build look more expensive than the optimizer actually spent.
        priced = path.default + ([starter] if starter is not None else [])
        desc = (
            f"here's your **{optimizer.role.value.title()}** {god.name} build, "
            f"built on **{optimizer.damage_stat.display_name}** for "
            f"{smite2_stats.total_cost(priced):,} gold.\n\n"
            f"{_context_note(optimizer.context)}"
            f"{smite2_stats.describe_build(god, priced)}"
        )
        return GeneratedBuild(path.default, extras, desc, path=path)

    async def _get_random_god_by_role(
        self, role: GodRole, queue_id: QueueId
    ) -> GodBuild:
        god = random.choice(
            list(
                filter(
                    lambda g: g.role == role and not g.latest_god, self.__gods.values()
                )
            )
        )
        print(f"Finding build for {god.name}...")
        build, _ = await self.top(BuildOptions(god.id, queue_id=queue_id))
        return GodBuild(god, build, 20)

    def _get_basic_attack_ttk(
        self, build_options: BuildOptions, build: List[Item], against_god: GodBuild
    ) -> float:
        calc = DamageCalculator()

        return calc.calculate_basic_ttk(
            GodBuild(self.__gods[build_options.god_id], build, 20), against_god
        )
