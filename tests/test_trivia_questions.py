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

from god_types import GodPro, GodRange, GodRole, GodType  # noqa: E402
from item import Item, ItemProperty, ItemAttribute, ItemType  # noqa: E402
from ability import Ability, _item, _itemDescription  # noqa: E402
from smitetrivia import (  # noqa: E402
    AnswerRange,
    GodQuestionGenerator,
    ItemQuestionGenerator,
    TriviaAnswer,
    TriviaQuestion,
)
from HirezAPI import PlayerRole  # noqa: E402


def fake_ability(name="Plague of Locusts", is_passive=False, menu=None):
    return Ability(
        _itemDescription(
            cooldown="10/9.5/9/8.5/8",
            cost="60/65/70/75/80",
            description="Deals damage in a line.",
            menu_items=menu or [_item("Damage:", "60/85/110")],
            rank_items=[_item("Damage:", "60/85/110")],
        ),
        id=1,
        name=name,
        icon_url="http://x/a.png",
        is_passive=is_passive,
    )


def fake_abilities():
    """Two abilities in the shape both games produce: slash-separated ranks."""
    return [fake_ability()]


def fake_stats(*attributes):
    """A stat block in the shape `get_stat_at_level` is asked for."""
    return types.SimpleNamespace(
        values={attribute: object() for attribute in attributes},
        basic_attack=types.SimpleNamespace(scaling=0.2, progression=None),
    )


