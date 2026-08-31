"""Drawing a build path: what you always buy, and where the game decides.

`build_path` works out that a build opens the same way whatever happens and then
forks on whether you are ahead or behind. Saying that in prose took four lines of
an embed and still left the reader assembling the picture themselves. It is a
picture, so it is drawn as one.

The layout is one row per plan, connected down the left gutter:

    RECOMMENDED  [item][item][item][item][item][item]
                  2,250 4,600 6,850 9,250 11,400 14,300
    AHEAD        [item][item][item][item][item][item]
    BEHIND       [item][item][item][item][item][item]
    RELICS       [item][item]

Every row is a complete build read across, and the first one is always the build
the embed lists in its Items field. That is not obvious housekeeping: the picture
used to be drawn from the two branches alone, and on the live Smite 2 aggregate
twenty-three gods of eighty-eight got a diagram with none of their six listed
items in it. A branch identical to the recommendation is dropped rather than
labelled, so the rows below the first are always real alternatives.

The gold under each tile is cumulative, so the last tile of a row is what that
whole build costs.

Nothing here raises. An icon that will not load becomes a named tile, and the
caller falls back to the plain grid if the whole thing fails, because a build
embed without a diagram is a small loss and no embed at all is a large one.
"""

from __future__ import annotations

import io
from typing import List, Optional, Sequence

from PIL import Image, ImageDraw

from build_path import BuildPath, Step
from item import Item
from item_tree_builder import font

TILE = 128
# Room under a tile for its running gold total.
CAPTION = 26
# Least room at the left for the row labels, which are the annotation the fork
# needs to mean anything. The real gutter is measured from the labels, since a
# long one ("STARTER + RELIC") otherwise renders straight over the first tile.
GUTTER = 150
LABEL_PAD = 22
ROW_GAP = 18
# Breathing room at the right edge, so the last tile is not flush with it.
MARGIN = 10

_LABEL_SIZE = 20
_REASON_SIZE = 15
_GOLD_SIZE = 16

_TEXT = (235, 236, 240, 255)
_MUTED = (150, 155, 165, 255)
_LINE = (110, 116, 130, 255)
_TILE_BACKGROUND = (49, 51, 56, 255)
_TILE_EDGE = (114, 118, 125, 255)


async def render(
    path: BuildPath,
    extras: Sequence[Item] = (),
    extras_label: str = "EXTRAS",
    tile: int = TILE,
) -> Optional[io.BytesIO]:
    """The path as a PNG, or None when there is no fork worth drawing.

    A build that every balance agreed on has one row and no decision in it, and
    is better served by the plain grid the embed already draws.

    `extras` are the starter and relics. They get a row of their own rather than
    trailing the shared one: they are not a step in the build order, and a god
    whose branches agree on nothing has an empty shared row, which would have
    left them labelled as the part of the build you always buy first.
    """
    if path is None or not path.forks:
        return None

    # Each branch is drawn as a continuation of the shared opening rather than
    # on a row of its own, so the picture runs wide instead of tall. Three
    # stacked rows of six tiles made a near-square image, which Discord scales
    # to a third of the available width and stacks the fields under — the whole
    # embed came out tall and narrow with most of the space unused.
    #
    # It also reads better: the shared items *are* the start of both branches,
    # and repeating them is how a build order is actually followed. Only the
    # divergent part is labelled, so the repetition is not mistaken for a
    # different decision.
    shared = list(path.shared)

    # The recommendation is drawn first and unconditionally. It used to be
    # drawn only when it happened to coincide with one of the two branches, and
    # on the live Smite 2 aggregate it did not coincide for twenty-three gods of
    # eighty-eight — so the embed listed six items and showed a picture of six
    # different ones, which for a support was often a full damage build.
    recommended = [step.item.id for step in path.neutral]
    branches = [("RECOMMENDED", "", shared + list(path.neutral), len(shared))]
    for label, note, steps in (
        ("AHEAD", "press", list(path.ahead)),
        ("BEHIND", "survive", list(path.behind)),
    ):
        if not steps or [step.item.id for step in steps] == recommended:
            # A branch identical to the recommendation is not a decision, and
            # drawing it under its own label says there is one.
            continue
        branches.append((label, note, shared + steps, len(shared)))

    branches = [row for row in branches if len(row[2]) > row[3]]
    if len(branches) < 2:
        # Nothing to choose between once the shared opening and the duplicates
        # are taken out; the plain grid says the same thing without implying a
        # choice that isn't being offered.
        return None

    rows = [(label, note, steps) for label, note, steps, _ in branches]
    if extras:
        rows.append((extras_label, "", list(extras)))

    # The gutter is sized to the labels rather than fixed. "STARTER + RELIC" is
    # wider than 150px at this size and was drawn straight over the first tile.
    gutter = _gutter_for(row[0] for row in rows)

    row_height = tile + CAPTION
    width = MARGIN + gutter + max(len(steps) for _, _, steps in rows) * tile
    height = len(rows) * row_height + (len(rows) - 1) * ROW_GAP

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    for index, (label, reason, entries) in enumerate(rows):
        top = index * (row_height + ROW_GAP)
        _draw_label(draw, label, reason, top, tile)
        await _draw_row(canvas, draw, entries, top, gutter, tile)

    _draw_connectors(draw, len(branches), row_height, gutter)

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    return out


