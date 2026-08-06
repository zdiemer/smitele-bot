"""Trivia builds its questions from god and item fields, and Smite 2 does not
populate the same ones Smite 1 does.

`/trivia game:Smite 2` crashed on `god.role.name` because the question bank was
a fixed list that assumed every Smite 1 field existed. The unit tests passed;
nothing generated a question from a Smite 2 god. These do.
"""

from __future__ import annotations

import types

import pytest

discord = pytest.importorskip("discord", reason="py-cord not installed")

from god_types import GodPro, GodRole  # noqa: E402
from item import Item, ItemProperty, ItemAttribute, ItemType  # noqa: E402
from ability import Ability, _item, _itemDescription  # noqa: E402
from smitetrivia import GodQuestionGenerator, ItemQuestionGenerator  # noqa: E402
from HirezAPI import PlayerRole  # noqa: E402


def fake_abilities():
    """Two abilities in the shape both games produce: slash-separated ranks."""
    return [
        Ability(
            _itemDescription(
                cooldown="10/9.5/9/8.5/8",
                cost="60/65/70/75/80",
                description="Deals damage in a line.",
                menu_items=[_item("Damage:", "60/85/110")],
                rank_items=[_item("Damage:", "60/85/110")],
            ),
            id=1,
            name="Plague of Locusts",
            icon_url="http://x/a.png",
            is_passive=False,
        )
    ]


def smite1_god():
    """What the Hi-Rez API gives: a class, Pros, no positions or Aspect."""
    return types.SimpleNamespace(
        name="Anubis",
        lore="Anubis weighs the hearts of the dead.",
        pantheon="Egyptian",
        title="God of the Dead",
        pros=[GodPro.HIGH_AREA_DAMAGE],
        role=GodRole.MAGE,
        positions=[],
        specs=[],
        aspect=None,
        abilities=fake_abilities(),
        id=1,
    )


def smite2_god(**overrides):
    """What the wiki gives: no class, no Pros, positions and specs instead."""
    base = dict(
        name="Hecate",
        lore="Hecate guards the crossroads.",
        pantheon="Greek",
        title="Goddess of Witchcraft",
        pros=[],
        role=None,
        positions=[PlayerRole.MID],
        specs=["Nuker", "Lockdown"],
        aspect=types.SimpleNamespace(name="Aspect of Ruin", description="", icon_url=None),
        abilities=fake_abilities(),
        id=2,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class FakeProvider:
    async def get_god_skins(self, _god_id):
        return []


def questions_for(god):
    generator = GodQuestionGenerator(god, FakeProvider())
    embed, question, _file = generator.question
    return generator, embed, question


class TestGodQuestions:
    def test_a_smite2_god_does_not_crash(self):
        """The reported bug: role is None, and `god.role.name` raised."""
        for _ in range(30):
            _, embed, question = questions_for(smite2_god())
            assert question.get_answer()
            assert embed.description

    def test_a_smite1_god_still_asks_about_its_class(self):
        asked = set()
        for _ in range(60):
            _, _, question = questions_for(smite1_god())
            asked.add(question.question)
        assert any("What role is" in q for q in asked)

    def test_a_smite2_god_is_asked_about_positions_and_aspect(self):
        asked = set()
        for _ in range(80):
            _, _, question = questions_for(smite2_god())
            asked.add(question.question)
        assert any("position" in q for q in asked)
        assert any("Aspect of Ruin" in q for q in asked)

    def test_no_question_is_generated_without_an_answer(self):
        """An empty `pros` list produced a question whose answer list was
        empty, and `get_answer()` indexes [0] on it."""
        for god in (smite1_god(), smite2_god()):
            for _ in range(40):
                _, _, question = questions_for(god)
                assert question.get_answer(), question.question

    def test_a_god_missing_everything_optional_still_works(self):
        bare = smite2_god(
            lore="", pantheon="", title="", positions=[], specs=[], aspect=None
        )
        # Nothing left to ask, which must be empty rather than a crash — the
        # caller falls back to ability questions.
        generator = GodQuestionGenerator(bare, FakeProvider())
        assert generator is not None


def make_item(kind: ItemType, **overrides):
    item = Item()
    item.name = "Book of Thoth"
    item.type = kind
    item.price = 850
    item.total_cost = 2300
    item.parent_item_id = None
    item.components = []
    item.icon_url = "http://x/i.png"
    item.passive = "Stacks mana."
    item.aura = None
    item.item_properties = [ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=30)]
    item.id = 1
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


class TestItemQuestions:
    def test_a_consumable_without_a_passive_is_not_asked_about_it(self):
        """Smite 2 relics and consumables often have no passive text, which
        rendered as "Name the consumable with this description: `None`"."""
        item = make_item(ItemType.CONSUMABLE, passive=None, name="Potion")
        asked = set()
        for _ in range(40):
            _, question, _f = ItemQuestionGenerator(item, {item.id: item}).question
            asked.add(question.question)
        assert not any("None" in q for q in asked)

    def test_a_relic_without_a_passive_still_has_a_question(self):
        item = make_item(ItemType.RELIC, passive="", name="Beads")
        _, question, _f = ItemQuestionGenerator(item, {item.id: item}).question
        assert question.get_answer() == "Beads"

    def test_cost_uses_the_stated_total_not_a_single_branch(self):
        """Smite 2 recipes fork. Walking one branch reported 1,650 for an item
        that costs 2,300 — a wrong answer, not a missing one."""
        thoth = make_item(ItemType.ITEM)
        oracle = make_item(ItemType.ITEM, name="Oracle Staff", price=800, total_cost=800)
        oracle.id = 2
        thoth.parent_item_id = oracle.id  # only one of its two components
        catalogue = {1: thoth, 2: oracle}

        asked = {}
        for _ in range(60):
            _, question, _f = ItemQuestionGenerator(thoth, catalogue).question
            asked[question.question] = question.get_answer()
        cost = [a for q, a in asked.items() if q.startswith("How much does **Book")]
        assert cost and cost[0] == "2300", asked
