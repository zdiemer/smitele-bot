import io
import os
import re
from typing import Dict, List

import aiohttp

import art_cache
from ability import Ability
from god_types import *
from item import ItemAttribute


class _basicAttackProgression:
    damage: List[float]
    swing_time: List[float]
    is_aoe: List[bool]
    has_progression: bool

    NUMBER_REGEX = r"([0-9]?\.?[0-9]+)"

    def __init__(self, progression: str):
        self.damage = []
        self.swing_time = []
        self.is_aoe = []
        self.parse_progression(progression)

    def parse_progression(self, progression: str):
        if "none" in progression or "special" in progression:
            self.has_progression = False
        progression = progression.lower()
        split_prog = progression.split("/")

        if len(split_prog) > 1:
            self.has_progression = True
            shared_swing_time = (
                "and swing time" in progression
                or "and speed" in progression
                or progression.endswith("damage")
            )
            is_swing_time = False
            for idx, p in enumerate(split_prog):
                if p.replace("x", "").isdigit():
                    if not is_swing_time:
                        self.damage.append(float(p.replace("x", "")))
                        self.is_aoe.append(False)
                    else:
                        self.swing_time.append(float(p.replace("x", "")))
                    if shared_swing_time:
                        self.swing_time.append(float(p.replace("x", "")))
                else:
                    matches: List[str] = re.findall(self.NUMBER_REGEX, p)
                    if any(matches):
                        self.is_aoe.append(False)
                        if len(matches) > 1 and idx == len(split_prog) - 1:
                            self.damage.append(float(matches[0].replace("x", "")))
                            self.swing_time.append(float(matches[1].replace("x", "")))
                            is_swing_time = True
                        else:
                            self.damage.append(float(matches[0].replace("x", "")))
            if "aoe on the final blow" in progression:
                self.is_aoe[-1] = True
            elif "aoe on first two hits" in progression:
                self.is_aoe[0] = True
                self.is_aoe[1] = True
            elif "3rd attack" in progression or "+aoe" in progression:
                self.is_aoe[2] = True
            elif "4th attack" in progression:
                self.is_aoe[3] = True
            if not any(self.swing_time):
                for _ in self.damage:
                    self.swing_time.append(1)
        self.has_progression = False


class _basicAttackProperties:
    DAMAGE_REGEX = r"(?P<base_damage>\d+\.?\d*) \+ (?P<per_level>\d*\.?\d*)/Lvl \(\+(?P<scaling>\d+)%\ of (Magical|Physical) Power\)"
    __damage: str
    __progression: str
    __damage_scaling: str

    base_damage: float
    per_level: float
    scaling: float
    progression: _basicAttackProgression = None

    # Izanami special case
    base_damage_back: float = 0
    per_level_back: float = 0
    scaling_back: float = 0

    def __init__(self, damage: str, progression: str, damage_scaling: str):
        self.__damage = damage
        self.__progression = progression
        self.__damage_scaling = damage_scaling

        regex = re.compile(self.DAMAGE_REGEX)
        groups = [match.groupdict() for match in regex.finditer(self.__damage)]
        for idx, group in enumerate(groups):
            base_damage = float(group["base_damage"])
            per_level = float(group["per_level"])
            scaling = float(group["scaling"]) / 100.0
            if idx == 1:  # Izanami special case
                self.base_damage_back = base_damage
                self.per_level_back = per_level
                self.scaling_back = scaling
            else:
                self.base_damage = base_damage
                self.per_level = per_level
                self.scaling = scaling
        if self.__progression is not None:
            self.progression = _basicAttackProgression(self.__progression)

    @staticmethod
    def from_json(obj):
        menu_items = obj["itemDescription"]["menuitems"]
        damage = None
        progression = None
        damage_scaling = None

        for item in menu_items:
            desc = item["description"]
            value = item["value"]
            if desc in ("Damage:", "Damage"):
                damage = value
            if "Progression:" in desc and value != "None":
                progression = value
            if "Damage Scaling:" in desc:
                damage_scaling = value

        return _basicAttackProperties(damage, progression, damage_scaling)


class GodStat:
    base: float
    per_level: float
    curve: List[float]

    def __init__(self, base: float, per_level: float = 0, curve: List[float] = None):
        self.base = base
        self.per_level = per_level
        # Smite 1 publishes a base and a per-level increment, which is exactly
        # linear. Smite 2 publishes the value at each of the twenty levels, and
        # those are not linear — Cu Chulainn's health runs 661 … 2319.84 at 19
        # and then 2447 at 20, a step a (base, per_level) pair cannot express.
        # When a curve is present it is the truth and the linear pair is only a
        # fallback for callers that predate it.
        self.curve = curve

    def at_level(self, level: int) -> float:
        if self.curve:
            return self.curve[max(1, min(level, len(self.curve))) - 1]
        return self.base + self.per_level * (level - 1)