def smite1_god(**overrides):
    """What the Hi-Rez API gives: a class, Pros, no positions or Aspect."""
    base = dict(
        name="Anubis",
        lore="Anubis weighs the hearts of the dead.",
        pantheon="Egyptian",
        title="God of the Dead",
        pros=[GodPro.HIGH_AREA_DAMAGE],
        role=GodRole.MAGE,
        type=GodType.MAGICAL,
        range=GodRange.RANGED,
        positions=[],
        specs=[],
        aspect=None,
        abilities=fake_abilities(),
        id=1,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


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


def asked_of(god, rounds=200):
    """Every distinct question a god produces, keyed by its text.

    Most of the bank is drawn per call — an ability, a stat — so a single
    `.question` sees one slice of what the god can be asked.
    """
    asked = {}
    for _ in range(rounds):
        _, _, question = questions_for(god)
        asked[question.question] = question
    return asked


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


class TestGodStatQuestions:
    def test_a_god_is_asked_for_a_stat_at_the_level_cap(self):
        god = smite1_god(
            stats=fake_stats(ItemAttribute.HEALTH),
            get_stat_at_level=lambda _stat, _level: 1682.0,
            icon_url="http://x/g.png",
        )
        asked = asked_of(god)
        health = [q for text, q in asked.items() if "Health" in text and "level" in text]
        assert health
        assert health[0].check_guess("1682")
        # The tolerance is stated in the question and honoured by the answer.
        assert health[0].check_guess("1700")
        assert not health[0].check_guess("900")
        assert health[0].image_url_or_bytes == "http://x/g.png"

    def test_a_stat_the_god_does_not_have_is_not_asked_about(self):
        """A manaless god's mana reads as zero, which is an absence rather than
        a value — and an AnswerRange around it would accept a guess of nothing."""
        god = smite1_god(
            stats=fake_stats(ItemAttribute.MANA),
            get_stat_at_level=lambda _stat, _level: 0,
        )
        assert not any(
            "**Mana** does **Anubis** have at level" in text for text in asked_of(god)
        )

    def test_a_god_without_stats_at_all_still_produces_questions(self):
        """Every Smite 2 god the wiki failed a stat curve for, and every fake in
        the rest of this file."""
        for god in (smite1_god(), smite2_god()):
            assert asked_of(god, rounds=40)

    def test_a_god_is_asked_its_damage_type_and_its_reach(self):
        asked = asked_of(smite1_god())
        assert any("magical or physical" in text for text in asked)
        assert any("melee or ranged" in text for text in asked)


class TestAbilityQuestions:
    def test_a_single_valued_ability_stat_is_asked_about(self):
        god = smite1_god(
            abilities=[
                fake_ability(menu=[_item("Radius:", "20"), _item("Damage:", "60/85/110")])
            ]
        )
        asked = asked_of(god)
        radius = [q for text, q in asked.items() if "Radius" in text]
        assert radius
        assert radius[0].check_guess("20")
        # The per-rank list has no one right answer to type.
        assert not any("**Damage** of" in text for text in asked)

    def test_a_unit_on_a_value_is_not_something_a_guesser_must_reproduce(self):
        god = smite1_god(abilities=[fake_ability(menu=[_item("Range:", "55 units")])])
        asked = asked_of(god)
        ranged = [q for text, q in asked.items() if "Range" in text]
        assert ranged
        assert ranged[0].check_guess("55")
        assert ranged[0].check_guess("55 units")

    def test_the_cost_question_names_the_resource_it_can(self):
        """Smite 2 publishes the resource, so the question stops hedging."""
        god = smite1_god(name="Surtr", resource="rage")
        assert any("**Rage**" in text for text in asked_of(god))

    def test_slots_are_asked_only_when_the_order_is_the_games(self):
        """Smite 1's five arrive in slot order with the passive last. Smite 2's
        come off a wiki page, whose order is the page's."""
        smite1 = smite1_god(
            abilities=[fake_ability(f"Ability {i}") for i in range(1, 5)]
            + [fake_ability("Sorrow", is_passive=True)]
        )
        slots = [q for text, q in asked_of(smite1).items() if "Which slot" in text]
        assert slots
        ultimate = [q for q in slots if "Ability 4" in q.question]
        assert ultimate and ultimate[0].check_guess("ultimate")

        wiki_order = smite2_god(
            abilities=[fake_ability("Triple Goddess", is_passive=True)]
            + [fake_ability(f"Ability {i}") for i in range(1, 5)]
        )
        assert not any("Which slot" in text for text in asked_of(wiki_order))


class TestAspectQuestions:
    def aspected_god(self):
        return smite2_god(
            aspect=types.SimpleNamespace(
                name="Aspect of Manipulation",
                description="Aspect of Manipulation steals an enemy's cooldowns.",
                icon_url="http://x/aspect.png",
                changed_abilities={"1": fake_ability("Mystic Flight")},
            )
        )

    def test_the_aspect_is_asked_from_its_description(self):
        asked = asked_of(self.aspected_god())
        described = [q for text, q in asked.items() if "steals an enemy" in text]
        assert described
        assert described[0].get_answer() == "Aspect of Manipulation"
        # The name is blanked out of its own description, as the lore is.
        assert "Aspect of Manipulation steals" not in described[0].question

    def test_the_abilities_an_aspect_changes_are_asked_about(self):
        asked = asked_of(self.aspected_god())
        changed = [q for text, q in asked.items() if "changes" in text]
        assert changed
        assert changed[0].check_guess("Mystic Flight")

    def test_an_aspect_with_neither_is_still_only_asked_by_name(self):
        god = smite2_god(
            aspect=types.SimpleNamespace(
                name="Aspect of Ruin",
                description="",
                icon_url=None,
                changed_abilities={},
            )
        )
        assert any("Which god has the Aspect" in text for text in asked_of(god))


class TestHints:
    def test_a_misspelling_is_told_it_is_a_misspelling(self):
        assert "spelling" in TriviaAnswer(["Rod of Asclepius"]).hint_for("Rod of Asclepus")

    def test_half_an_answer_is_told_it_is_half_an_answer(self):
        assert "part of it" in TriviaAnswer(["Book of Thoth"]).hint_for("Book")

    def test_a_shared_word_is_told_it_is_close(self):
        assert "right track" in TriviaAnswer(["Book of Thoth"]).hint_for("Thoth Staff")

    def test_a_wrong_answer_is_told_nothing(self):
        """A hint for everything is a hint for nothing, and "of" is in half the
        item list."""
        assert TriviaAnswer(["Book of Thoth"]).hint_for("Soul Reaver") is None
        assert TriviaAnswer(["Book of Thoth"]).hint_for("The Sledge") is None

    def test_a_short_name_is_not_declared_a_near_miss_of_every_other(self):
        assert TriviaAnswer(["Ymir"]).hint_for("Hera") is None

    def test_a_number_is_left_to_the_higher_lower_hint(self):
        assert TriviaAnswer(["40"]).hint_for("30") is None

    def test_a_range_answer_has_no_free_text_hint(self):
        answer = TriviaAnswer(answer_range=AnswerRange(0, 10, 5))
        assert answer.hint_for("nine") is None


class TestNumericAnswers:
    def test_a_percent_answer_accepts_the_number_either_way(self):
        """Stored as "20%", the guess had its sign stripped and the answer did
        not, so neither "20" nor "20%" was ever accepted."""
        answer = TriviaAnswer(["20%"])
        assert answer.check_guess("20")
        assert answer.check_guess("20%")

    def test_a_number_offered_beside_its_unit_is_still_a_number(self):
        """`answer_is_number` insisted on exactly one all-digit answer, so a
        question answered by "55" or "55 units" got no higher/lower hint."""
        question = TriviaQuestion("How far?", TriviaAnswer(["55 units", "55"]))
        assert question.numeric_answer() == 55


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


def asked_of_item(item, catalogue, rounds=200):
    """Every distinct question an item produces, keyed by its text."""
    asked = {}
    for _ in range(rounds):
        _, question, _f = ItemQuestionGenerator(item, catalogue).question
        asked[question.question] = question
    return asked


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

    def test_the_stat_questions_are_actually_asked(self):
        """They were generated into a copy of the bank and then drawn from the
        bank it was copied from, so no round has ever asked one."""
        item = make_item(ItemType.ITEM)
        asked = asked_of_item(item, {item.id: item})
        assert any("Intelligence" in text for text in asked)

    def test_a_percent_stat_can_be_answered(self):
        item = make_item(
            ItemType.ITEM,
            item_properties=[
                ItemProperty(ItemAttribute.COOLDOWN_RATE, percent_value=0.1)
            ],
        )
        asked = asked_of_item(item, {item.id: item})
        percent = [q for text, q in asked.items() if "What percent" in text]
        assert percent
        assert percent[0].check_guess("10")
        assert percent[0].check_guess("10%")

    def test_an_item_is_asked_its_tier_over_its_own_icon(self):
        item = make_item(ItemType.ITEM, tier=3)
        asked = asked_of_item(item, {item.id: item})
        tier = [q for text, q in asked.items() if "What tier" in text]
        assert tier
        assert tier[0].check_guess("3")
        assert tier[0].image_url_or_bytes == item.icon_url

    def test_a_recipe_is_asked_in_both_directions(self):
        thoth = make_item(ItemType.ITEM, components=[2, 3], id=1)
        tome = make_item(ItemType.ITEM, name="Mana Tome", id=2, components=[])
        staff = make_item(ItemType.ITEM, name="Oracle Staff", id=3, components=[])
        catalogue = {1: thoth, 2: tome, 3: staff}

        built_from = asked_of_item(thoth, catalogue)
        components = [q for text, q in built_from.items() if "built out of" in text]
        assert components
        assert components[0].check_guess("Mana Tome")
        assert components[0].check_guess("Oracle Staff")

        # Nothing stores the upwards direction, so it is a scan of the rest.
        builds_into = asked_of_item(tome, catalogue)
        parents = [q for text, q in builds_into.items() if "builds into" in text]
        assert parents
        assert parents[0].check_guess("Book of Thoth")

    def test_a_leaf_item_is_not_asked_what_it_builds_out_of(self):
        item = make_item(ItemType.ITEM, components=[], parent_item_id=None)
        asked = asked_of_item(item, {item.id: item})
        assert not any("built out of" in text for text in asked)
        assert not any("builds into" in text for text in asked)

    def test_the_catalogue_is_asked_for_its_superlatives(self):
        cheap = make_item(ItemType.ITEM, name="Mana Tome", id=2, total_cost=700,
                          item_properties=[
                              ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=20)
                          ])
        dear = make_item(ItemType.ITEM, name="Soul Reaver", id=3, total_cost=3700,
                         item_properties=[
                             ItemProperty(ItemAttribute.INTELLIGENCE, flat_value=120)
                         ])
        catalogue = {2: cheap, 3: dear}
        asked = asked_of_item(cheap, catalogue)

        most = [q for text, q in asked.items() if "most **Intelligence**" in text]
        assert most and most[0].check_guess("Soul Reaver")
        expensive = [q for text, q in asked.items() if "most expensive" in text]
        assert expensive and expensive[0].check_guess("Soul Reaver")

    def test_an_inactive_item_does_not_win_a_superlative(self):
        """The generator is handed every item the provider knows, not the
        active ones the round draws its subject from."""
        live = make_item(ItemType.ITEM, name="Soul Reaver", id=2, total_cost=3700,
                         active=True)
        cheap = make_item(ItemType.ITEM, name="Mana Tome", id=4, total_cost=700,
                          active=True)
        removed = make_item(ItemType.ITEM, name="Hide of the Nemean Lion", id=3,
                            total_cost=9000, active=False)
        asked = asked_of_item(live, {2: live, 3: removed, 4: cheap})
        expensive = [q for text, q in asked.items() if "most expensive" in text]
        assert expensive and expensive[0].get_answer() == "Soul Reaver"

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