def _draw_label(draw, label: str, reason: str, top: int, tile: int) -> None:
    offset = 18 if reason else 8
    draw.text((10, top + tile // 2 - offset), label, font=font(_LABEL_SIZE), fill=_TEXT)
    if reason:
        draw.text(
            (10, top + tile // 2 + 6),
            f"({reason})",
            font=font(_REASON_SIZE),
            fill=_MUTED,
        )


def _gutter_for(labels) -> int:
    """Wide enough for the longest label, never narrower than the default."""
    face = font(_LABEL_SIZE)
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    widest = max(
        (measure.textlength(label, font=face) for label in labels), default=0
    )
    return max(GUTTER, int(widest) + LABEL_PAD + 10)


async def _draw_row(canvas, draw, entries, top: int, gutter: int, tile: int) -> None:
    """One row of tiles. `Step`s carry a running total; bare items do not."""
    for column, entry in enumerate(entries):
        left = gutter + column * tile
        item = entry.item if isinstance(entry, Step) else entry
        await _draw_tile(canvas, item, left, top, tile)
        if isinstance(entry, Step):
            _draw_gold(draw, f"{entry.spent:,}", left, top + tile, tile)


async def _draw_tile(canvas, item: Item, left: int, top: int, tile: int) -> None:
    try:
        with await item.get_icon_bytes() as raw:
            with Image.open(raw) as image:
                icon = image.convert("RGBA")
                if icon.size != (tile, tile):
                    icon = icon.resize((tile, tile))
                canvas.paste(icon, (left, top))
                return
    except Exception as ex:  # pylint: disable=broad-except
        print(f"Unable to draw {item.name} into a build path: {ex}")
    canvas.paste(_named_tile(item.name, tile), (left, top))


def _named_tile(name: str, tile: int) -> Image.Image:
    """The fallback for art that will not load: the item's name, wrapped."""
    out = Image.new("RGBA", (tile, tile), _TILE_BACKGROUND)
    draw = ImageDraw.Draw(out)
    draw.rectangle([0, 0, tile - 1, tile - 1], outline=_TILE_EDGE)

    face = font(_GOLD_SIZE)
    lines: List[str] = []
    current = ""
    for word in name.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=face) > tile - 12:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    line_height = _GOLD_SIZE + 4
    start = max(4, (tile - len(lines[:5]) * line_height) // 2)
    for index, line in enumerate(lines[:5]):
        draw.text((6, start + index * line_height), line, font=face, fill=_TEXT)
    return out


def _draw_gold(draw, text: str, left: int, top: int, tile: int) -> None:
    face = font(_GOLD_SIZE)
    width = draw.textlength(text, font=face)
    draw.text((left + (tile - width) / 2, top + 5), text, font=face, fill=_MUTED)


def _draw_connectors(draw, rows: int, row_height: int, gutter: int) -> None:
    """A spine down the gutter linking the shared row to each branch.

    Drawn in the label gutter rather than between the tiles so it never crosses
    an icon.
    """
    if rows < 2:
        return
    spine = gutter - 16
    first = row_height // 2
    last = (rows - 1) * (row_height + ROW_GAP) + row_height // 2
    draw.line([(spine, first), (spine, last)], fill=_LINE, width=3)
    for index in range(1, rows):
        middle = index * (row_height + ROW_GAP) + row_height // 2
        draw.line([(spine, middle), (gutter - 2, middle)], fill=_LINE, width=3)
