"""The link-preview card, drawn from whatever the snapshot last wrote.

Nothing here is checked into the repository and nothing is generated at build
time. A static card would be a picture of the numbers on the day someone
exported it, and this site's whole point is that its numbers move — a preview
claiming 158M player records and a healthy crawl, pinned six weeks after both
stopped being true, is worse than no preview.

So it is drawn on demand and cached against the snapshot files' mtimes: it
re-renders when the data changes and not once more. A crawler that fetches the
card a thousand times gets one render and 999 cache hits.

Pillow rather than an SVG rasteriser because Pillow is already a dependency and
DejaVu is already in the base image — a preview card is not worth adding a
rendering stack for. The cost is that this draws with rectangles and text
instead of paths, so the bolt is a polygon literal rather than SVG `d` data.
"""

from __future__ import annotations

import io
import os
from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 630

# The site's dark palette. A preview is a small bright rectangle in somebody's
# chat client, and the dark theme holds together at thumbnail size where the
# paper one turns into a grey smear.
INK = (20, 23, 26)
TEXT = (228, 231, 226)
TEXT_2 = (180, 186, 180)
DIM = (126, 133, 126)
RULE = (42, 47, 51)
SMITE1 = (74, 154, 216)
SMITE2 = (208, 106, 170)
HEALTHY = (99, 183, 156)
LATE = (211, 162, 73)
REFUSE = (232, 115, 74)
WHITE = (255, 255, 255)

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def _bolt(
    draw: ImageDraw.ImageDraw, x: int, y: int, scale: float, radius: int = 0
) -> None:
    """The mark, as a polygon.

    The same outline as `icon.svg`, transcribed — if that file changes, this has
    to change with it, which is the price of not shipping a rasteriser.

    `radius` draws the dark tile behind it, which the standalone icon needs and
    the card does not: the card already has a dark ground.
    """
    if radius:
        draw.rounded_rectangle(
            [x, y, x + 64 * scale, y + 64 * scale], radius=radius, fill=INK + (255,)
        )

    points = [(37, 7), (21, 34), (31, 34), (27, 57), (47, 26), (34, 26)]
    draw.polygon([(x + px * scale, y + py * scale) for px, py in points], fill=WHITE)

    # Motion lines: blue trailing, magenta thrown ahead, fading outward.
    streaks = (
        ((9, 20, 20, 20), SMITE1, 255),
        ((6, 30, 16, 30), SMITE1, 180),
        ((11, 40, 18, 40), SMITE1, 115),
        ((46, 26, 55, 26), SMITE2, 115),
        ((48, 36, 58, 36), SMITE2, 180),
        ((44, 46, 55, 46), SMITE2, 255),
    )
    for (x1, y1, x2, y2), colour, alpha in streaks:
        draw.line(
            [
                (x + x1 * scale, y + y1 * scale),
                (x + x2 * scale, y + y2 * scale),
            ],
            fill=colour + (alpha,),
            width=max(int(3.5 * scale), 1),
        )


def icon(size: int = 180) -> bytes:
    """The mark alone, as a square PNG. For iOS, which will not take SVG.

    Drawn from the same polygon as the card rather than checked in as a binary,
    so there is one shape to change instead of three — and a rasteriser stays
    out of the build. `icon.svg` is still a separate transcription, because a
    favicon wants real vector crispness at 16px.
    """
    tile = Image.new("RGBA", (size, size), INK + (255,))
    draw = ImageDraw.Draw(tile)
    scale = size / 64
    _bolt(draw, 0, 0, scale, radius=int(12 * scale))

    buffer = io.BytesIO()
    tile.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _number(value: Any) -> Optional[str]:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return None


def _facts(status: Dict[str, Any], stats: Dict[str, Any]) -> Tuple[list, list]:
    """Two columns of live figures, skipping anything the snapshot lacks.

    Every line is optional. A card with three true lines beats one with five
    where two say "—", and a preview is the one surface where a placeholder is
    indistinguishable from a broken site.
    """
    left, right = [], []

    games = (stats or {}).get("games") or {}
    smite = games.get("smite") if isinstance(games.get("smite"), dict) else {}
    if smite.get("built"):
        per_day = smite.get("matches_per_day")
        if isinstance(per_day, dict) and per_day.get("all"):
            total = sum(row["matches"] for row in per_day["all"])
            left.append((f"{total:,} matches over {len(per_day['all'])} days", DIM))
        gods = _number(smite.get("distinct_gods"))
        plays = _number(smite.get("total_plays"))
        if gods and plays:
            left.append((f"{gods} gods · {plays} player records", DIM))

    hirez = (status or {}).get("hirez") or {}
    quota = hirez.get("quota") if isinstance(hirez, dict) else None
    if isinstance(quota, dict) and "requests_today" in quota:
        share = quota["requests_today"] / max(quota.get("requests_limit") or 1, 1)
        right.append(
            (
                f"· Hi-Rez {quota['requests_today']:,} / "
                f"{quota.get('requests_limit', 0):,}",
                HEALTHY if share < 0.6 else LATE,
            )
        )

    tracker = (status or {}).get("tracker") or {}
    standdown = tracker.get("standdown") if isinstance(tracker, dict) else None
    if isinstance(standdown, dict):
        if standdown.get("active"):
            minutes = int((standdown.get("remaining_seconds") or 0) / 60)
            right.append((f"× tracker.gg blocked · {minutes}m left", REFUSE))
        else:
            right.append(("· tracker.gg clear", HEALTHY))

    return left, right


def render(status: Dict[str, Any], stats: Dict[str, Any]) -> bytes:
    """A 1200×630 PNG of the current state."""
    image = Image.new("RGB", (WIDTH, HEIGHT), INK)
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # The masthead rule, split at the middle — the same device the site uses to
    # say "two games, one timeline".
    draw.rectangle([0, 0, WIDTH // 2, 6], fill=SMITE1 + (255,))
    draw.rectangle([WIDTH // 2, 0, WIDTH, 6], fill=SMITE2 + (255,))

    _bolt(draw, 84, 72, 2.4)

    title = _font("DejaVuSans-Bold.ttf", 70)
    mono = _font("DejaVuSansMono.ttf", 27)
    small = _font("DejaVuSansMono.ttf", 23)

    draw.text((84, 286), "smite", font=title, fill=TEXT + (255,))
    width = draw.textlength("smite", font=title)
    draw.text((84 + width, 286), ".diemer.codes", font=title, fill=SMITE1 + (255,))

    draw.text(
        (84, 372),
        "data & API liveness for Smite 1 and Smite 2",
        font=mono,
        fill=TEXT_2 + (255,),
    )

    # Two columns: corpus facts from the left margin, health right-aligned to
    # the rule. Right-aligned rather than at a fixed x because the left lines
    # grow with the corpus — at 158 million records they already reached the
    # column that was sitting at x=660 and overlapped it.
    left, right = _facts(status, stats)
    for index, (line, colour) in enumerate(left[:2]):
        draw.text((84, 478 + index * 40), line, font=small, fill=colour + (255,))
    for index, (line, colour) in enumerate(right[:2]):
        width = draw.textlength(line, font=small)
        draw.text(
            (WIDTH - 84 - width, 478 + index * 40),
            line,
            font=small,
            fill=colour + (255,),
        )

    draw.rectangle([84, 574, WIDTH - 84, 576], fill=RULE + (255,))

    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