class GodStats:
    values: Dict[ItemAttribute, GodStat]
    basic_attack: _basicAttackProperties

    def __init__(self):
        pass

    @staticmethod
    def from_json(obj):
        stats = GodStats()
        stats.values = {}
        if "AttackSpeed" in obj and "AttackSpeedPerLevel" in obj:
            stats.values[ItemAttribute.ATTACK_SPEED] = GodStat(
                float(obj["AttackSpeed"]), float(obj["AttackSpeedPerLevel"])
            )
        if "HealthPerFive" in obj and "HP5PerLevel" in obj:
            stats.values[ItemAttribute.HP5] = GodStat(
                float(obj["HealthPerFive"]), float(obj["HP5PerLevel"])
            )
        if "Health" in obj and "HealthPerLevel" in obj:
            stats.values[ItemAttribute.HEALTH] = GodStat(
                float(obj["Health"]), float(obj["HealthPerLevel"])
            )
        if "ManaPerFive" in obj and "MP5PerLevel" in obj:
            stats.values[ItemAttribute.MP5] = GodStat(
                float(obj["ManaPerFive"]), float(obj["MP5PerLevel"])
            )
        if "MagicProtection" in obj and "MagicProtectionPerLevel" in obj:
            stats.values[ItemAttribute.MAGICAL_PROTECTION] = GodStat(
                float(obj["MagicProtection"]), float(obj["MagicProtectionPerLevel"])
            )
        if "Mana" in obj and "ManaPerLevel" in obj:
            stats.values[ItemAttribute.MANA] = GodStat(
                float(obj["Mana"]), float(obj["ManaPerLevel"])
            )
        if "PhysicalProtection" in obj and "PhysicalProtectionPerLevel" in obj:
            stats.values[ItemAttribute.PHYSICAL_PROTECTION] = GodStat(
                float(obj["PhysicalProtection"]),
                float(obj["PhysicalProtectionPerLevel"]),
            )
        if "Speed" in obj:
            stats.values[ItemAttribute.MOVEMENT_SPEED] = GodStat(float(obj["Speed"]))

        stats.basic_attack = _basicAttackProperties.from_json(obj["basicAttack"])
        return stats


class Aspect:
    """A Smite 2 god's Aspect — a way of playing them, chosen at god select.

    An Aspect is toggled once during selection and fixed for the match; it
    cannot be switched in game. It is neither cosmetic nor a set of extra
    abilities of its own — it changes how the god's existing kit behaves, often
    substantially enough that the god fills a different role. So an Anubis
    played with one is not the same character as an Anubis without, and the
    corpus carries the choice as its own column rather than folding it into the
    build.

    `changed_abilities` is the god's *own* abilities as the Aspect alters them,
    keyed by slot — the wiki publishes both forms. An empty mapping means the
    Aspect's effect is described in prose rather than as changed ability
    numbers, which is common.

    tracker.gg calls these `talent` in its equipment list. They are the same
    thing: every one comes back named "Aspect of …", at most one per player,
    which is what being a selection-time toggle implies.
    """

    name: str
    description: str
    icon_url: str
    changed_abilities: dict

    def __init__(
        self,
        name: str,
        description: str = "",
        icon_url: str = None,
        changed_abilities: dict = None,
    ):
        self.name = name
        self.description = description
        self.icon_url = icon_url
        self.changed_abilities = changed_abilities or {}


