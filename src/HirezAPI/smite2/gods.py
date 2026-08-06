"""Building `God` objects out of wiki.smite2.com.

Two sources per god, because neither is complete on its own. `Data:Gods.json`
carries the twenty-level stat curves and the character tags; the god's article
carries the infobox, the lore, the abilities and the Aspect. They are joined on
name, and the *article list* is authoritative for who exists — `Data:Gods.json`
is missing Xing Tian entirely while listing Bastet twice, which is exactly the
kind of thing that makes a roster driven by it quietly 1% wrong.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ability import Ability, _item, _itemDescription
from god import Aspect, God, GodStat, GodStats, _basicAttackProperties
from skin import Skin
from god_types import GodRange, GodType
from HirezAPI import PlayerRole
from item import ItemAttribute
from smite2 import wikitext
from smite2.ids import NameIndex, god_id, squash
from smite2.wiki_client import WikiClient, cache_key

GODS_DATA_PAGE = "Data:Gods.json"
GODS_BUCKET = 'bucket("god_infoboxs2").select("page_name","icon").limit(1000).run()'

LEVELS = 20

# Data:Gods.json's stat keys onto ours. Anything not here is carried in the
# curve dict but has no ItemAttribute to hang off, which is fine — the stat
# card only renders the ones it knows.
STAT_KEYS: Dict[str, ItemAttribute] = {
    "MaxHealth": ItemAttribute.HEALTH,
    "MaxMana": ItemAttribute.MANA,
    "HealthPerTime": ItemAttribute.HP5,
    "ManaPerTime": ItemAttribute.MP5,
    "PhysicalProtection": ItemAttribute.PHYSICAL_PROTECTION,
    "MagicalProtection": ItemAttribute.MAGICAL_PROTECTION,
    "MovementSpeed": ItemAttribute.MOVEMENT_SPEED,
    "AttackSpeedPercent": ItemAttribute.ATTACK_SPEED,
    "InhandPower": ItemAttribute.BASIC_ATTACK_POWER,
    "Strength": ItemAttribute.STRENGTH,
    "Intelligence": ItemAttribute.INTELLIGENCE,
}

# Character.Resource.Primary.X -> what the god actually spends.
_RESOURCE_TAG = "Character.Resource.Primary."

_ABILITY_SECTIONS = ("abilities", "ability")
_ASPECT_SECTIONS = ("god aspect", "aspect")
_LORE_SECTIONS = ("lore",)


def _sections(page: str, wanted: Tuple[str, ...]) -> Optional[str]:
    for heading, body in wikitext.sections(page).items():
        if heading.strip().lower() in wanted:
            return body
    return None


def _curve(stats: Any, key: str) -> Optional[List[float]]:
    """One stat's twenty-level curve, if it is complete.

    Published as a list, or as a level-keyed object depending on the stat. A
    short curve is dropped rather than padded: a god whose health stops at level
    twelve should read as having no health curve, not as one that plateaus.
    """
    if not isinstance(stats, dict):
        return None
    raw = stats.get(key)
    if isinstance(raw, dict):
        try:
            raw = [raw[str(level)] for level in range(1, LEVELS + 1)]
        except KeyError:
            return None
    if not isinstance(raw, list) or len(raw) < LEVELS:
        return None
    try:
        return [float(value) for value in raw[:LEVELS]]
    except (TypeError, ValueError):
        return None


def _god_stats(record: Dict[str, Any]) -> GodStats:
    stats = GodStats()
    stats.values = {}
    base_stats = record.get("baseStats") or {}
    for key, attribute in STAT_KEYS.items():
        curve = _curve(base_stats, key)
        if curve is None:
            continue
        # Keep the linear pair populated too, so anything reading .base or
        # .per_level directly gets a sane approximation rather than zero.
        per_level = (curve[-1] - curve[0]) / (LEVELS - 1) if LEVELS > 1 else 0.0
        stats.values[attribute] = GodStat(curve[0], per_level, curve=curve)

    # Smite 2 has no basic-attack progression string to parse. The object is
    # still constructed so `get_stat_at_level` and the stat card do not have to
    # special-case its absence; it simply has no damage.
    stats.basic_attack = _basicAttackProperties.__new__(_basicAttackProperties)
    stats.basic_attack.base_damage = 0.0
    stats.basic_attack.per_level = 0.0
    stats.basic_attack.scaling = 0.0
    stats.basic_attack.base_damage_back = 0.0
    stats.basic_attack.per_level_back = 0.0
    stats.basic_attack.scaling_back = 0.0
    stats.basic_attack.progression = None
    return stats


def _resource(record: Dict[str, Any]) -> str:
    for tag in record.get("characterTags") or []:
        text = str(tag)
        if text.startswith(_RESOURCE_TAG):
            return text[len(_RESOURCE_TAG) :].split(".")[0].lower()
    return "mana"


def _specs(infobox: Optional[wikitext.Template]) -> List[str]:
    if infobox is None:
        return []
    out = []
    for slot in range(1, 7):
        value = wikitext.strip_markup(infobox.get(f"spec{slot}"))
        if value:
            out.append(value)
    return out


def _positions(infobox: Optional[wikitext.Template]) -> List[PlayerRole]:
    """Where the god is played.

    The infobox spells the middle lane "Mid"; PlayerRole calls it the same, but
    tracker.gg says "middle", so both are accepted here to keep one vocabulary.
    """
    if infobox is None:
        return []
    out = []
    for slot in ("role1", "role2"):
        text = wikitext.strip_markup(infobox.get(slot)).strip().lower()
        if not text:
            continue
        if text in ("middle", "mid"):
            text = "mid"
        try:
            role = PlayerRole(text)
        except ValueError:
            continue
        if role not in out:
            out.append(role)
    return out


_SKIN_KEY = re.compile(r"^(skin\d+(?:_prism\d+)?)$")


def _skins(page: str, god, urls: Dict[str, str]) -> List[Skin]:
    """The god's skins, from the SkinViewer invocation on its article.

    Keys come in families — `skin1`, `skin1_img`, `skin1_icon`, `skin1_price`,
    plus `skin1_prism1` and friends for the recoloured variants — so a skin is
    a prefix rather than a template of its own.

    Without these, `/smitele` loses two of its six rounds to an empty list and
    trivia loses its "which god is this a skin for" question.
    """
    section = _sections(page, ("skins",))
    if section is None:
        return []

    invocations = [
        t for t in wikitext.parse_all(section)
        if t.name.lower().startswith("#invoke:skinviewer")
    ]
    if not invocations:
        return []
    params = invocations[0].params

    out: List[Skin] = []
    for key, name in sorted(params.items()):
        if not _SKIN_KEY.match(key) or not name.strip():
            continue
        image = _file_name(params.get(f"{key}_img") or params.get(f"{key}_icon") or "")
        url = urls.get(_titled(image)) if image else None
        if not url:
            continue

        skin = Skin()
        skin.name = wikitext.strip_markup(name)
        skin.card_url = url
        skin.god_id = god.id
        # The trivia skin question deliberately skips "Normal" skins, so the
        # default is marked as such and everything else keeps its rarity.
        rarity = wikitext.strip_markup(params.get(f"{key}_rarity") or "")
        skin.obtainability = (
            "Normal" if skin.name.lower() in ("default", "standard") else (rarity or "Skin")
        )
        skin.price_favor = 0
        skin.price_gems = 0
        skin.id = (0, 0)
        out.append(skin)
    return out


def _skin_files(page: str) -> List[str]:
    """Every File: a god's skins reference, for one batched URL lookup."""
    section = _sections(page, ("skins",))
    if section is None:
        return []
    out = []
    for template in wikitext.parse_all(section):
        if not template.name.lower().startswith("#invoke:skinviewer"):
            continue
        for key, value in template.params.items():
            if key.endswith("_img") or key.endswith("_icon"):
                name = _file_name(value)
                if name:
                    out.append(name)
    return out


