"""The browser Smite-le ships as a self-contained daily game.

The web process has no Hi-Rez credentials and anonymous traffic must never be
able to spend the API quota, so the roster and clue art are deliberately a
frozen public asset in the bundle. These checks keep that asset usable and
keep the shared result pointed at the real game route.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "web" / "ui"
CATALOGUE = UI / "public" / "smite-gods.json"
GAME = UI / "src" / "views" / "Smitele.tsx"


def gods():
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))["gods"]


def test_catalogue_has_a_complete_unique_roster():
    roster = gods()

    assert len(roster) >= 100
    assert len({god["id"] for god in roster}) == len(roster)
    assert len({god["name"].casefold() for god in roster}) == len(roster)


def test_every_god_can_supply_all_six_clues():
    for god in gods():
        assert all(
            god[field]
            for field in ("name", "title", "pantheon", "role", "damage", "range")
        ), god["name"]
        assert god["card"].startswith("https://")
        assert god["icon"].startswith("https://")
        assert len(god["abilities"]) == 5
        assert all(
            ability["name"] and ability["icon"].startswith("https://")
            for ability in god["abilities"]
        ), god["name"]


def test_share_result_links_back_to_the_game():
    source = GAME.read_text(encoding="utf-8")

    assert "https://smite.diemer.codes/smitele" in source
    assert "navigator.share" in source
    assert "navigator.clipboard.writeText" in source