class God(object):
    abilities: List[Ability]
    stats: GodStats
    name: str
    role: GodRole
    type: GodType
    range: GodRange
    auto_banned: bool
    on_free_rotation: bool
    latest_god: bool
    title: str
    lore: str
    pantheon: str
    pros: List[GodPro]
    card_url: str
    icon_url: str
    id: GodId

    # Smite 2 has no classes — no Mage, Guardian, Hunter — so `role` is None
    # there and these carry what it does have. `positions` is where the god is
    # played (which in this codebase is PlayerRole, not GodRole) and `specs` is
    # the Nuker/Lockdown/Sustain vocabulary that replaced GodPro.
    #
    # Kept off GodRole deliberately: Item.from_json parses RestrictedRoles into
    # GodRole and build_optimizer switches on it, so widening that enum to hold
    # positions would quietly change which items a Smite 1 god may build.
    positions: List = []
    specs: List[str] = []

    # Smite 2's resource is a characterTag rather than a hardcoded exception
    # list. "mana" for almost everyone; rage, spirit, omi and health for the
    # handful that differ.
    resource: str = "mana"

    # Smite 2 only, and None for the seventeen gods that have not been given one.
    aspect: "Aspect" = None

    def __init__(self):
        pass

    @staticmethod
    def from_json(obj):
        god = God()

        god.abilities = [
            Ability.from_json(obj[f"Ability_{idx}"], idx == 5) for idx in range(1, 6)
        ]
        god.stats = GodStats.from_json(obj)
        god.name = obj["Name"]
        god.role = GodRole(obj["Roles"].strip().lower())
        god.auto_banned = obj["AutoBanned"] == "y"
        god.on_free_rotation = obj["OnFreeRotation"] == "y"
        god.latest_god = obj["latestGod"] == "y"
        god.title = obj["Title"]
        god.lore = obj["Lore"]
        god.pantheon = obj["Pantheon"]
        god.pros = [GodPro(val.strip().lower()) for val in obj["Pros"].split(",")]
        god.card_url = obj["godCard_URL"]
        god.icon_url = obj["godIcon_URL"]
        god.id = GodId(obj["id"])

        types = [val.strip() for val in obj["Type"].split(",")]
        for typ in types:
            typ = typ.lower()
            if GodType.has_value(typ):
                god.type = GodType(typ)
            if GodRange.has_value(typ):
                god.range = GodRange(typ)

        return god

    async def get_card_bytes(self) -> io.BytesIO:
        # Keyed on card_url, so fetch card_url — this fetched icon_url, which
        # cached the icon under the card's name and served it as the card art.
        return await art_cache.fetch(
            self.card_url, "gods", "cards", art_cache.cache_key(self.card_url)
        )

    async def get_icon_bytes(self) -> io.BytesIO:
        return await art_cache.fetch(
            self.icon_url, "gods", "icons", art_cache.cache_key(self.icon_url)
        )

    @property
    def is_manaless(self) -> bool:
        """Whether this god's mana pool is really something else.

        Smite 1 has exactly two and they are named here because the API gives
        no other signal. Smite 2 publishes the resource as a tag, so its gods
        answer from data — the hardcoded pair must not catch them, since the
        two games' ids are disjoint but the *names* overlap.
        """
        if self.resource != "mana":
            return True
        return isinstance(self.id, GodId) and self.id in (
            GodId.CU_CHULAINN,
            GodId.YEMOJA,
        )

    def get_stat_at_level(self, stat: ItemAttribute, level: int) -> float:
        try:
            if stat == ItemAttribute.BASIC_ATTACK_DAMAGE:
                basic = (
                    self.stats.basic_attack.base_damage
                    + self.stats.basic_attack.per_level * (level - 1)
                )
                basic_back = (
                    self.stats.basic_attack.base_damage_back
                    + self.stats.basic_attack.per_level_back * (level - 1)
                )
                total_basic = basic + basic_back
                # Hunters get an extra 0.5% per level on top; everyone else
                # gets the plain total. This returned 0 for every non-Hunter,
                # so /build and the god stats card reported no basic attack
                # damage at all for mages, warriors, guardians and assassins.
                if self.role == GodRole.HUNTER:
                    return total_basic + (0.005 * level * total_basic)
                return total_basic
            if stat == ItemAttribute.MOVEMENT_SPEED:
                # Smite 2 publishes movement speed per level directly; Smite 1
                # gives a base that grows 3% a level and stops at 8.
                if self.stats.values[stat].curve:
                    return self.stats.values[stat].at_level(level)
                level = 8 if level > 8 else level
                speed = self.stats.values[stat].base
                return speed + (speed * 0.03 * (level - 1))
            if self.is_manaless:
                if stat == ItemAttribute.MANA:
                    return 0
                if stat == ItemAttribute.MP5:
                    return 0
                # No mana bar, so the mana pool is really extra health and the
                # MP5 extra HP5.
                if stat in (ItemAttribute.HEALTH, ItemAttribute.HP5):
                    pooled_stat = (
                        ItemAttribute.MANA
                        if stat == ItemAttribute.HEALTH
                        else ItemAttribute.MP5
                    )
                    if pooled_stat not in self.stats.values:
                        return self.stats.values[stat].at_level(level)
                    return self.stats.values[stat].at_level(level) + self.stats.values[
                        pooled_stat
                    ].at_level(level)

            return self.stats.values[stat].at_level(level)
        except KeyError:
            return 0
