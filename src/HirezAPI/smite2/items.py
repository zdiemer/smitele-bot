"""Building `Item` objects out of wiki.smite2.com.

The item catalogue is the half of the static data tracker.gg cannot supply at
all: it names an item by slug and stops. Tier in particular only exists here,
and `build_features` needs it to decide whether six slots constitute a build.

Measured over the whole catalogue: tier 3 is exactly the
Offensive/Defensive/Hybrid population, 133 items, every one of which has both a
tier and at least one stat. Tier is absent only where it is meaningless — 12
relics, 15 consumables, 5 curios, 17 god-specific items — none of which can
occupy a core slot.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from item import Item, ItemAttribute, ItemProperty, ItemType
from smite2 import wikitext
from smite2.ids import NameIndex, item_id
from smite2.wiki_client import WikiClient

ITEMS_BUCKET = (
    'bucket("item_infobox").select("page_name","icon","cost","total_cost")'
    ".limit(1000).run()"
)

STAT_TEMPLATE_CATEGORY = "Stat templates"

_STAT_PARAM = re.compile(r"stat\d+")
_COMPONENT_PARAM = re.compile(r"i\d+")

# Only these can fill one of the six core build slots.
CORE_TYPES = ("Offensive", "Defensive", "Hybrid")

# The wiki's stat template names onto our attributes. Read from
# Category:Stat templates at load time so a stat added to the game needs no code
# change; this is the fallback for the handful whose label does not match an
# ItemAttribute value directly, and for running without a network.
STAT_TEMPLATES: Dict[str, ItemAttribute] = {
    "hp": ItemAttribute.HEALTH,
    "mp": ItemAttribute.MANA,
    "hpr": ItemAttribute.HEALTH_REGEN,
    "mpr": ItemAttribute.MANA_REGEN,
    "manaregen": ItemAttribute.MANA_REGEN,
    "healr": ItemAttribute.HEAL_REDUCTION,
    "pprot": ItemAttribute.PHYSICAL_PROTECTION,
    "mprot": ItemAttribute.MAGICAL_PROTECTION,
    "int": ItemAttribute.INTELLIGENCE,
    "str": ItemAttribute.STRENGTH,
    "cdr": ItemAttribute.COOLDOWN_RATE,
    "as": ItemAttribute.ATTACK_SPEED,
    "bap": ItemAttribute.BASIC_ATTACK_POWER,
    "ls": ItemAttribute.LIFESTEAL,
    "pen": ItemAttribute.PENETRATION,
    "crit": ItemAttribute.CRITICAL_CHANCE,
    "ms": ItemAttribute.MOVEMENT_SPEED,
    "ten": ItemAttribute.TENACITY,
    "damp": ItemAttribute.DAMPENING,
    "echo": ItemAttribute.ECHO,
    "plat": ItemAttribute.PLATED,
    "con": ItemAttribute.CONSTRUCTION,
    "path": ItemAttribute.PATHFINDING,
    "sta": ItemAttribute.STAMINA,
    "act": ItemAttribute.COOLDOWN_RATE,
    "pas": ItemAttribute.COOLDOWN_RATE,
}

# The wiki's `type=` onto ours. Smite 2 adds two kinds Smite 1 has no name for;
# both are mapped onto ITEM so /trivia and $item render them, and neither can
# reach a core slot because neither carries tier 3.
TYPE_NAMES: Dict[str, ItemType] = {
    "offensive": ItemType.ITEM,
    "defensive": ItemType.ITEM,
    "hybrid": ItemType.ITEM,
    "starter": ItemType.ITEM,
    "god specific": ItemType.ITEM,
    "curio": ItemType.ITEM,
    "consumable": ItemType.CONSUMABLE,
    "relic": ItemType.RELIC,
}

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _int(value: object, default: int = 0) -> int:
    match = _NUMBER.search(str(value or ""))
    return int(float(match.group(0))) if match else default


def _properties(
    infobox: wikitext.Template, templates: Dict[str, ItemAttribute]
) -> List[ItemProperty]:
    """`stat1={{Int|30}}` and friends as ItemProperty.

    Percent versus flat follows the `%` suffix, the same convention
    `ItemProperty.from_json` already uses for Hi-Rez's values.
    """
    out: List[ItemProperty] = []
    for key, raw in infobox.params.items():
        if not _STAT_PARAM.fullmatch(key):
            continue
        for template in wikitext.parse_all(raw):
            attribute = templates.get(template.name.strip().lower())
            if attribute is None:
                continue
            value = wikitext.strip_markup(template.get("1")).strip()
            if not value:
                continue
            number = _NUMBER.search(value)
            if number is None:
                continue
            amount = float(number.group(0))
            if "%" in value:
                out.append(ItemProperty(attribute, percent_value=amount / 100))
            else:
                out.append(ItemProperty(attribute, flat_value=amount))
    return out


def _recipe_components(page: str) -> List[str]:
    """The item names one item is built from, one level down.

    `{{Recipe}}` nests: the page's own recipe is the outermost one and its `iN`
    parameters hold the components, each itself a `{{Recipe}}`. Note this is the
    *inverse* of Hi-Rez's ChildItemId, which points at what an item builds
    *into* — so the tree has to be inverted after every item is known.
    """
    recipes = wikitext.parse_templates(page, "Recipe", top_level=True)
    if not recipes:
        return []
    out = []
    for key, raw in recipes[0].params.items():
        # `i1`, `i2`, … and not `item`, which also begins with an i and holds
        # the recipe's own name.
        if not _COMPONENT_PARAM.fullmatch(key):
            continue
        # top_level within this parameter: its outermost Recipe is the direct
        # component. Anything deeper is that component's own recipe.
        for nested in wikitext.parse_templates(raw, "Recipe", top_level=True):
            name = wikitext.strip_markup(nested.get("item")).strip()
            if name:
                out.append(name)
            break
    return out


async def _stat_templates(client: WikiClient) -> Dict[str, ItemAttribute]:
    """The stat vocabulary, read from the wiki rather than hardcoded.

    Category:Stat templates is a self-describing list of the 26 stat templates
    in use. Anything it names that we can already resolve keeps working without
    a code change; anything new is reported once and ignored.
    """
    templates = dict(STAT_TEMPLATES)
    try:
        members = await client.category_members(STAT_TEMPLATE_CATEGORY)
    except Exception:  # noqa: BLE001  — a missing category must not stop a load
        return templates
    for member in members:
        name = member.split(":", 1)[-1].strip().lower()
        if name in templates:
            continue
        try:
            templates[name] = ItemAttribute(name)
        except ValueError:
            continue
    return templates


async def load(
    client: WikiClient, silent: bool = False
) -> Tuple[Dict[int, Item], NameIndex]:
    """Every Smite 2 item, keyed by synthetic id, plus a name index."""
    rows = await client.bucket(ITEMS_BUCKET)
    titles = sorted({str(r["page_name"]) for r in rows if r.get("page_name")})
    totals = {
        str(r["page_name"]): r.get("total_cost")
        for r in rows
        if r.get("page_name")
    }

    templates = await _stat_templates(client)
    pages = await client.query_pages(titles)

    files = []
    infoboxes: Dict[str, wikitext.Template] = {}
    for title in titles:
        page = pages.get(title, {}).get("content") or ""
        found = wikitext.parse_templates(page, "Item infobox", top_level=True)
        if not found:
            continue
        infoboxes[title] = found[0]
        image = found[0].get("image")
        if image:
            files.append(image if image.startswith("File:") else f"File:{image}")
    urls = await client.file_urls(files)

    items: Dict[int, Item] = {}
    index = NameIndex()
    components: Dict[int, List[str]] = {}
    unknown_types: Set[str] = set()

    for title in titles:
        infobox = infoboxes.get(title)
        if infobox is None:
            if not silent:
                print(f"smite2: {title} has no item infobox", flush=True)
            continue
        page = pages[title]["content"]

        name = wikitext.strip_markup(infobox.get("name")) or title
        kind = wikitext.strip_markup(infobox.get("type")).strip()

        item = Item()
        item.name = name
        item.id = item_id(_slug(title))
        item.tier = _int(infobox.get("tier"))
        item.price = _int(infobox.get("cost"))
        item.description = ""
        item.icon_id = 0
        item.root_item_id = item.id
        item.parent_item_id = None
        item.active = True
        item.is_starter = kind.lower() == "starter"

        lowered = kind.lower()
        item.type = TYPE_NAMES.get(lowered, ItemType.ITEM)
        if lowered and lowered not in TYPE_NAMES:
            unknown_types.add(kind)

        item.item_properties = _properties(infobox, templates)
        passive = wikitext.strip_markup(infobox.get("passive"))
        item.passive = passive or None
        # Smite 2 has no aura concept, and no PassiveParser grammar — that regex
        # set is written against Smite 1's phrasing, and running it here would
        # produce confident nonsense rather than nothing.
        item.aura = None
        item.passive_properties = set()
        # Nor role restrictions, so every god may build every item.
        item.restricted_roles = []
        item.glyph = False
        item.recipe = False

        image = infobox.get("image")
        item.icon_url = urls.get(
            image if str(image).startswith("File:") else f"File:{image}", ""
        )

        total = totals.get(title)
        item.total_cost = _int(total, item.price) if total is not None else item.price

        items[item.id] = item
        index.add(name, title)
        components[item.id] = _recipe_components(page)

    _link_tree(items, index, components, silent)

    if unknown_types and not silent:
        print(f"smite2: unmapped item types {sorted(unknown_types)}", flush=True)

    return items, index


def _link_tree(
    items: Dict[int, Item],
    index: NameIndex,
    components: Dict[int, List[str]],
    silent: bool,
) -> None:
    """Record what each item is built from.

    `parent_item_id` is named as though it pointed at an item's parent; it does
    not. Hi-Rez calls the same field ChildItemId and it points at the item one
    step *down* the recipe, which is why `compute_item_price` sums by following
    it. Smite 2 matches that direction — but forks, so the single field takes
    the first component and `components` carries them all.
    """
    by_name = {item.name: item.id for item in items.values()}
    missing: Set[str] = set()

    for item_id_, names in components.items():
        resolved: List[int] = []
        for name in names:
            component_id = by_name.get(name)
            if component_id is None:
                canonical = index.get(name)
                component_id = by_name.get(canonical) if canonical else None
            if component_id is None:
                missing.add(name)
                continue
            resolved.append(component_id)
        items[item_id_].components = resolved
        items[item_id_].parent_item_id = resolved[0] if resolved else None

    if missing and not silent:
        print(f"smite2: recipes name unknown items {sorted(missing)}", flush=True)

    # The bottom of each recipe, which only exists once the links do.
    for item in items.values():
        root = item
        seen = {root.id}
        while root.parent_item_id is not None and root.parent_item_id not in seen:
            seen.add(root.parent_item_id)
            root = items[root.parent_item_id]
        item.root_item_id = root.id


def _slug(title: str) -> str:
    from smite2.ids import slugify  # noqa: PLC0415

    return slugify(title)
