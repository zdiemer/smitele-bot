"""Reading structured values back out of MediaWiki source.

wiki.smite2.com is the only published source for Smite 2 god abilities, item
tiers, costs and passives — tracker.gg carries none of it — so everything the
static-data layer knows comes through here.

The parsing is a brace-matching scanner rather than a regex, and that is not
fastidiousness. Template parameters routinely contain pipes that are not
parameter separators:

    |stat1={{Int|30}}
    |role1=[[File:S2 Role Mid.png|link=Mid|25px]] [[Mid]]
    |damage=60 {{!}} 85 {{!}} 110 {{!}} 135 {{!}} 160

A `split("|")` gets the first two wrong, and the third is the shape every
per-rank ability value uses, so getting it wrong silently yields plausible
nonsense — a cooldown of 10 where the real value is "10 at rank 1". Splitting
only at brace depth zero handles all three, and leaves `{{!}}` intact as
literal text for `rank_values` to split on afterwards.
"""

from __future__ import annotations

import re
from typing import Dict, List, NamedTuple, Optional, Tuple

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_REF = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SECTION = re.compile(r"^==([^=].*?)==\s*$", re.MULTILINE)
_LEADING_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


class Template(NamedTuple):
    """One `{{Name|...}}` call, with its parameters resolved to a dict.

    Anonymous parameters are keyed by their 1-based position as a string, which
    is how MediaWiki numbers them, so `{{Ia|foo.ogg}}` gives `{"1": "foo.ogg"}`.

    `depth` is nesting depth within the text it was parsed from, zero being
    outermost. Item recipes are trees of `{{Recipe}}` inside `{{Recipe}}`, so
    "the recipe on this page" means depth zero and the rest are its components;
    a parameter's value keeps its nested source verbatim, so descending is a
    matter of re-parsing `params["i1"]`.
    """

    name: str
    params: Dict[str, str]
    depth: int = 0
    # Offset of the opening `{{`. Only used to restore source order, since the
    # scanner necessarily finishes an inner template before its parent.
    start: int = -1

    def get(self, key: str, default: str = "") -> str:
        return self.params.get(key, default)


def normalize_name(name: str) -> str:
    """Template names as MediaWiki compares them: underscores are spaces, case
    is insignificant except that we lower everything, and surrounding space is
    noise. `God_infoboxS2` and `god infoboxs2` are the same template."""
    return name.replace("_", " ").strip().lower()


def _skip_link(text: str, index: int) -> int:
    """Index just past the `]]` closing the link starting at `index`.

    Links nest — `[[File:x.png|thumb|[[Mid]]]]` is legal — so this counts rather
    than searching for the first `]]`.
    """
    depth = 0
    while index < len(text):
        pair = text[index : index + 2]
        if pair == "[[":
            depth += 1
            index += 2
            continue
        if pair == "]]":
            depth -= 1
            index += 2
            if depth <= 0:
                return index
            continue
        index += 1
    return len(text)


def _scan(
    text: str, index: int, found: List[Template], depth: int = 0
) -> Tuple[Optional[str], int]:
    """Consume the template starting at `index`, appending it and anything
    nested inside it to `found`. Returns its name and the index just past it.

    Parameters are split on `|` only at depth zero, and the *separator* `=` is
    likewise tracked at depth zero — otherwise `{{Foo|{{Bar|a=1}}}}` reads its
    anonymous parameter as a named one called `{{Bar|a`.
    """
    start = index
    index += 2  # past "{{"
    parts: List[Tuple[str, Optional[int]]] = []
    buffer: List[str] = []
    equals_at: Optional[int] = None
    width = 0

    def flush() -> None:
        nonlocal buffer, equals_at, width
        parts.append(("".join(buffer), equals_at))
        buffer = []
        equals_at = None
        width = 0

    while index < len(text):
        pair = text[index : index + 2]

        if pair == "}}":
            flush()
            template = _build(parts, depth, start)
            if template is not None:
                found.append(template)
            return (template.name if template is not None else None), index + 2

        # `nested` rather than reusing `start`, which holds this template's own
        # opening offset and is what restores source order.
        if pair == "{{":
            nested = index
            _, index = _scan(text, index, found, depth + 1)
            raw = text[nested:index]
            buffer.append(raw)
            width += len(raw)
            continue

        if pair == "[[":
            nested = index
            index = _skip_link(text, index)
            raw = text[nested:index]
            buffer.append(raw)
            width += len(raw)
            continue

        character = text[index]
        if character == "|":
            flush()
            index += 1
            continue
        if character == "=" and equals_at is None:
            equals_at = width

        buffer.append(character)
        width += 1
        index += 1

    # Unterminated template. Everything after it is unparseable anyway, so bail
    # rather than guessing where it was meant to end.
    return None, len(text)


def _build(
    parts: List[Tuple[str, Optional[int]]], depth: int, start: int
) -> Optional[Template]:
    if not parts:
        return None

    name = parts[0][0].strip()
    if not name:
        return None

    params: Dict[str, str] = {}
    position = 0
    for raw, equals_at in parts[1:]:
        if equals_at is None:
            position += 1
            params[str(position)] = raw.strip()
            continue
        key = raw[:equals_at].strip()
        value = raw[equals_at + 1 :].strip()
        params[key] = value

    return Template(name=name, params=params, depth=depth, start=start)


