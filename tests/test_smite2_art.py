"""The wiki serves art the Hi-Rez CDN never did, and three rounds assumed
otherwise.

`/smitele game:Smite 2` died on `cannot write mode RGBA as JPEG` — the crop
round has always saved JPEG, and until now every card it was handed was already
RGB. The wiki also serves cutout renders on transparency, where a uniformly
random square is often entirely background.

The other two are data shape rather than encoding: prism recolours inherit their
parent skin's full art, so taking the first image named would give a god five
identical "different" skins; and the wiki gives the basic attack an `{{Ability}}`
block of its own, which Smite 1 does not.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

os.environ.setdefault("SMITELE_DISCORD_TOKEN", "test-token")
os.environ.setdefault("SMITELE_HIREZ_DEV_ID", "0")
os.environ.setdefault("SMITELE_HIREZ_AUTH_KEY", "0")

Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
pytest.importorskip("discord", reason="py-cord not installed")

from smite2 import gods as gods_module  # noqa: E402
from smite2 import wikitext  # noqa: E402


def crop_of(img):
    from smitele_bot import Smitele

    out = io.BytesIO()
    Smitele._Smitele__random_crop(img, out)
    result = Image.open(out)
    result.load()
    return result


class TestRandomCrop:
    def test_rgba_does_not_raise(self):
        """The reported crash: Pillow refuses to write alpha as JPEG."""
        assert crop_of(Image.new("RGBA", (1280, 720), (255, 0, 0, 255)))

    def test_palette_does_not_raise(self):
        assert crop_of(Image.new("P", (800, 800)))

    def test_rgb_still_works(self):
        """Every Smite 1 card. Must be untouched."""
        assert crop_of(Image.new("RGB", (1024, 1024), (0, 128, 0)))

    def test_output_is_the_expected_size(self):
        assert crop_of(Image.new("RGB", (1024, 1024))).size == (180, 180)

    def test_crop_lands_on_the_subject_not_the_transparency(self):
        """A cutout render on a transparent field. A uniformly random square
        would usually be empty; the crop is taken from the opaque bounding box.
        """
        img = Image.new("RGBA", (1000, 1000), (0, 0, 0, 0))
        img.paste(Image.new("RGBA", (300, 300), (255, 0, 0, 255)), (600, 600))

        for _ in range(25):
            assert crop_of(img).convert("L").getextrema()[1] > 0

    def test_a_subject_smaller_than_the_crop_falls_back_to_the_frame(self):
        """The bounding box can be narrower than a quarter of the width, which
        would leave `randint` an empty range."""
        img = Image.new("RGBA", (1000, 1000), (0, 0, 0, 0))
        img.paste(Image.new("RGBA", (10, 10), (255, 0, 0, 255)), (500, 500))
        assert crop_of(img).size == (180, 180)


SKINVIEWER = (
    "==Skins==\n"
    "{{#invoke:SkinViewer|main"
    "|skin1=Default"
    "|skin1_img=T Neith Default.png"
    "|skin1_model=T Neith Default Model.png"
    "|skin1_prism1=Shadow"
    "|skin1_prism1_img=T Neith Default.png"
    "|skin1_prism1_model=T Neith Shadow Model.png"
    "|skin1_prism2=Onyx"
    "|skin1_prism2_img=T Neith Default.png"
    "|skin1_prism2_model=T Neith Onyx Model.png"
    "}}\n"
)


class Named:
    id = 1


class TestSkinArt:
    """`skin1_prism1_img` is the *parent's* full art — the wiki publishes no
    separate art for a recolour. Taking it at face value gave three skins one
    picture, which made the skin round and the base-card round identical."""

    @staticmethod
    def skins():
        urls = {
            f"File:{name}": f"http://wiki/{name}"
            for name in (
                "T Neith Default.png",
                "T Neith Default Model.png",
                "T Neith Shadow Model.png",
                "T Neith Onyx Model.png",
            )
        }
        return gods_module._skins(SKINVIEWER, Named(), urls)

    def test_every_skin_is_found(self):
        assert [s.name for s in self.skins()] == ["Default", "Shadow", "Onyx"]

    def test_no_two_skins_share_a_picture(self):
        urls = [s.card_url for s in self.skins()]
        assert len(set(urls)) == len(urls)

    def test_the_base_skin_keeps_its_full_art(self):
        """Only the recolours fall through to the model render."""
        assert self.skins()[0].card_url.endswith("T Neith Default.png")

    def test_a_skin_with_no_art_at_all_is_dropped(self):
        assert gods_module._skins(SKINVIEWER, Named(), {}) == []


ABILITIES = (
    "==Abilities==\n"
    "{{Ability|slot=Basic Attack|name=Neith Basic Attack"
    "|icon=Icons2 BasicAttack Physical.png|description=x}}\n"
    "{{Ability|slot=Passive|name=Broken Weave"
    "|icon=Icons2 Neith Passive.png|description=x}}\n"
    "{{Ability|slot=1st Ability|name=Spirit Arrow"
    "|icon=Icons2 Neith A01.png|description=x}}\n"
)


class TestBasicAttackExclusion:
    """Its name is "<God> Basic Attack" and its icon is one of two files shared
    across the roster, so trivia asked questions with forty-odd valid answers
    and `/smitele` showed every magical god the same picture."""

    def test_it_is_not_in_the_kit(self):
        names = [a.name for a in gods_module._abilities(ABILITIES)]
        assert names == ["Broken Weave", "Spirit Arrow"]

    def test_its_icon_is_not_fetched(self):
        assert "Icons2 BasicAttack Physical.png" not in gods_module._ability_icons(
            ABILITIES
        )

    def test_the_real_abilities_keep_theirs(self):
        assert gods_module._ability_icons(ABILITIES) == [
            "Icons2 Neith Passive.png",
            "Icons2 Neith A01.png",
        ]

    def test_the_slot_is_what_identifies_it_not_the_name(self):
        """A god renamed on the wiki must still have its basic attack dropped."""
        page = ABILITIES.replace("Neith Basic Attack", "Whatever They Call It")
        assert "Whatever They Call It" not in [
            a.name for a in gods_module._abilities(page)
        ]