def _ability_icons(page: str) -> List[str]:
    section = _sections(page, _ABILITY_SECTIONS)
    if section is None:
        return []
    out = []
    for template in wikitext.parse_templates(section, "Ability"):
        name = _file_name(template.get("icon"))
        if name:
            out.append(name)
    return out


def _ability(template: wikitext.Template, urls: Dict[str, str] = None) -> Ability:
    """One `{{Ability}}` as an `Ability`.

    `Ability` reads its cooldowns and costs out of slash-separated strings
    because that is the shape Hi-Rez returns, so the per-rank arrays parsed out
    of the wiki are joined back into that shape rather than the class being
    widened. It keeps one code path for both games' rank handling.
    """
    stats = wikitext.parse_stat_block(template.get("stats"))

    def joined(*labels: str) -> str:
        for label in labels:
            for key, (values, _unit) in stats.items():
                if key.strip().lower() == label:
                    return "/".join(
                        f"{v:g}" for v in values
                    )
        return ""

    description = wikitext.strip_markup(template.get("description"))
    short = wikitext.strip_markup(template.get("short"))
    if short and short.lower() not in description.lower():
        description = f"{short}\n\n{description}" if description else short

    menu_items = [
        _item(f"{label}:", "/".join(f"{v:g}" for v in values) + (f" {unit}" if unit else ""))
        for label, (values, unit) in stats.items()
    ]

    return Ability(
        _itemDescription(
            cooldown=joined("cooldown"),
            cost=joined("cost", "mana cost"),
            description=description,
            menu_items=menu_items,
            rank_items=menu_items,
        ),
        id=0,
        name=wikitext.strip_markup(template.get("name")),
        icon_url=(urls or {}).get(_titled(_file_name(template.get("icon")))),
        is_passive=template.get("slot", "").strip().lower() == "passive",
    )