def parse_all(text: str) -> List[Template]:
    """Every template in the text, at any nesting depth, in source order.

    Sorted, because the scanner cannot append in source order: a template is
    only finished once its closing braces are reached, so nested ones complete
    first and would come back before their parent. Left unsorted, "the first
    Recipe in this parameter" returns the innermost one — which reads an item's
    recipe as its grandchildren.
    """
    text = _COMMENT.sub("", text)
    found: List[Template] = []
    index = 0
    while index < len(text):
        if text[index : index + 2] == "{{":
            _, index = _scan(text, index, found)
            continue
        if text[index : index + 2] == "[[":
            index = _skip_link(text, index)
            continue
        index += 1
    return sorted(found, key=lambda template: template.start)


def parse_templates(
    text: str, name: str, top_level: bool = False
) -> List[Template]:
    """Every `{{name|...}}` on the page, in source order.

    `top_level` keeps only the outermost ones, which is what "the recipe for
    this item" means — Book of Thoth's page holds six `{{Recipe}}` calls
    describing one tree.
    """
    wanted = normalize_name(name)
    return [
        t
        for t in parse_all(text)
        if normalize_name(t.name) == wanted and (not top_level or t.depth == 0)
    ]


def sections(text: str) -> Dict[str, str]:
    """Top-level `== Heading ==` sections, keyed by heading.

    Only level two, deliberately. Gods with an Aspect repeat their `{{Ability}}`
    blocks under `==God Aspect==` in an enhanced form, so an ability parser fed
    the whole page sees each ability twice and picks whichever it hits first.
    Scoping to the Abilities section is what makes that safe, and it only works
    if subsection headings don't split the block.
    """
    # Strip once and index into the stripped text — matching on one string and
    # slicing another shifts every boundary by the length of the comments.
    text = _COMMENT.sub("", text)
    out: Dict[str, str] = {}
    matches = list(_SECTION.finditer(text))
    for position, match in enumerate(matches):
        end = (
            matches[position + 1].start()
            if position + 1 < len(matches)
            else len(text)
        )
        out[match.group(1).strip()] = text[match.end() : end]
    return out


def strip_markup(value: str) -> str:
    """Wikitext down to something renderable in a Discord embed.

    Link targets give way to their display text, `{{!}}` becomes the pipe it
    stands for, and HTML — the wiki colours item passives with inline spans —
    goes entirely.
    """
    value = _COMMENT.sub("", value)
    value = _REF.sub("", value)
    value = value.replace("{{!}}", "|")
    value = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", value)
    value = re.sub(r"\[\[([^\]]*)\]\]", r"\1", value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = _TAG.sub("", value)
    value = value.replace("'''", "").replace("''", "")
    value = value.replace("&nbsp;", " ").replace("&amp;", "&")
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


# The rank separator, in both spellings. `{{!}}` is how it is written in the
# source; a bare pipe is what it becomes once markup is stripped. Splitting on
# only the former quietly truncated every rank array to its first entry —
# a cooldown of 10 where the ability really runs 10/9.5/9/8.5/8.
_RANK_SEPARATOR = re.compile(r"\{\{!\}\}|\|")


def rank_values(value: str) -> List[float]:
    """The per-rank numbers in `60 {{!}} 85 {{!}} 110 {{!}} 135 {{!}} 160`.

    Returns one entry per rank, so a single number gives a one-element list and
    callers can treat "same at every rank" and "scales" identically. Anything
    without a leading number — "None", "N/A" — drops out, because a rank array
    with a zero standing in for "no cost" would read as a real value of zero.
    """
    out: List[float] = []
    for piece in _RANK_SEPARATOR.split(value):
        match = _LEADING_NUMBER.search(strip_markup(piece))
        if match is not None:
            out.append(float(match.group(0)))
    return out


def parse_stat_line(line: str) -> Optional[Tuple[str, List[float], str]]:
    """`*Cooldown: 10 {{!}} 9.5 {{!}} 9 seconds` → `("Cooldown", [...], "seconds")`.

    Returns None for a line that carries no label/value pair, which is most of
    the prose in an ability's `stats` parameter.
    """
    text = line.strip().lstrip("*:# ").strip()
    if not text:
        return None

    label, separator, remainder = strip_markup(text).partition(":")
    if not separator:
        return None

    label = label.strip()
    values = rank_values(remainder)
    if not label or not values:
        return None

    # Whatever trails the final number: "seconds", "%", "meters". Split with
    # the same separator the values used, or the unit picks up the rest of the
    # rank array instead.
    tail = strip_markup(_RANK_SEPARATOR.split(remainder)[-1])
    match = _LEADING_NUMBER.search(tail)
    unit = tail[match.end() :].strip() if match is not None else ""

    return label, values, unit


def parse_stat_block(value: str) -> Dict[str, Tuple[List[float], str]]:
    """Every labelled line in an ability's `stats` parameter, keyed by label."""
    out: Dict[str, Tuple[List[float], str]] = {}
    for line in value.splitlines():
        parsed = parse_stat_line(line)
        if parsed is None:
            continue
        label, values, unit = parsed
        out.setdefault(label, (values, unit))
    return out
