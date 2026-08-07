"""Drawing a build path.

The picture replaces four lines of prose in the embed, so what matters is that
it renders at all, that every row it claims to draw is there, and that a broken
icon degrades to a named tile rather than taking the embed down with it.
"""

from __future__ import annotations

import asyncio
import io
import zlib

import pytest

PIL = pytest.importorskip("PIL", reason="pillow not installed")

from PIL import Image  # noqa: E402

import build_path_image  # noqa: E402
from build_path import BuildPath, Step  # noqa: E402
from item import Item, ItemType  # noqa: E402


def _stable_id(name: str) -> int:
    return zlib.crc32(name.encode()) % 10_000_000


def make_item(name, icon=True):
    item = Item()
    item.name = name
    item.id = _stable_id(name)
    item.tier = 3
    item.price = item.total_cost = 2500
    item.active = True
    item.is_starter = False
    item.type = ItemType.ITEM
    item.item_properties = []
    item.passive = None
    # A URL that resolves to nothing, so every tile takes the fallback path
    # unless a test says otherwise.
    item.icon_url = "https://example.invalid/icon.png" if icon else ""
    item.restricted_roles = []
    item.glyph = False
    return item


def steps(names, start=1000):
    spent = 0
    out = []
    for index, name in enumerate(names):
        spent += start + index * 100
        out.append(Step(make_item(name), spent))
    return out


def render(path, extras=(), label="EXTRAS"):
    return asyncio.run(build_path_image.render(path, extras, extras_label=label))


def opened(image_bytes: io.BytesIO) -> Image.Image:
    return Image.open(image_bytes)


class TestWhenItDraws:
    def test_a_path_that_never_forks_is_not_drawn(self):
        """One row with no decision in it is the plain grid's job."""
        path = BuildPath(steps(["A", "B"]), [], [])
        assert render(path) is None

    def test_no_path_at_all_is_not_drawn(self):
        assert render(None) is None

    def test_a_forking_path_is_drawn(self):
        path = BuildPath(steps(["A"]), steps(["B"]), steps(["C"]))
        assert render(path) is not None


class TestLayout:
    def test_every_row_gets_drawn(self):
        """Shared, ahead, behind and the extras row: four bands of tiles."""
        path = BuildPath(steps(["A"]), steps(["B"]), steps(["C"]))
        image = opened(render(path, extras=[make_item("Relic")]))
        row_height = build_path_image.TILE + build_path_image.CAPTION
        expected = 4 * row_height + 3 * build_path_image.ROW_GAP
        assert image.height == expected

    def test_a_row_with_no_steps_is_skipped(self):
        """A build whose branches agree on nothing has no shared row, and must
        not leave a band of empty space where one would have been."""
        path = BuildPath([], steps(["B"]), steps(["C"]))
        image = opened(render(path))
        row_height = build_path_image.TILE + build_path_image.CAPTION
        assert image.height == 2 * row_height + build_path_image.ROW_GAP

    def test_width_follows_the_longest_row(self):
        short = opened(render(BuildPath(steps(["A"]), steps(["B"]), steps(["C"]))))
        long = opened(
            render(BuildPath(steps(["A"]), steps(["B", "C", "D", "E"]), steps(["F"])))
        )
        assert long.width > short.width
        assert long.width == (
            build_path_image.MARGIN + build_path_image.GUTTER + 4 * build_path_image.TILE
        )

    def test_extras_widen_the_image_when_they_are_the_longest_row(self):
        path = BuildPath(steps(["A"]), steps(["B"]), steps(["C"]))
        extras = [make_item(f"R{i}") for i in range(5)]
        image = opened(render(path, extras=extras))
        assert image.width == (
            build_path_image.MARGIN + build_path_image.GUTTER + 5 * build_path_image.TILE
        )

    def test_it_is_a_transparent_png(self):
        path = BuildPath(steps(["A"]), steps(["B"]), steps(["C"]))
        image = opened(render(path))
        assert image.mode == "RGBA"
        assert image.format == "PNG"


class TestFailureIsNotFatal:
    def test_an_icon_that_will_not_load_becomes_a_named_tile(self):
        """`art_cache.fetch` always returns bytes, possibly of garbage, so the
        renderer has to survive `Image.open` raising on every single tile."""
        path = BuildPath(steps(["Unloadable"]), steps(["Also"]), steps(["Broken"]))
        image = render(path)
        assert image is not None
        # Something was drawn into the tile area rather than left transparent.
        opened_image = opened(image)
        tile = opened_image.crop(
            (
                build_path_image.GUTTER,
                0,
                build_path_image.GUTTER + build_path_image.TILE,
                build_path_image.TILE,
            )
        )
        assert tile.getextrema()[3][1] > 0

    def test_an_item_with_a_very_long_name_still_fits_a_tile(self):
        name = "Extraordinarily Overlong Ceremonial Item Of Considerable Verbosity"
        tile = build_path_image._named_tile(name, build_path_image.TILE)
        assert tile.size == (build_path_image.TILE, build_path_image.TILE)
