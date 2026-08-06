"""Rendering an item tree, which is what a trivia ITEMS question does.

The tree asked PIL for arial.ttf — present on Windows, where this ran before
containerisation, absent from python-slim. Every ITEMS question therefore died
with `OSError: cannot open resource`, for both games, and nothing noticed
because no test ever drew one.
"""

from __future__ import annotations

import pytest

PIL = pytest.importorskip("PIL", reason="pillow not installed")

from item_tree_builder import _FONT_CANDIDATES, _placeholder_font  # noqa: E402


def test_a_font_is_always_returned():
    """PIL's built-in is the floor. An exception here is no trivia answer."""
    assert _placeholder_font(64) is not None


def test_the_font_can_actually_render():
    from PIL import Image, ImageDraw

    with Image.new("RGB", (96, 96)) as image:
        ImageDraw.Draw(image).text((32, 19), "?", font=_placeholder_font(64))


def test_fonts_are_cached_per_size():
    assert _placeholder_font(64) is _placeholder_font(64)


def test_arial_is_not_relied_on():
    """It is still tried last, for a Windows checkout, but must not be first —
    that is exactly how this broke."""
    assert _FONT_CANDIDATES[0] != "arial.ttf"
    assert "arial.ttf" in _FONT_CANDIDATES


def test_a_missing_font_falls_back_rather_than_raising(monkeypatch):
    import item_tree_builder

    monkeypatch.setattr(item_tree_builder, "_font_cache", {})
    monkeypatch.setattr(
        item_tree_builder, "_FONT_CANDIDATES", ("definitely-not-a-font.ttf",)
    )
    assert item_tree_builder._placeholder_font(64) is not None