def _abilities(page: str, urls: Dict[str, str] = None) -> List[Ability]:
    """The god's own abilities, and only those.

    Scoped to the Abilities section because 70 of 88 articles repeat every
    `{{Ability}}` in the Aspect section in its altered form. Parsing the whole
    page silently doubles the kit and picks whichever copy comes first.
    """
    section = _sections(page, _ABILITY_SECTIONS)
    if section is None:
        return []
    return [_ability(t, urls) for t in wikitext.parse_templates(section, "Ability")]


def _aspect(page: str, urls: Dict[str, str]) -> Optional[Aspect]:
    """The god's Aspect, if it has one.

    The section holds an `{{Achievement}}` carrying the Aspect's name, icon and
    what it changes — not an `{{Aspect}}` template, which does not exist —
    followed by a collapsed div of `{{Ability}}` blocks.

    Those blocks are the god's *own* abilities as the Aspect alters them, which
    is why they are keyed by slot here rather than appended to the kit: an
    Aspect is a selection-time toggle on how the god plays, and has no abilities
    of its own. It is also why the ability parser scopes itself to the Abilities
    section — 70 of 88 articles would otherwise return each ability twice.
    """
    section = _sections(page, _ASPECT_SECTIONS)
    if section is None:
        return None

    name = ""
    description = ""
    image = ""
    for template in wikitext.parse_templates(section, "Achievement"):
        candidate = wikitext.strip_markup(template.get("name"))
        if not candidate:
            continue
        name = candidate
        description = wikitext.strip_markup(template.get("description"))
        image = _file_name(template.get("image"))
        break

    if not name:
        return None

    changed = {}
    for template in wikitext.parse_templates(section, "Ability"):
        slot = wikitext.strip_markup(template.get("slot")).strip()
        if slot:
            changed[slot] = _ability(template)

    return Aspect(
        name=name,
        description=description,
        icon_url=urls.get(_titled(image)) if image else None,
        changed_abilities=changed,
    )


def _lore(page: str) -> str:
    section = _sections(page, _LORE_SECTIONS)
    return wikitext.strip_markup(section) if section else ""


async def load(
    client: WikiClient, silent: bool = False
) -> Tuple[Dict[int, God], NameIndex, Dict[int, List[Skin]]]:
    """Every Smite 2 god, keyed by synthetic id, plus the name index.

    The index answers to slugs, display names and the `Gods.X` engine tokens
    tracker.gg emits, which together cover 100% of the god values observed in
    26,444 sampled player rows.
    """
    rows = await client.bucket(GODS_BUCKET)
    titles = sorted({str(r["page_name"]) for r in rows if r.get("page_name")})
    icons = {
        str(r["page_name"]): str(r.get("icon") or "")
        for r in rows
        if r.get("page_name")
    }

    data_page = await client.query_pages([GODS_DATA_PAGE])
    records = json.loads(data_page[GODS_DATA_PAGE]["content"])
    if isinstance(records, dict):
        records = records.get("json") or records.get("data") or []

    # Dedupe on name, preferring the record with the most complete curves —
    # Bastet appears twice and one copy is missing its protections. Deterministic
    # so every process derives the same id from the same slug.
    by_name: Dict[str, Dict[str, Any]] = {}
    for record in records:
        key = squash(record.get("name"))
        if not key:
            continue
        incumbent = by_name.get(key)
        if incumbent is None or _completeness(record) > _completeness(incumbent):
            by_name[key] = record

    pages = await client.query_pages(titles)

    # Every File: the gods need, resolved in one batched pass — card art, the
    # bucket's icon, and each Aspect's image.
    files = []
    for title in titles:
        page = pages.get(title, {}).get("content") or ""
        infoboxes = wikitext.parse_templates(page, "God infoboxS2", top_level=True)
        image = infoboxes[0].get("image") if infoboxes else ""
        if image:
            files.append(image)
        if icons.get(title):
            files.append(_file_name(icons[title]))
        aspect_section = _sections(page, _ASPECT_SECTIONS)
        if aspect_section:
            for template in wikitext.parse_templates(aspect_section, "Achievement"):
                files.append(_file_name(template.get("image")))
        files.extend(_ability_icons(page))
        files.extend(_skin_files(page))
    urls = await client.file_urls([f for f in files if f])

    gods: Dict[int, God] = {}
    index = NameIndex()
    skins: Dict[int, List[Skin]] = {}

    for title in titles:
        page = pages.get(title, {}).get("content")
        if not page:
            if not silent:
                print(f"smite2: no article for {title}", flush=True)
            continue

        record = by_name.get(squash(title), {})
        infoboxes = wikitext.parse_templates(page, "God infoboxS2", top_level=True)
        infobox = infoboxes[0] if infoboxes else None

        god = God()
        god.name = str(record.get("name") or title)
        slug = str(record.get("slug") or "") or _slug_for(god.name)
        god.id = god_id(slug)
        god.title = wikitext.strip_markup(
            str(record.get("title") or (infobox.get("title") if infobox else ""))
        )
        god.pantheon = wikitext.strip_markup(
            str(record.get("pantheon") or (infobox.get("pantheon") if infobox else ""))
        )
        god.lore = _lore(page)
        god.abilities = _abilities(page, urls)
        god.aspect = _aspect(page, urls)
        god.stats = _god_stats(record)
        god.resource = _resource(record)
        god.positions = _positions(infobox)
        god.specs = _specs(infobox)

        # No classes in Smite 2. Deliberately left None rather than mapped onto
        # a GodRole, which means something else — see God.positions.
        god.role = None
        god.pros = []

        damage = str(
            record.get("primaryDamageType")
            or (infobox.get("attack damage") if infobox else "")
        ).strip().lower()
        god.type = GodType(damage) if GodType.has_value(damage) else None

        attack = wikitext.strip_markup(
            infobox.get("attack type") if infobox else ""
        ).strip().lower()
        god.range = GodRange(attack) if GodRange.has_value(attack) else None

        card_file = infobox.get("image") if infobox else ""
        god.card_url = urls.get(_titled(card_file), "")
        god.icon_url = urls.get(_titled(_file_name(icons.get(title, "")))) or god.card_url

        god.auto_banned = False
        god.on_free_rotation = False
        god.latest_god = False

        gods[god.id] = god
        skins[god.id] = _skins(page, god, urls)
        tags = [
            t for t in (record.get("characterTags") or []) if str(t).startswith("Gods.")
        ]
        index.add(god.name, title, slug, *tags)

    return gods, index, skins


def _completeness(record: Dict[str, Any]) -> int:
    stats = record.get("baseStats") or {}
    return sum(1 for key in stats if _curve(stats, key) is not None)


def _slug_for(name: str) -> str:
    from smite2.ids import slugify  # noqa: PLC0415

    return slugify(name)


def _file_name(value: str) -> str:
    """A bucket `icon` cell down to a plain file name.

    Bucket PAGE columns come back either bare or as `[[File:x.png]]`.
    """
    text = str(value or "").strip()
    if text.startswith("[["):
        text = text.strip("[]").split("|")[0]
    if text.lower().startswith("file:"):
        text = text.split(":", 1)[1]
    return text.strip()


def _titled(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    return name if name.startswith("File:") else f"File:{name}"


__all__ = ["load", "cache_key"]
