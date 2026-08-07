import asyncio
import io
import json
import math
import os
import random
import re
import time
import uuid
from enum import Enum
from json.decoder import JSONDecodeError
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import discord
import edit_distance
from discord.ext import commands
from unidecode import unidecode

import art_cache
import paths
from game import Game
from providers import Providers
from slash_guilds import SLASH_COMMAND_GUILD_IDS
from player import Player, PlayerId
from queue_stats import QueueStats
from SmiteProvider import SmiteProvider
from god import God, GodId
from item import Item, ItemAttribute, ItemType
from skin import Skin
from HirezAPI import PlayerRole, QueueId
from build_optimizer import compute_item_price
from item_tree_builder import ItemTreeBuilder


class StoppedError(Exception):
    pass


class AnswerRange:
    min_value: float
    max_value: float
    correct_value: float
    is_percent: bool

    def __init__(
        self,
        min_value: int,
        max_value: int,
        correct_value: int,
        is_percent: bool = False,
    ):
        self.min_value = min_value
        self.max_value = max_value
        self.correct_value = correct_value
        self.is_percent = is_percent

        if max_value <= min_value:
            raise ValueError("min_value must be lower than max_value")
        if correct_value < min_value or correct_value > max_value:
            raise ValueError("correct_value must be bound by min_value and max_value")

    def check_guess(self, guess: str) -> bool:
        try:
            guess_number = float(guess.replace("%", ""))

            return self.min_value <= guess_number <= self.max_value
        except ValueError:
            return False

    def get_answer(self) -> str:
        return f"{self.correct_value}{'%' if self.is_percent else ''}"


# Words too common to count as having got part of a name right. "of" and "the"
# are in almost every item in the game, so matching one says nothing.
_HINT_STOPWORDS = frozenset({"the", "of", "a", "an", "and"})


def _within_percent(value: int, percent: int = 10) -> AnswerRange:
    """A range of `percent`% either side of a count.

    Never narrower than one either way: `AnswerRange` refuses a range it cannot
    bound, and ten percent of a player's four penta kills rounds to nothing.
    """
    tolerance = max(1, math.ceil(abs(value) * percent / 100))
    return AnswerRange(max(value - tolerance, 0), value + tolerance, value)


def _percent_within_five(fraction: float) -> AnswerRange:
    """A 0-1 rate as a percentage, guessable within five points either side."""
    value = int(fraction * 100)
    return AnswerRange(max(value - 5, 0), min(value + 5, 100), value, is_percent=True)


_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(value: int) -> str:
    """1 -> "1st", so that "3rd" is accepted wherever a bare "3" is."""
    if 10 <= value % 100 <= 20:
        return f"{value}th"
    return f"{value}{_ORDINAL_SUFFIXES.get(value % 10, 'th')}"


def _hint_words(text: str) -> set:
    return {
        word
        for word in re.split(r"[^a-z0-9]+", text)
        if len(word) > 2 and word not in _HINT_STOPWORDS
    }


class TriviaAnswer:
    valid_answers: Optional[List[str]]
    answer_range: Optional[AnswerRange]

    def __init__(
        self,
        answers: Optional[List[str]] = None,
        answer_range: Optional[AnswerRange] = None,
    ):
        self.valid_answers = answers
        self.answer_range = answer_range

        if self.valid_answers is not None and self.answer_range is not None:
            raise ValueError("Cannot specify both valid_answers and answer_range")

    def check_guess(self, guess: str) -> bool:
        if self.valid_answers is not None:
            for answer in self.valid_answers:
                # The percent sign comes off the answer as well as the guess.
                # It only ever came off the guess, so an answer stored as "20%"
                # — which is how every percent-valued item stat is stored — had
                # no accepted spelling at all: "20" missed on the sign and "20%"
                # missed because the guess had been stripped and the answer had
                # not. Being all digits, it then skipped the fuzzy match too.
                answer = (
                    unidecode(str(answer)).lower().replace("-", " ").replace("%", "")
                )
                correct = answer == unidecode(guess).lower().replace("-", " ").replace(
                    "%", ""
                )
                if not correct and not answer.isdigit():
                    if answer.startswith("the") and not guess.lower().startswith("the"):
                        answer = answer.replace("the ", "")
                    correct = (
                        edit_distance.SequenceMatcher(
                            a=answer, b=guess.lower()
                        ).distance()
                        <= 2
                    )
                if correct:
                    return True
        if self.answer_range is not None:
            return self.answer_range.check_guess(guess)

        return False

    def hint_for(self, guess: str) -> Optional[str]:
        """A nudge for a wrong free-text guess that is nearly right.

        Numbers get the higher/lower hint; this is the same courtesy for names,
        where a near miss is nearly always a spelling slip or one half of a
        two-word item and the guesser otherwise gets nothing back to work with.
        It says which *kind* of near miss it was and never which part is wrong,
        so it narrows the search without handing the answer over.
        """
        if self.valid_answers is None:
            return None

        guess = unidecode(guess).lower().replace("-", " ").strip()
        # A guess long enough to be a sentence is not a near miss at anything.
        if not guess or len(guess) > 60:
            return None

        answers = [
            unidecode(str(value)).lower().replace("-", " ").replace("%", "").strip()
            for value in self.valid_answers
        ]
        answers = [answer for answer in answers if answer and not answer.isdigit()]
        if not answers:
            return None

        # Two edits already counts as correct, so the band that earns a spelling
        # nudge starts above it and widens with the length of the name. Short
        # answers get no band at all, which is right — "Ymir" and "Hera" are
        # three edits from most of the roster.
        for answer in answers:
            if edit_distance.SequenceMatcher(a=answer, b=guess).distance() <= max(
                2, len(answer) // 3
            ):
                return "that's very nearly it, check your spelling. 🔤"

        guess_words = _hint_words(guess)
        if not guess_words:
            return None
        for answer in answers:
            if guess_words < _hint_words(answer):
                return "you have part of it, but not all of it. ➕"
        for answer in answers:
            if guess_words & _hint_words(answer):
                return "you're on the right track. 🔍"
        return None

    def get_answer(self) -> str:
        if self.valid_answers is not None and len(self.valid_answers) > 1:
            return f'either {", ".join(self.valid_answers[:-1])}{"," if len(self.valid_answers) > 2 else ""} or {self.valid_answers[-1]}'
        elif self.answer_range is not None:
            return self.answer_range.get_answer()
        return f"{self.valid_answers[0]}"


class TriviaQuestion:
    answer: TriviaAnswer
    id: uuid
    question: str
    image_url_or_bytes: Optional[str | io.BytesIO]
    choices: Optional[List[str]]

    def __init__(
        self,
        question: str,
        answer: str | TriviaAnswer,
        image_url_or_bytes: Optional[str | io.BytesIO] = None,
        choices: Optional[List[str]] = None,
    ):
        self.question = question
        self.answer = (
            answer if isinstance(answer, TriviaAnswer) else TriviaAnswer([answer])
        )
        self.id = uuid.uuid4()
        self.image_url_or_bytes = image_url_or_bytes
        # Set means the round shows buttons and takes no typed answer. Left
        # None means the question is typed, which is still most of them.
        self.choices = list(choices) if choices else None

    def check_guess(self, guess: str) -> bool:
        return self.answer.check_guess(guess)

    def get_answer(self) -> str:
        return self.answer.get_answer()

    def hint_for(self, guess: str) -> Optional[str]:
        return self.answer.hint_for(guess)

    def numeric_answer(self) -> Optional[float]:
        """The number a guess is measured against for the higher/lower hint.

        Replaces a check that recognised a numeric question only when its one
        and only answer was all digits, which would have gone quiet the moment
        a number was offered alongside the same number with its unit — "55" and
        "55 units" — as the ability stat questions do.
        """
        if self.answer.answer_range is not None:
            return self.answer.answer_range.correct_value
        for value in self.answer.valid_answers or []:
            text = unidecode(str(value)).replace("%", "").strip()
            if text.isdigit():
                return float(text)
        return None


def _with_choices(
    question: TriviaQuestion,
    pool: Iterable[str],
    count: int = 4,
    minimum: int = 3,
) -> Optional[TriviaQuestion]:
    """The question as multiple choice, or None if the pool was too thin.

    Returns None rather than raising because the two kinds of caller want
    different things from a pool that cannot fill the buttons. A question that
    is meaningless without options — "which of these is *not* on this item?" —
    drops the None and is never asked. One that reads fine either way passes it
    through `or question` and stays typed.

    A candidate the question would *accept* is not a distractor, and this is
    the only place that can tell: several questions hold more than one right
    answer, and the fuzzy match accepts anything within two edits of one. So
    "Mana Tomb" is refused as an option beside "Mana Tome", and so is the
    second correct answer to "name an item that builds into Circlet".
    """
    answers = question.answer.valid_answers
    if not answers:
        return None

    correct = random.choice(answers)
    seen = {unidecode(str(correct)).lower().strip()}
    candidates: List[str] = []
    for value in pool:
        text = str(value).strip()
        key = unidecode(text).lower()
        if not text or key in seen or question.check_guess(text):
            continue
        seen.add(key)
        candidates.append(text)

    if len(candidates) < minimum - 1:
        return None

    choices = random.sample(candidates, min(count - 1, len(candidates)))
    choices.append(str(correct))
    random.shuffle(choices)

    # Narrowed to the option actually on screen, so the reveal names the button
    # that was there rather than "either X or Y" of which only one was offered.
    question.answer = TriviaAnswer([str(correct)])
    question.choices = choices
    return question


# Enough of Discord's list to keep the extension honest; anything else it will
# not treat as an image at all.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


async def _attach_image(
    embed: discord.Embed, question: TriviaQuestion
) -> Optional[discord.File]:
    """Send the picture with the message rather than pointing Discord at it.

    `set_image(url=...)` posts the message the moment it is built and leaves
    every client to go and fetch the art itself, so a multiple-choice question
    arrived as four buttons above an empty frame that filled in a beat later.
    Uploading the bytes makes the image part of the message — which is what the
    build-tree question always did, since the tree it draws exists nowhere to
    link to.

    Art is fetched through the same disk cache the rest of the bot uses, under
    a directory of its own: trivia asks for ability icons and Discord avatars,
    which no other command caches, and keying them by kind would mean the
    question knowing what kind of thing its picture is.

    Falls back to the URL whenever the fetch does not produce an image, which
    is exactly the behaviour every question had before.
    """
    source = question.image_url_or_bytes
    if not isinstance(source, str) or not source:
        return None

    name = art_cache.cache_key(source)
    extension = os.path.splitext(name)[1].lower()
    if extension not in _IMAGE_EXTENSIONS:
        extension = ".png"
    filename = f"question{extension}"

    data = await art_cache.fetch(source, "trivia", name)
    if not art_cache.looks_like_image(data.getvalue()):
        embed.set_image(url=source)
        return None

    embed.set_image(url=f"attachment://{filename}")
    return discord.File(data, filename=filename)


def _offer_choices(
    question: TriviaQuestion,
    pool: Iterable[str],
    count: int = 4,
    minimum: int = 3,
) -> TriviaQuestion:
    """Multiple choice where the pool allows it, typed where it does not.

    For the questions that read fine either way — an icon, a passive, a piece
    of lore — where buttons only make an unfair question fair.
    """
    return _with_choices(question, pool, count, minimum) or question


class _ChoiceButton(discord.ui.Button):
    """One option. The view it belongs to owns the answering."""

    choice: str

    def __init__(self, choice: str, row: int):
        # Discord truncates past 80 characters; keep the full text for matching.
        super().__init__(
            label=choice[:80], style=discord.ButtonStyle.secondary, row=row
        )
        self.choice = choice

    async def callback(self, interaction: discord.Interaction):
        await self.view.press(interaction, self.choice)


class _ChoiceView(discord.ui.View):
    """The buttons for a multiple-choice question, and its one winner.

    A wrong press is answered privately. That is the whole reason this is
    buttons rather than a Discord poll: a poll's tally is public as it fills
    in, so the first person to vote hands the answer to everybody else.
    """

    # One press each. With four options in front of you a second guess is a
    # coin flip, which is not a trivia question.
    GUESSES_PER_USER = 1

    def __init__(self, question: TriviaQuestion, timeout: float):
        super().__init__(timeout=timeout, disable_on_timeout=False)
        self.__question = question
        self.__pressed: Dict[int, int] = {}
        self.winner: asyncio.Future = asyncio.get_running_loop().create_future()
        choices = question.choices or []
        # Two to a row once there are more than three, so long item names are
        # not squeezed into slivers.
        per_row = len(choices) if len(choices) <= 3 else 2
        for index, choice in enumerate(choices):
            self.add_item(_ChoiceButton(choice, row=index // per_row))

    async def press(self, interaction: discord.Interaction, choice: str):
        if self.winner.done():
            await interaction.response.send_message(
                "Someone already got this one! <:mleh:472905075208093717>",
                ephemeral=True,
            )
            return

        pressed = self.__pressed.get(interaction.user.id, 0)
        if pressed >= self.GUESSES_PER_USER:
            await interaction.response.send_message(
                "You've already had your guess on this one. "
                "<:noshot:782396496104128573>",
                ephemeral=True,
            )
            return
        self.__pressed[interaction.user.id] = pressed + 1

        if not self.__question.check_guess(choice):
            await interaction.response.send_message(
                f"Not **{choice}**. ❌", ephemeral=True
            )
            return

        self.winner.set_result(interaction.user)
        self.disable_all_items()
        await interaction.response.edit_message(view=self)


class QuestionGenerator:
    @property
    def question(self) -> Tuple[discord.Embed, TriviaQuestion, discord.File]:
        raise NotImplementedError


class ItemQuestionGenerator(QuestionGenerator):
    __all_items: Dict[int, Item]
    __item: Item
    __question_bank: Dict[ItemType, List[TriviaQuestion]]

    def __init__(self, item: Item, items: Dict[int, Item]):
        self.__all_items = items
        self.__item = item
        self.__init_question_bank()

    @property
    def question(self) -> Tuple[discord.Embed, TriviaQuestion, discord.File]:
        question_bank = self.__question_bank[self.__item.type].copy()
        if self.__item.type == ItemType.ITEM:
            if any(self.__item.item_properties):
                question_bank.extend(self.__generate_properties_questions(self.__item))
            question_bank.extend(self.__generate_catalogue_questions())
        # Drawn from the extended copy, not the bank it was copied from. The
        # stat questions have been generated and thrown away since the day they
        # were written: "How much Intelligence does Book of Thoth give?" has
        # never once been asked.
        question = random.choice(question_bank)
        embed = discord.Embed(description=question.question)
        if question.image_url_or_bytes is not None:
            if isinstance(question.image_url_or_bytes, io.BytesIO):
                file = discord.File(question.image_url_or_bytes, filename="tree.png")
                embed.set_image(url="attachment://tree.png")
                return (embed, question, file)
            embed.set_image(url=question.image_url_or_bytes)
        return (embed, question, None)

    def __compute_price(self, item: Item) -> int:
        # The shared implementation, which prefers a stated total over walking
        # the chain. Smite 2's recipes fork — Book of Thoth is built from two
        # items at once — so a walk down one branch reports 1,650 for an item
        # that costs 2,300, and the trivia answer would be confidently wrong.
        return compute_item_price(item, self.__all_items)

    async def generate_tree_question(self):
        tree_builder = ItemTreeBuilder(self.__all_items)
        tree_image = await tree_builder.generate_build_tree(
            self.__item, trivia_mode=True
        )

        self.__question_bank[ItemType.ITEM].append(
            _offer_choices(
                TriviaQuestion(
                    "What item has been replaced by a question mark in this tree?",
                    tree_builder.trivia_item.name,
                    tree_image,
                ),
                self.__names(ItemType.ITEM),
            )
        )

    def __init_question_bank(self):
        item = self.__item
        consumables = self.__names(ItemType.CONSUMABLE)
        relics = self.__names(ItemType.RELIC)
        self.__question_bank = {
            ItemType.CONSUMABLE: [
                q
                for q in [
                    TriviaQuestion(
                        f"How much does "
                        f'{"an" if item.name[0].lower() in "aeiou" else "a"} **{item.name}** cost?',
                        f"{item.price}",
                    ),
                    _offer_choices(
                        TriviaQuestion(
                            f"Name the consumable with this description: \n\n`{item.passive}`",
                            item.name,
                        ),
                        consumables,
                    )
                    if (item.passive or "").strip()
                    else None,
                    _offer_choices(
                        TriviaQuestion(
                            "What consumable is this?", item.name, item.icon_url
                        ),
                        consumables,
                    ),
                ]
                if q is not None
            ],
            ItemType.RELIC: [
                q
                for q in [
                    _offer_choices(
                        TriviaQuestion(
                            f"Name the relic with this description: \n\n`{item.passive}`",
                            item.name,
                        ),
                        relics,
                    )
                    if (item.passive or "").strip()
                    else None,
                    _offer_choices(
                        TriviaQuestion("What relic is this?", item.name, item.icon_url),
                        relics,
                    ),
                ]
                if q is not None
            ],
            ItemType.ITEM: list(
                filter(
                    lambda q: q is not None,
                    [
                        TriviaQuestion(
                            f"How much does **{item.name}** cost?",
                            f"{self.__compute_price(item)}",
                        ),
                        _offer_choices(
                            TriviaQuestion(
                                f'Name the item with this {"passive" if item.passive is not None and item.passive.strip() != "" else "aura"}'
                                f':\n\n`{item.passive if item.passive is not None and item.passive.strip() != "" else item.aura.strip()}`',
                                item.name,
                            ),
                            self.__names(ItemType.ITEM),
                        )
                        if (item.passive is not None and item.passive.strip() != "")
                        or (item.aura is not None and item.aura.strip() != "")
                        else None,
                        TriviaQuestion(
                            f"How much does it cost to upgrade **{self.__all_items[item.parent_item_id].name}** into **{item.name}**?",
                            f"{item.price}",
                        )
                        if item.parent_item_id is not None and item.price > 0
                        else None,
                        _offer_choices(
                            TriviaQuestion(
                                "What item is this?", item.name, item.icon_url
                            ),
                            # Same tier first: an icon question between four
                            # tier 3s is a real question, and between a tier 3
                            # and three starters it is not.
                            self.__names(ItemType.ITEM, tier=getattr(item, "tier", None))
                            or self.__names(ItemType.ITEM),
                        ),
                        *self.__generate_choice_questions(item),
                        TriviaQuestion(
                            f"What tier is **{item.name}**?",
                            f"{item.tier}",
                            item.icon_url,
                        )
                        if getattr(item, "tier", 0)
                        else None,
                        self.__restricted_roles_question(item),
                        self.__components_question(item),
                        self.__builds_into_question(item),
                    ],
                )
            ),
        }

    def __active(self, item_type: ItemType) -> List[Item]:
        return [
            other
            for other in self.__all_items.values()
            if getattr(other, "active", True) and other.type == item_type
        ]

    def __names(self, item_type: ItemType, tier: int = None) -> List[str]:
        """Names to draw distractors from, optionally narrowed to one tier."""
        return [
            other.name
            for other in self.__active(item_type)
            if other.id != self.__item.id
            and (tier is None or getattr(other, "tier", None) == tier)
        ]

    def __generate_choice_questions(self, item: Item) -> List[TriviaQuestion]:
        """The questions that only exist because there are buttons.

        None of these can be typed. "Which of these is *not* on this item?" has
        no answer without the other three in front of you, and "which costs
        more?" is a coin flip nobody would type a name into.
        """
        return list(
            filter(
                lambda q: q is not None,
                [
                    self.__odd_stat_out(item),
                    self.__comparison(item),
                    self.__real_name_question(item),
                ],
            )
        )

    def __odd_stat_out(self, item: Item) -> Optional[TriviaQuestion]:
        mine = {prop.attribute for prop in getattr(item, "item_properties", None) or []}
        if len(mine) < 2:
            return None
        elsewhere = {
            prop.attribute
            for other in self.__active(ItemType.ITEM)
            for prop in getattr(other, "item_properties", None) or []
        } - mine
        if not elsewhere:
            return None
        return _with_choices(
            TriviaQuestion(
                f"Which of these does **{item.name}** **not** give?",
                random.choice(sorted(elsewhere, key=lambda a: a.value)).display_name,
                item.icon_url,
            ),
            [attribute.display_name for attribute in mine],
            count=min(4, len(mine) + 1),
            minimum=3,
        )

    def __comparison(self, item: Item) -> Optional[TriviaQuestion]:
        """This item against one other, on price or on a shared stat."""
        others = [
            other for other in self.__active(ItemType.ITEM) if other.id != item.id
        ]
        if not others:
            return None
        other = random.choice(others)

        shared = [
            (prop, match)
            for prop in getattr(item, "item_properties", None) or []
            for match in getattr(other, "item_properties", None) or []
            if prop.attribute == match.attribute
            and prop.flat_value
            and match.flat_value
            and prop.flat_value != match.flat_value
        ]
        if shared and random.choice((True, False)):
            prop, match = random.choice(shared)
            winner = item if prop.flat_value > match.flat_value else other
            return _with_choices(
                TriviaQuestion(
                    f"Which gives more **{prop.attribute.display_name}**, "
                    f"**{item.name}** or **{other.name}**?",
                    winner.name,
                ),
                [(other if winner is item else item).name],
                count=2,
                minimum=2,
            )

        mine, theirs = self.__compute_price(item), self.__compute_price(other)
        if mine == theirs:
            return None
        winner = item if mine > theirs else other
        return _with_choices(
            TriviaQuestion(
                f"Which costs more, **{item.name}** or **{other.name}**?",
                winner.name,
            ),
            [(other if winner is item else item).name],
            count=2,
            minimum=2,
        )

    # Real names are mostly "<thing> of <somebody>", so swapping the halves
    # produces something that sounds exactly like an item and is not one.
    __NAME_JOINER = " of "

    def __real_name_question(self, item: Item) -> Optional[TriviaQuestion]:
        names = [other.name for other in self.__active(ItemType.ITEM)]
        halves = [name.split(self.__NAME_JOINER, 1) for name in names]
        heads = [half[0] for half in halves if len(half) == 2]
        tails = [half[1] for half in halves if len(half) == 2]
        if len(heads) < 4 or self.__NAME_JOINER not in item.name:
            return None

        real = set(names)
        invented = {
            f"{head}{self.__NAME_JOINER}{tail}"
            for head in random.sample(heads, min(8, len(heads)))
            for tail in random.sample(tails, min(8, len(tails)))
        } - real
        if len(invented) < 3:
            return None

        return _with_choices(
            TriviaQuestion("Which of these is a real item?", item.name),
            sorted(invented),
        )

    def __components_of(self, item: Item) -> List[Item]:
        """What the item is built out of, in either game.

        Smite 2 states every component; Smite 1 states one, because a recipe
        there is a chain rather than a fork. Taking the chain link as a
        one-element list is what lets the question be asked of both.
        """
        component_ids = list(getattr(item, "components", None) or [])
        if not component_ids and item.parent_item_id is not None:
            component_ids = [item.parent_item_id]
        return [
            self.__all_items[component_id]
            for component_id in component_ids
            if component_id in self.__all_items
        ]

    def __builds_into(self, item: Item) -> List[Item]:
        """The items this one is a component of — the recipe read upwards.

        Nothing stores this direction, so it is a scan. Both games point only
        downwards, and a Smite 2 component like Circlet feeds several unrelated
        items, which is what makes the question worth asking.
        """
        return [
            other
            for other in self.__all_items.values()
            if other.id != item.id
            and getattr(other, "active", True)
            and item.id in self.__component_ids(other)
        ]

    @staticmethod
    def __component_ids(item: Item) -> List[int]:
        component_ids = list(getattr(item, "components", None) or [])
        if item.parent_item_id is not None:
            component_ids.append(item.parent_item_id)
        return component_ids

    @staticmethod
    def __restricted_roles_question(item: Item) -> Optional[TriviaQuestion]:
        roles = getattr(item, "restricted_roles", None) or []
        if not any(roles):
            return None
        return TriviaQuestion(
            f'Name {"a" if len(roles) > 1 else "the"} class that **cannot** '
            f"build **{item.name}**.",
            TriviaAnswer([f"{role.name.title()}" for role in roles]),
            item.icon_url,
        )

    def __components_question(self, item: Item) -> Optional[TriviaQuestion]:
        components = self.__components_of(item)
        if not components:
            return None
        return TriviaQuestion(
            f'Name {"an" if len(components) > 1 else "the"} item that '
            f"**{item.name}** is built out of.",
            TriviaAnswer([component.name for component in components]),
            item.icon_url,
        )

    def __builds_into_question(self, item: Item) -> Optional[TriviaQuestion]:
        builds_into = self.__builds_into(item)
        if not builds_into:
            return None
        return TriviaQuestion(
            f'Name {"an" if len(builds_into) > 1 else "the"} item that '
            f"**{item.name}** builds into.",
            TriviaAnswer([parent.name for parent in builds_into]),
            item.icon_url,
        )

    # A superlative over a set of tied items is a fine question; over a set of
    # thirty it is a giveaway with an unreadable answer.
    __MAX_ANSWERS = 5

    def __generate_catalogue_questions(self) -> List[TriviaQuestion]:
        """Questions about the item list rather than about one item.

        Deliberately without an image: every answer here is an item name, so
        showing any one of the candidates' icons would be showing the answer.
        """
        items = [
            item
            for item in self.__all_items.values()
            if getattr(item, "active", True) and item.type == ItemType.ITEM
        ]
        if len(items) < 2:
            return []

        questions: List[TriviaQuestion] = []

        by_attribute: Dict[ItemAttribute, List[Tuple[float, Item]]] = {}
        for item in items:
            for prop in getattr(item, "item_properties", None) or []:
                if prop.flat_value:
                    by_attribute.setdefault(prop.attribute, []).append(
                        (prop.flat_value, item)
                    )
        leaders: Dict[ItemAttribute, List[str]] = {}
        for attribute, values in by_attribute.items():
            if len(values) < 2:
                continue
            most = max(value for value, _ in values)
            names = [item.name for value, item in values if value == most]
            if len(names) <= self.__MAX_ANSWERS:
                leaders[attribute] = names
        if leaders:
            attribute = random.choice(list(leaders))
            questions.append(
                TriviaQuestion(
                    f"Which item gives the most **{attribute.display_name}**?",
                    TriviaAnswer(leaders[attribute]),
                )
            )

        priced = [(self.__compute_price(item), item) for item in items]
        dearest = max(price for price, _ in priced)
        most_expensive = [item.name for price, item in priced if price == dearest]
        if len(most_expensive) <= self.__MAX_ANSWERS:
            questions.append(
                TriviaQuestion(
                    "What is the most expensive item in the game?",
                    TriviaAnswer(most_expensive),
                )
            )

        by_passive: Dict[object, List[Item]] = {}
        for item in items:
            for attribute in getattr(item, "passive_properties", None) or set():
                by_passive.setdefault(attribute, []).append(item)
        rare = [
            attribute
            for attribute, matched in by_passive.items()
            if len(matched) <= self.__MAX_ANSWERS
        ]
        if rare:
            attribute = random.choice(rare)
            matched = by_passive[attribute]
            questions.append(
                TriviaQuestion(
                    f'Name {"an" if len(matched) > 1 else "the"} item with '
                    f'{"a" if attribute.name[0] not in "AEIOU" else "an"} '
                    f'**{attribute.name.replace("_", " ").title()}** passive.',
                    TriviaAnswer([item.name for item in matched]),
                )
            )

        return questions

    @staticmethod
    def __generate_properties_questions(item: Item) -> List[TriviaQuestion]:
        prop = random.choice(item.item_properties)
        value: str = None
        if prop.flat_value is not None:
            value = f"{prop.flat_value}"
        else:
            value = f"{int(prop.percent_value * 100)}%"

        matched_values = list(
            filter(
                lambda ip: ip.flat_value == prop.flat_value
                or ip.percent_value == prop.percent_value,
                item.item_properties,
            )
        )

        return [
            TriviaQuestion(
                f'{"How much" if prop.flat_value is not None else "What percent"} '
                f"**{prop.attribute.display_name}** does **{item.name}** provide?",
                value,
            ),
            TriviaQuestion(
                f'{"How much" if prop.flat_value is not None else "What percent"} '
                f"**{prop.attribute.display_name}** does this item provide?",
                value,
                item.icon_url,
            ),
            TriviaQuestion(
                f'Name {"the" if len(matched_values) == 1 else "a"} stat on **{item.name}** which provides **{value}**?',
                TriviaAnswer([p.attribute.display_name for p in matched_values]),
            ),
        ]


class GodQuestionGenerator(QuestionGenerator):
    __god: God
    __question_bank: List[TriviaQuestion]
    __provider: SmiteProvider

    def __init__(self, god: God, provider: SmiteProvider):
        self.__god = god
        self.__provider = provider
        self.__init_question_bank()

    @property
    def question(self) -> Tuple[discord.Embed, TriviaQuestion, discord.File]:
        question_bank = self.__question_bank.copy()
        question_bank.extend(self.__generate_abilities_questions(self.__god))
        question_bank.extend(self.__generate_stat_questions(self.__god))
        question = random.choice(question_bank)
        embed = discord.Embed(description=question.question)
        if question.image_url_or_bytes is not None:
            embed.set_image(url=question.image_url_or_bytes)
        return (embed, question, None)

    # The level a god is asked about. Both games cap there, so it is the one
    # level every stat curve is guaranteed to reach.
    __STAT_LEVEL = 20

    # Stats worth asking a number for. Attack speed is left out on purpose: it
    # is a fraction either side of 1.0, so a plus-or-minus range around it
    # rounds to nothing a guesser could aim at.
    __ASKABLE_STATS = (
        ItemAttribute.HEALTH,
        ItemAttribute.MANA,
        ItemAttribute.PHYSICAL_PROTECTION,
        ItemAttribute.MAGICAL_PROTECTION,
        ItemAttribute.HP5,
        ItemAttribute.MP5,
        ItemAttribute.MOVEMENT_SPEED,
        ItemAttribute.STRENGTH,
        ItemAttribute.INTELLIGENCE,
        ItemAttribute.BASIC_ATTACK_POWER,
    )

    @classmethod
    def __generate_stat_questions(cls, god: God) -> List[TriviaQuestion]:
        """One randomly chosen base stat, and what the basic attack does with it.

        Generated per question rather than banked, because banking one entry
        per stat would leave the gods category asking about little else.

        `get_stat_at_level` is asked for the value rather than the curve being
        read directly: it is what knows that a manaless god's mana pool is
        really extra health, and that Smite 1 stops growing movement speed at
        level eight.
        """
        stats = getattr(god, "stats", None)
        values = getattr(stats, "values", None) or {}
        if not hasattr(god, "get_stat_at_level"):
            return []

        questions: List[TriviaQuestion] = []
        icon = getattr(god, "icon_url", None)

        askable = [
            (stat, god.get_stat_at_level(stat, cls.__STAT_LEVEL))
            for stat in cls.__ASKABLE_STATS
            if stat in values
        ]
        # A zero is a stat the god does not have — a manaless god's mana, say —
        # rather than a stat whose value is zero.
        askable = [(stat, value) for stat, value in askable if value > 0]
        if askable:
            stat, value = random.choice(askable)
            tolerance = max(5, int(value * 0.1))
            questions.append(
                TriviaQuestion(
                    f"How much **{stat.display_name}** does **{god.name}** have "
                    f"at level {cls.__STAT_LEVEL} (+/- {tolerance})?",
                    TriviaAnswer(
                        answer_range=AnswerRange(
                            max(int(value) - tolerance, 0),
                            int(value) + tolerance,
                            int(value),
                        )
                    ),
                    icon,
                )
            )

        basic_attack = getattr(stats, "basic_attack", None)
        # Smite 2 publishes no basic attack numbers at all — the wiki gives the
        # attack its own ability block instead — so the object exists with every
        # field zeroed and these two questions simply do not arise there.
        scaling = getattr(basic_attack, "scaling", 0) or 0
        if scaling > 0:
            questions.append(
                TriviaQuestion(
                    f"What percent of your power does **{god.name}**'s basic "
                    f"attack scale with?",
                    f"{int(scaling * 100)}%",
                    icon,
                )
            )

        progression = getattr(basic_attack, "progression", None)
        hits = list(getattr(progression, "damage", None) or [])
        if len(hits) > 1:
            questions.append(
                TriviaQuestion(
                    f"How many hits are in **{god.name}**'s basic attack "
                    f"progression?",
                    f"{len(hits)}",
                    icon,
                )
            )
            is_aoe = list(getattr(progression, "is_aoe", None) or [])
            aoe_hits = [
                index + 1
                for index, aoe in enumerate(is_aoe[: len(hits)])
                if aoe
            ]
            if aoe_hits:
                questions.append(
                    TriviaQuestion(
                        f"Name a hit in **{god.name}**'s basic attack "
                        f"progression that hits an area."
                        if len(aoe_hits) > 1
                        else f"Which hit in **{god.name}**'s basic attack "
                        f"progression hits an area?",
                        TriviaAnswer(
                            [f"{hit}" for hit in aoe_hits]
                            + [_ordinal(hit) for hit in aoe_hits]
                        ),
                        icon,
                    )
                )

        return questions

    def __init_question_bank(self):
        """Ask only about what this god actually has.

        The bank used to be a fixed list, which assumed every field Smite 1
        populates. Smite 2 has no god classes — `role` is None there, and
        `pros` is empty — so a fixed list crashed on `god.role.name` and would
        have followed it with a pro question that had no answer. What Smite 2
        has instead is positions, specs and an Aspect, which are perfectly good
        questions; they simply are not the same questions.
        """
        god = self.__god
        bank: List[TriviaQuestion] = []

        # The god's own icon, on every question whose answer is not the god.
        # Where the answer *is* the god it has to stay off, since the icon is
        # the whole of the "which god is this?" question.
        icon = getattr(god, "icon_url", None)

        lore = (god.lore or "").replace(god.name, "_____").replace("\\n", "\n")
        if lore.strip():
            bank.append(
                _offer_choices(
                    TriviaQuestion(
                        f"Name the god with this lore: \n\n```{lore}```", god.name
                    ),
                    self.__other_gods(god.pantheon),
                )
            )
        if god.pantheon:
            bank.append(
                _offer_choices(
                    TriviaQuestion(
                        f"What pantheon is **{god.name}** a part of?", god.pantheon, icon
                    ),
                    self.__other_pantheons(),
                )
            )
            odd_one_out = self.__odd_pantheon_out()
            if odd_one_out is not None:
                bank.append(odd_one_out)
        if god.title:
            bank.append(
                _offer_choices(
                    TriviaQuestion(
                        f"Which god has the title **{god.title}**?", god.name
                    ),
                    self.__other_gods(god.pantheon),
                )
            )
        lane = self.__lane_question()
        if lane is not None:
            bank.append(lane)

        god_type = getattr(god, "type", None)
        if god_type is not None:
            bank.append(
                TriviaQuestion(
                    f"Does **{god.name}** deal magical or physical damage?",
                    god_type.value.title(),
                    icon,
                )
            )
        god_range = getattr(god, "range", None)
        if god_range is not None:
            bank.append(
                TriviaQuestion(
                    f"Is **{god.name}** melee or ranged?",
                    god_range.value.title(),
                    icon,
                )
            )

        # Smite 1: a class, and the Pros the API lists.
        if god.pros:
            bank.append(
                TriviaQuestion(
                    f'Name {"one listed" if len(god.pros) > 1 else "the listed"} '
                    f"_pro_ for **{god.name}**.",
                    TriviaAnswer([pro.value.title() for pro in god.pros]),
                    icon,
                )
            )
        if god.role is not None:
            bank.append(
                TriviaQuestion(
                    f"What role is **{god.name}**?", god.role.name.title(), icon
                )
            )

        # Smite 2: where a god is played, what it is for, and its Aspect.
        if god.positions:
            bank.append(
                TriviaQuestion(
                    f'Name {"a" if len(god.positions) > 1 else "the"} position '
                    f"**{god.name}** is played in.",
                    TriviaAnswer([p.value.title() for p in god.positions]),
                    icon,
                )
            )
        if god.specs:
            bank.append(
                TriviaQuestion(
                    f'Name {"one" if len(god.specs) > 1 else "the"} thing '
                    f"**{god.name}** is described as.",
                    TriviaAnswer(list(god.specs)),
                    icon,
                )
            )
        bank.extend(self.__generate_aspect_questions(god, icon))

        self.__question_bank = bank

    def __roster(self) -> List[God]:
        """Every god of this game, for distractors. Empty is a valid answer.

        Only `__get_next_question` builds these generators with a real
        provider; the ability and stat questions carry a round on their own
        where there is none.
        """
        return list((getattr(self.__provider, "gods", None) or {}).values())

    def __other_gods(self, pantheon: str = None) -> List[str]:
        """Names to draw distractors from, from the same pantheon if it can.

        Four Egyptians is a question. An Egyptian, a Norse, a Greek and a Mayan
        is a question you can reason your way out of without knowing the god.
        """
        names = [g.name for g in self.__roster() if g.name != self.__god.name]
        if pantheon:
            same = [
                g.name
                for g in self.__roster()
                if g.name != self.__god.name
                and getattr(g, "pantheon", None) == pantheon
            ]
            if len(same) >= 3:
                return same
        return names

    def __other_pantheons(self) -> List[str]:
        return sorted(
            {
                g.pantheon
                for g in self.__roster()
                if getattr(g, "pantheon", None)
                and g.pantheon != getattr(self.__god, "pantheon", None)
            }
        )

    def __odd_pantheon_out(self) -> Optional[TriviaQuestion]:
        pantheon = self.__god.pantheon
        same = [
            g.name for g in self.__roster() if getattr(g, "pantheon", None) == pantheon
        ]
        elsewhere = [
            g.name
            for g in self.__roster()
            if getattr(g, "pantheon", None) and g.pantheon != pantheon
        ]
        if len(same) < 3 or not elsewhere:
            return None
        return _with_choices(
            TriviaQuestion(
                f"Which of these gods is **not** part of the "
                f"**{pantheon}** pantheon?",
                random.choice(elsewhere),
            ),
            same,
            minimum=4,
        )

    def __lane_question(self) -> Optional[TriviaQuestion]:
        """Where the god builds which damage stat.

        Smite 2 only, and out of the tags the in-game item store filters
        itself by — which makes it the only published answer for a god like
        Danzaburou, who is Strength in the carry lane and Intelligence in mid.
        Unaskable as free text; a list of five lanes is exactly five buttons.
        """
        by_lane = getattr(self.__god, "role_scaling", None) or {}
        lanes_by_stat: Dict[ItemAttribute, List] = {}
        for lane, stat in by_lane.items():
            if stat is not None:
                lanes_by_stat.setdefault(stat, []).append(lane)
        if not lanes_by_stat:
            return None

        stat = random.choice(list(lanes_by_stat))
        lanes = lanes_by_stat[stat]
        elsewhere = [role for role in PlayerRole if role not in lanes]
        if not elsewhere:
            return None

        return _with_choices(
            TriviaQuestion(
                f"Which lane does **{self.__god.name}** build "
                f"**{stat.display_name}** in?",
                TriviaAnswer([lane.value.title() for lane in lanes]),
                getattr(self.__god, "icon_url", None),
            ),
            [role.value.title() for role in elsewhere],
            count=min(4, len(elsewhere) + 1),
        )

    @staticmethod
    def __generate_aspect_questions(god: God, icon) -> List[TriviaQuestion]:
        """An Aspect asked from three sides: name, description, and effect.

        Only the first existed, and an Aspect is more than a name — it is the
        one selection-time choice that changes how a god plays, so what it does
        and which of the kit it touches are the parts worth knowing.
        """
        aspect = getattr(god, "aspect", None)
        if aspect is None or not aspect.name:
            return []

        aspect_icon = getattr(aspect, "icon_url", None) or icon
        questions = [
            TriviaQuestion(f"Which god has the Aspect **{aspect.name}**?", god.name)
        ]

        description = (getattr(aspect, "description", "") or "").replace(
            aspect.name, "_____"
        )
        if description.strip():
            questions.append(
                TriviaQuestion(
                    f"Name **{god.name}**'s Aspect, which does this: "
                    f"\n\n`{description}`",
                    aspect.name,
                    aspect_icon,
                )
            )

        changed = [
            ability.name
            for ability in (getattr(aspect, "changed_abilities", None) or {}).values()
            if ability.name
        ]
        if changed:
            questions.append(
                TriviaQuestion(
                    f'Name {"an" if len(changed) > 1 else "the"} ability of '
                    f"**{god.name}**'s that **{aspect.name}** changes.",
                    TriviaAnswer(changed),
                    aspect_icon,
                )
            )

        return questions

    async def generate_skin_question(self):
        skins = list(
            filter(
                lambda s: s.obtainability not in ("Normal"),
                [
                    Skin.coerce(skin)
                    for skin in await self.__provider.get_god_skins(self.__god.id)
                ],
            )
        )

        if not any(skins):
            return

        skin = random.choice(skins)

        self.__question_bank.append(
            _offer_choices(
                TriviaQuestion(
                    "Which god is this a skin for?", self.__god.name, skin.card_url
                ),
                self.__other_gods(getattr(self.__god, "pantheon", None)),
            )
        )

    def __other_abilities(self, ability) -> List[str]:
        """Distractors for an ability question.

        The god's *own* other abilities first, which is the hardest set there
        is: it means recognising this icon rather than recognising the god.
        """
        names = [
            other.name
            for other in self.__god.abilities
            if other is not ability and other.name
        ]
        if len(names) >= 3:
            return names
        return names + [
            other.name
            for god in self.__roster()
            for other in getattr(god, "abilities", None) or []
            if other.name
        ]

    def __generate_abilities_questions(self, god: God) -> List[TriviaQuestion]:
        # A god whose abilities failed to parse would otherwise take down the
        # round on random.choice of an empty list. Smite 1 always has five;
        # Smite 2's come from a wiki page that could change shape.
        if not god.abilities:
            return []
        ability = random.choice(god.abilities)
        cooldown_rank = (
            random.randint(0, len(ability.cooldown_by_rank) - 1)
            if any(ability.cooldown_by_rank)
            else None
        )
        cost_rank = (
            random.randint(0, len(ability.cost_by_rank) - 1)
            if any(ability.cost_by_rank)
            else None
        )
        ability_with_modifier = (
            f"{int(ability.cost_by_rank[cost_rank])} {ability.cost_modifier}"
            if ability.cost_modifier is not None
            else None
        )

        pattern = re.compile(ability.name, re.IGNORECASE)
        ability_or_passive = f'**{"ability" if not ability.is_passive else "passive"}**'

        return list(
            filter(
                lambda q: q is not None,
                [
                    self.__slot_question(god, ability),
                    self.__property_question(god, ability),
                    _offer_choices(
                        TriviaQuestion(
                            f'Name **{god.name}**\'s {ability_or_passive} with this description: \n\n`{pattern.sub("_____", ability.description)}`',
                            ability.name,
                        ),
                        self.__other_abilities(ability),
                    ),
                    _offer_choices(
                        TriviaQuestion(
                            f"What {ability_or_passive} is this?",
                            ability.name,
                            ability.icon_url,
                        ),
                        self.__other_abilities(ability),
                    ),
                    TriviaQuestion(
                        f"What is the cooldown (in seconds) for **{god.name}'s {ability.name}** at **rank {cooldown_rank + 1}**?",
                        TriviaAnswer(
                            [
                                f"{int(ability.cooldown_by_rank[cooldown_rank])}",
                                f"{int(ability.cooldown_by_rank[cooldown_rank])} seconds",
                            ]
                        ),
                        ability.icon_url,
                    )
                    if cooldown_rank is not None
                    else None,
                    TriviaQuestion(
                        f"How much **{self.__resource_name(god)}** "
                        f"does **{god.name}'s {ability.name}** cost at "
                        f"**rank {cost_rank + 1}**?",
                        TriviaAnswer(
                            list(
                                filter(
                                    lambda a: a is not None,
                                    [
                                        f"{int(ability.cost_by_rank[cost_rank])}",
                                        ability_with_modifier,
                                    ],
                                )
                            )
                        ),
                        ability.icon_url,
                    )
                    if cost_rank is not None
                    else None,
                ],
            )
        )

    @staticmethod
    def __resource_name(god: God) -> str:
        """What the god actually spends, named.

        The question used to hedge — "Mana (or Omi, Rage, etc.)" — because
        Smite 1 gives no signal beyond two hardcoded exceptions. Smite 2
        publishes the resource as a character tag, so there it can simply be
        asked by name, and the hedge is kept only for the pair whose resource
        the Hi-Rez API declines to name.
        """
        resource = (getattr(god, "resource", None) or "mana").strip().lower()
        if resource != "mana":
            return resource.title()
        if getattr(god, "is_manaless", False):
            return "resource (Mana, Omi, Rage, etc.)"
        return "Mana"

    # Smite 1's five abilities arrive in slot order with the passive last, which
    # is the shape this checks for rather than trusting the count: Smite 2's
    # come off a wiki page whose ordering is the page's, not the game's.
    __SLOTS = (["1", "1st"], ["2", "2nd"], ["3", "3rd"], ["Ultimate", "4"])

    @classmethod
    def __slot_question(cls, god: God, ability) -> Optional[TriviaQuestion]:
        abilities = god.abilities
        if len(abilities) != len(cls.__SLOTS) + 1 or not abilities[-1].is_passive:
            return None
        index = abilities.index(ability)
        answers = ["Passive", "5"] if index == len(cls.__SLOTS) else cls.__SLOTS[index]
        return TriviaQuestion(
            f"Which slot is **{god.name}'s {ability.name}** in: "
            f"1, 2, 3, Ultimate or Passive?",
            TriviaAnswer(list(answers)),
            ability.icon_url,
        )

    # Asked separately, and about the whole rank list rather than one value.
    __ASKED_ELSEWHERE = ("cooldown", "cost", "mana cost")

    @classmethod
    def __property_question(cls, god: God, ability) -> Optional[TriviaQuestion]:
        """A stat off the ability's own menu — radius, range, duration.

        Restricted to properties with a single value. A slash-separated one is
        a per-rank list, which has no one right answer to type; the cooldown
        and cost questions handle those two by picking a rank first.
        """
        properties = [
            prop
            for prop in ability.ability_properties
            if prop.value
            and "/" not in prop.value
            and prop.name
            and prop.name.strip().lower() not in cls.__ASKED_ELSEWHERE
        ]
        if not properties:
            return None

        prop = random.choice(properties)
        value = prop.value.strip()
        answers = [value]
        # "55 units" is also answered by "55" — the unit is the wiki's, not
        # something a guesser should have to reproduce.
        number = re.match(r"^-?\d+(?:\.\d+)?", value)
        if number is not None and number.group(0) != value:
            answers.append(number.group(0))

        return TriviaQuestion(
            f"What is the **{prop.name}** of **{god.name}'s {ability.name}**?",
            TriviaAnswer(answers),
            ability.icon_url,
        )


class FriendQuestionGenerator(QuestionGenerator):
    __friends: Dict[int, str]
    __provider: SmiteProvider
    __gods: Dict[GodId, God]
    __question_bank: List[TriviaQuestion]

    def __init__(
        self, friends: Dict[int, str], provider: SmiteProvider, gods: Dict[GodId, God]
    ):
        self.__friends = friends
        self.__provider = provider
        self.__gods = gods
        self.__question_bank = []

    def __god_names(self) -> List[str]:
        """Every god, for distractors. "Who is their worst god?" is unguessable
        as free text against a hundred-god roster and a coin flip against four.
        """
        return [god.name for god in self.__gods.values()]

    async def __get_random_friend(self) -> Tuple[int, Player | None]:
        discord_user_id, smite_user_name = random.choice(list(self.__friends.items()))

        player_ids = await self.__provider.get_player_id_by_name(smite_user_name)

        if not any(player_ids):
            return (discord_user_id, None)

        player_id_info = PlayerId.from_json(player_ids[0], self.__provider)

        if player_id_info.private:
            return (discord_user_id, None)

        player = await player_id_info.get_player()

        if player is not None and player.active_player_id != player.id:
            player = await player_id_info.get_player(
                id_override=player.active_player_id
            )

        if player is None:
            return (discord_user_id, None)

        return (discord_user_id, player)

    async def init_question_bank(self):
        discord_user_id, player = await self.__get_random_friend()

        while player is None:
            discord_user_id, player = await self.__get_random_friend()

        player_display_name = f"**{player.name}** (<@{discord_user_id}>)"

        self.__question_bank = list(
            filter(
                lambda q: q is not None,
                [
                    TriviaQuestion(
                        f"What clan is {player_display_name} a member of?",
                        unidecode(player.clan_name),
                        player.avatar_url,
                    )
                    if player.clan_name is not None and any(player.clan_name.strip())
                    else None,
                    TriviaQuestion(
                        f"What account level (+/- 5) is {player_display_name}?",
                        TriviaAnswer(
                            answer_range=AnswerRange(
                                max(player.level - 5, 0),
                                player.level + 5,
                                player.level,
                            )
                        ),
                        player.avatar_url,
                    ),
                    TriviaQuestion(
                        f"How many times (+/- 10) has {player_display_name} left a game?",
                        TriviaAnswer(
                            answer_range=AnswerRange(
                                max(player.leaves - 10, 0),
                                player.leaves + 10,
                                player.leaves,
                            )
                        ),
                        player.avatar_url,
                    ),
                    TriviaQuestion(
                        f"What is {player_display_name}'s total playtime in hours (+/- 20 hours)?",
                        TriviaAnswer(
                            answer_range=AnswerRange(
                                max(player.hours_played - 20, 0),
                                player.hours_played + 20,
                                player.hours_played,
                            )
                        ),
                        player.avatar_url,
                    ),
                    TriviaQuestion(
                        f"What year did {player_display_name} make their account?",
                        f"{player.created_datetime.year}",
                        player.avatar_url,
                    ),
                    TriviaQuestion(
                        f"What platform does {player_display_name} play on?",
                        player.platform,
                        player.avatar_url,
                    )
                    if (player.platform or "").strip()
                    else None,
                    TriviaQuestion(
                        f"What region does {player_display_name} play in?",
                        player.region,
                        player.avatar_url,
                    )
                    if (player.region or "").strip()
                    else None,
                    TriviaQuestion(
                        f"What mastery level (+/- 5) is {player_display_name}?",
                        TriviaAnswer(
                            answer_range=AnswerRange(
                                max(player.mastery_level - 5, 0),
                                player.mastery_level + 5,
                                player.mastery_level,
                            )
                        ),
                        player.avatar_url,
                    )
                    if player.mastery_level > 0
                    else None,
                    TriviaQuestion(
                        f"How many achievements (+/- 5) does {player_display_name} have?",
                        TriviaAnswer(
                            answer_range=AnswerRange(
                                max(player.total_achievements - 5, 0),
                                player.total_achievements + 5,
                                player.total_achievements,
                            )
                        ),
                        player.avatar_url,
                    )
                    if player.total_achievements > 0
                    else None,
                    TriviaQuestion(
                        f"How many worshippers (+/- 10%) does {player_display_name} "
                        f"have in total?",
                        TriviaAnswer(
                            answer_range=_within_percent(player.total_worshippers)
                        ),
                        player.avatar_url,
                    )
                    if player.total_worshippers > 0
                    else None,
                    TriviaQuestion(
                        f"How many games (+/- 10%) has {player_display_name} won?",
                        TriviaAnswer(answer_range=_within_percent(player.wins)),
                        player.avatar_url,
                    )
                    if player.wins > 0
                    else None,
                    TriviaQuestion(
                        f"What is {player_display_name}'s overall win percent (+/- 5%)?",
                        TriviaAnswer(
                            answer_range=_percent_within_five(
                                player.wins / (player.wins + player.losses)
                            )
                        ),
                        player.avatar_url,
                    )
                    if player.wins + player.losses > 0
                    else None,
                ],
            )
        )

        for queue_id in player.ranked_stats.keys():
            self.__question_bank.extend(
                [
                    TriviaQuestion(
                        f"What rank is {player_display_name} in **{queue_id.display_name}** currently?",
                        player.ranked_stats[queue_id].tier.display_name,
                        player.avatar_url,
                    ),
                    TriviaQuestion(
                        f"What MMR (+/- 100) does {player_display_name} currently have in **{queue_id.display_name}**?",
                        TriviaAnswer(
                            answer_range=AnswerRange(
                                player.ranked_stats[queue_id].mmr - 100,
                                player.ranked_stats[queue_id].mmr + 100,
                                player.ranked_stats[queue_id].mmr,
                            )
                        ),
                        player.avatar_url,
                    ),
                    TriviaQuestion(
                        f"How many times (+/- 5) has {player_display_name} left a **{queue_id.display_name}** game this season?",
                        TriviaAnswer(
                            answer_range=AnswerRange(
                                max(player.ranked_stats[queue_id].leaves - 10, 0),
                                player.ranked_stats[queue_id].leaves + 10,
                                player.ranked_stats[queue_id].leaves,
                            )
                        ),
                        player.avatar_url,
                    ),
                ]
            )

        god_ranks = await self.__provider.get_god_ranks(player.id)

        stats = {
            GodId(int(god["god_id"])): {
                "assists": int(god["Assists"]),
                "deaths": int(god["Deaths"]),
                "kills": int(god["Kills"]),
                "losses": int(god["Losses"]),
                "rank": int(god["Rank"]),
                "wins": int(god["Wins"]),
                "worshippers": int(god["Worshippers"]),
                "minions": int(god["MinionKills"]),
            }
            for god in god_ranks
        }

        for god_id in random.choices(list(stats.keys()), k=2):
            god = self.__gods[god_id]
            god_win_percent = stats[god_id]["wins"] / (
                stats[god_id]["wins"] + stats[god_id]["losses"]
            )
            # The god's icon rather than the player's avatar: the question names
            # the player already, and the god is the part worth picturing.
            god_icon = getattr(god, "icon_url", None) or player.avatar_url
            self.__question_bank.extend(
                list(
                    filter(
                        lambda q: q is not None,
                        [
                            TriviaQuestion(
                                f"How many worshippers (+/- 30) does {player_display_name} have on **{god.name}**?",
                                TriviaAnswer(
                                    answer_range=AnswerRange(
                                        max(stats[god_id]["worshippers"] - 30, 0),
                                        stats[god_id]["worshippers"] + 30,
                                        stats[god_id]["worshippers"],
                                    )
                                ),
                                god_icon,
                            ),
                            TriviaQuestion(
                                f"What is {player_display_name}'s overall win percent (+/- 5%) on **{god.name}**?",
                                TriviaAnswer(
                                    answer_range=_percent_within_five(god_win_percent)
                                ),
                                god_icon,
                            ),
                            TriviaQuestion(
                                f"What mastery rank (+/- 1) is {player_display_name} "
                                f"on **{god.name}**?",
                                TriviaAnswer(
                                    answer_range=AnswerRange(
                                        max(stats[god_id]["rank"] - 1, 0),
                                        stats[god_id]["rank"] + 1,
                                        stats[god_id]["rank"],
                                    )
                                ),
                                god_icon,
                            )
                            if stats[god_id]["rank"] > 0
                            else None,
                            TriviaQuestion(
                                f"How many kills (+/- 10%) does {player_display_name} "
                                f"have on **{god.name}**?",
                                TriviaAnswer(
                                    answer_range=_within_percent(stats[god_id]["kills"])
                                ),
                                god_icon,
                            )
                            if stats[god_id]["kills"] > 0
                            else None,
                        ],
                    )
                )
            )

        for queue_id in random.choices(list(QueueId), k=2):
            queue_list = await self.__provider.get_queue_stats(player.id, queue_id)

            if not any(queue_list):
                continue

            queue_stats = QueueStats.from_json(queue_list)

            self.__question_bank.extend(
                list(
                    filter(
                        lambda q: q is not None,
                        [
                            TriviaQuestion(
                                f"What is {player_display_name}'s win rate (+/- 5%) in {queue_id.display_name}?",
                                TriviaAnswer(
                                    answer_range=AnswerRange(
                                        int(
                                            max(queue_stats.win_percent - 0.05, 0) * 100
                                        ),
                                        int(
                                            min(queue_stats.win_percent + 0.05, 1) * 100
                                        ),
                                        int(queue_stats.win_percent * 100),
                                        is_percent=True,
                                    ),
                                ),
                                player.avatar_url,
                            ),
                            TriviaQuestion(
                                f"What is {player_display_name}'s hours played (+/- 5 hours) in {queue_id.display_name}?",
                                TriviaAnswer(
                                    answer_range=AnswerRange(
                                        int(
                                            max(
                                                (queue_stats.total_minutes - 300) / 60,
                                                0,
                                            )
                                        ),
                                        int((queue_stats.total_minutes + 300) / 60),
                                        int(queue_stats.total_minutes / 60),
                                    ),
                                ),
                                player.avatar_url,
                            ),
                            _offer_choices(
                                TriviaQuestion(
                                    f"What is {player_display_name}'s best god in {queue_id.display_name}?",
                                    self.__gods[queue_stats.best_god].name,
                                    player.avatar_url,
                                ),
                                self.__god_names(),
                            )
                            if queue_stats.best_god is not None
                            else None,
                            _offer_choices(
                                TriviaQuestion(
                                    f"What is {player_display_name}'s worst god in {queue_id.display_name}?",
                                    self.__gods[queue_stats.worst_god].name,
                                    player.avatar_url,
                                ),
                                self.__god_names(),
                            )
                            if queue_stats.worst_god is not None
                            else None,
                        ],
                    )
                )
            )

        player_achievements = await player.get_player_achievements()

        multi_kills = [
            ("Double Kills", player_achievements.double_kills),
            ("Triple Kills", player_achievements.triple_kills),
            ("Quadra Kills", player_achievements.quadra_kills),
            ("Penta Kills", player_achievements.penta_kills),
        ]

        sprees = [
            ("Killing Sprees", player_achievements.killing_spree),
            ("Rampage Sprees", player_achievements.rampage_spree),
            ("Unstoppable Sprees", player_achievements.unstoppable_spree),
            ("Divine Sprees", player_achievements.divine_spree),
            ("Immortal Sprees", player_achievements.immortal_spree),
            ("Godlike Sprees", player_achievements.god_like_spree),
        ]

        objectives = [
            ("Camps", player_achievements.camps_cleared),
            ("Fire Giants", player_achievements.fire_giant_kills),
            ("Gold Furies", player_achievements.gold_fury_kills),
            ("Phoenixes", player_achievements.phoenix_kills),
            ("Siege Juggernauts", player_achievements.siege_juggernaut_kills),
            ("Towers", player_achievements.tower_kills),
            ("Wild Juggernauts", player_achievements.wild_juggernaut_kills),
        ]

        # The lifetime tallies that are not a spree, a multi-kill or an
        # objective, and so had nowhere to be asked from.
        tallies = [
            ("First Bloods", "drawn", player_achievements.first_bloods),
            ("Shutdown Sprees", "ended", player_achievements.shutdown_spree),
            ("god kills", "gotten", player_achievements.player_kills),
            ("assists", "gotten", player_achievements.assisted_kills),
            ("minion kills", "gotten", player_achievements.minion_kills),
            ("deaths", "suffered", player_achievements.deaths),
        ]

        multi_kill_name, multi_kill_count = random.choice(multi_kills)
        spree_name, spree_count = random.choice(sprees)
        objective_name, objective_count = random.choice(objectives)
        tally_name, tally_verb, tally_count = random.choice(tallies)

        self.__question_bank.extend(
            list(
                filter(
                    lambda q: q is not None,
                    [
                        TriviaQuestion(
                            f"How many **{multi_kill_name}** (within +/- 5%) has {player_display_name} gotten?",
                            TriviaAnswer(
                                answer_range=_within_percent(multi_kill_count, 5)
                            ),
                            player.avatar_url,
                        )
                        if multi_kill_count > 0
                        else None,
                        TriviaQuestion(
                            f"How many **{spree_name}** (within +/- 5%) has {player_display_name} been on?",
                            TriviaAnswer(answer_range=_within_percent(spree_count, 5)),
                            player.avatar_url,
                        )
                        if spree_count > 0
                        else None,
                        TriviaQuestion(
                            f"How many **{objective_name}** (within +/- 5%) has {player_display_name} killed?",
                            TriviaAnswer(
                                answer_range=_within_percent(objective_count, 5)
                            ),
                            player.avatar_url,
                        )
                        if objective_count > 0
                        else None,
                        TriviaQuestion(
                            f"How many **{tally_name}** (within +/- 5%) has "
                            f"{player_display_name} {tally_verb}?",
                            TriviaAnswer(answer_range=_within_percent(tally_count, 5)),
                            player.avatar_url,
                        )
                        if tally_count > 0
                        else None,
                    ],
                )
            )
        )

    @property
    def question(self) -> Tuple[discord.Embed, TriviaQuestion, discord.File]:
        question_bank = self.__question_bank.copy()
        question = random.choice(question_bank)
        embed = discord.Embed(description=question.question)
        if question.image_url_or_bytes is not None:
            embed.set_image(url=question.image_url_or_bytes)
        return (embed, question, None)


@dataclass(frozen=True)
class _Catalogue:
    """One game's askable items, split by type."""

    consumables: List[Item] = field(default_factory=list)
    items: List[Item] = field(default_factory=list)
    relics: List[Item] = field(default_factory=list)


class TriviaCategory(Enum):
    CONSUMABLES = 1
    GODS = 2
    ITEMS = 3
    RELICS = 4
    FRIENDS = 5


class SmiteTrivia(commands.Cog):
    __all_items: Dict[int, Item]
    __bot: commands.Bot
    __consumables: List[Item]
    __gods: Dict[GodId, God]
    __items: List[Item]
    __relics: List[Item]
    __provider: SmiteProvider

    __FRIENDS: Dict[int, str] = {
        269238299019706369: "starfoxa",
        231849691250294784: "rawlout",
        143592135730528256: "vinnied",
        269276185656164355: "jalbagel",
        294977341648797706: "artavious",
        325874261682290688: "nastrian",
        270012612048060416: "snootin",
        232171953845305344: "indelmaen",
        145309655122313216: "tyjelly69",
        269980529942593546: "zachjak",
        267050303902187520: "mehtev4s",
        250146567011434506: "doyleville",
        # 326896426619502593: "iskyerdo",
        # 311259851953274880: "ledweasel",
        # 184504307939278859: "WhiskeyTwentyTwo",
        478381808912695298: "NDependntVariabl",
        475838616770314240: "Guenhywvar",
    }

    # How often each category comes up when the user does not pick one. Was an
    # unlabelled [1, 5, 5, 2, 5] positionally aligned to the enum, which broke
    # silently as soon as a category could be withheld for a game.
    __CATEGORY_WEIGHTS = {
        TriviaCategory.CONSUMABLES: 1,
        TriviaCategory.GODS: 5,
        TriviaCategory.ITEMS: 5,
        TriviaCategory.RELICS: 2,
        TriviaCategory.FRIENDS: 5,
    }

    def __init__(self, bot: commands.Bot, providers: Providers):
        self.__bot = bot
        self.providers = providers
        # A trivia round lives entirely inside its coroutine, so there was
        # nothing to ask about whether one was in progress. The deploy guard
        # needs exactly that, since restarting mid-round drops the questions.
        self.__active_rounds = 0
        self.__catalogues: Dict[Game, _Catalogue] = {}

    def catalogue(self, provider) -> "_Catalogue":
        """The askable items for one game, split by type.

        Computed once per game rather than per question — it is a few filters
        over a few hundred items, but it used to happen in the constructor
        against the one provider that existed, which cannot work when the
        question's game is not known until the interaction arrives.
        """
        if provider.game not in self.__catalogues:
            active = [i for i in provider.items.values() if i.active]
            self.__catalogues[provider.game] = _Catalogue(
                consumables=[i for i in active if i.type == ItemType.CONSUMABLE],
                items=[i for i in active if i.type == ItemType.ITEM],
                relics=[i for i in active if i.type == ItemType.RELIC],
            )
        return self.__catalogues[provider.game]

    @commands.slash_command(
        name="trivia",
        description="Start a game of Smite trivia",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    @discord.option(
        name="question_count",
        type=int,
        description="The number of trivia questions to ask",
        default=5,
    )
    @discord.option(
        name="category",
        type=str,
        description="The trivia category to ask questions about",
        choices=[c.name.title() for c in list(TriviaCategory)],
        default="",
    )
    @discord.option(
        name="game",
        type=str,
        description="Which game to ask about; defaults to this server's",
        choices=[g.display_name for g in Game],
        default="",
    )
    async def smitetrivia(
        self,
        ctx: discord.ApplicationContext,
        question_count: int,
        category: str,
        game: str,
    ):
        await self.__smitetrivia(ctx, question_count, category, game)

    @commands.slash_command(
        name="scores",
        description="Show the Smite trivia scoreboard",
        guild_ids=SLASH_COMMAND_GUILD_IDS,
    )
    async def scores(self, ctx: discord.ApplicationContext):
        await self.__scores(ctx)

    @staticmethod
    def __hint(question: TriviaQuestion, guess: str) -> Optional[str]:
        """What to say back to a wrong guess, if anything.

        A number that is too low has always been told so. A name got nothing at
        all, which is a poor deal for the guesser who typed "Rod of Asclepius"
        as "Rod of Asclepious" and cannot tell a spelling slip from being wrong
        about the item.
        """
        answer_number = question.numeric_answer()
        if answer_number is not None and guess.replace("%", "").strip().isdigit():
            if int(guess.replace("%", "")) < answer_number:
                return "try a higher guess. ↗️"
            return "try a lower guess. ↘️"
        return question.hint_for(guess)

    def __check_message(
        self,
        message: discord.Message,
        attempted_answers: dict,
        question: TriviaQuestion,
    ):
        correct = False
        if message.author == self.__bot.user:
            return False

        if message.content.startswith("$stoptrivia"):
            loop = asyncio.get_running_loop()
            loop.create_task(
                message.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.red(), description="Trivia round canceled!"
                    )
                )
            )
            raise StoppedError

        if message.author not in attempted_answers.keys():
            attempted_answers[message.author] = {"answered": 1, "warned": False}
        else:
            attempted_answers[message.author]["answered"] += 1

        correct = question.check_guess(message.content)
        if not correct and attempted_answers[message.author]["answered"] < 3:
            hint = self.__hint(question, message.content)
            if hint is not None:
                asyncio.get_running_loop().create_task(
                    message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.blue(),
                            description=f"Not quite, {message.author.mention}, {hint}",
                        )
                    )
                )

        if correct and attempted_answers[message.author]["answered"] <= 3:
            return correct

        if (
            attempted_answers[message.author]["answered"] >= 3
            and not attempted_answers[message.author]["warned"]
        ):
            loop = asyncio.get_running_loop()
            loop.create_task(
                message.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.red(),
                        description=f"{message.author.mention}, you've reached your maximum number of guesses. <:noshot:782396496104128573> Try again next question!",
                    )
                )
            )
            attempted_answers[message.author]["warned"] = True
            return False

    async def __countdown_loop(
        self,
        message: discord.Interaction | discord.WebhookMessage,
        exp: float,
        embed: discord.Embed,
    ):
        while time.time() < exp:
            await asyncio.sleep(1)
            rem = math.ceil(exp - time.time())
            embed.set_field_at(
                0,
                name="Time Remaining:",
                value=f'_{rem} second{"s" if rem != 1 else ""}_',
            )
            # No `view=` here on purpose: leaving it out of the payload keeps
            # whatever components the message already has, so a multiple-choice
            # question does not lose its buttons once a second.
            await self.__edit_response(message, embed=embed)

    @staticmethod
    async def __edit_response(response, **kwargs):
        """Edit whichever of the two things `ctx.respond` returned."""
        message = (
            await response.original_response()
            if isinstance(response, discord.Interaction)
            else response
        )
        await message.edit(**kwargs)

    async def __close_choices(self, response, view: _ChoiceView):
        """Stop taking presses, however the question ended."""
        view.stop()
        if view.winner.done() and not view.winner.cancelled():
            # The winning press disabled them already, through its own
            # interaction, which is both faster and one fewer API call.
            return
        view.disable_all_items()
        try:
            await self.__edit_response(response, view=view)
        except discord.HTTPException:
            pass

    async def __await_choice(
        self, ctx: discord.ApplicationContext, view: _ChoiceView, exp: float
    ) -> discord.User:
        """The first correct button press, or the round being called off.

        `$stoptrivia` is still typed, so a multiple-choice question has to keep
        listening for messages even though it accepts no typed answers — losing
        the ability to stop a round on the questions that show buttons would be
        a worse trade than the extra listener.
        """
        stopped = asyncio.get_running_loop().create_task(
            self.__bot.wait_for(
                "message",
                check=lambda message: message.author != self.__bot.user
                and message.content.startswith("$stoptrivia"),
            )
        )
        done, pending = await asyncio.wait(
            {stopped, view.winner},
            timeout=max(exp - time.time(), 0),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            # Everything but the winner, which the caller reads to decide
            # whether the buttons still need closing out. Cancelling it would
            # make an unanswered question indistinguishable from an answered one.
            if task is not view.winner:
                task.cancel()

        if stopped in done:
            await ctx.respond(
                embed=discord.Embed(
                    color=discord.Color.red(), description="Trivia round canceled!"
                )
            )
            raise StoppedError
        if view.winner in done:
            return view.winner.result()
        raise asyncio.TimeoutError

    def categories_for(self, provider) -> List[TriviaCategory]:
        """Which categories can be asked about for one game.

        FRIENDS asks about specific players' god mastery through the Hi-Rez
        player API, which has no Smite 2 counterpart, so the category is
        withheld rather than offered and then failed.
        """
        return [
            category
            for category in TriviaCategory
            if category != TriviaCategory.FRIENDS or provider.game is Game.SMITE
        ]

    async def __get_next_question(
        self, provider, category: TriviaCategory = None
    ) -> Tuple[discord.Embed, TriviaQuestion, discord.File]:
        catalogue = self.catalogue(provider)
        if category is None:
            allowed = self.categories_for(provider)
            category = random.choices(
                allowed,
                weights=[self.__CATEGORY_WEIGHTS[c] for c in allowed],
                k=1,
            )[0]
        embed, question, file = await self.__generate(provider, catalogue, category)
        if file is None:
            # One place, so every generator's art is uploaded rather than
            # linked — the tree question was the only one that ever was, and
            # only because it had no URL to link to.
            file = await _attach_image(embed, question)
        return (embed, question, file)

    async def __generate(
        self, provider, catalogue: _Catalogue, category: TriviaCategory
    ) -> Tuple[discord.Embed, TriviaQuestion, discord.File]:
        if category == TriviaCategory.CONSUMABLES:
            return ItemQuestionGenerator(
                random.choice(catalogue.consumables), provider.items
            ).question
        if category == TriviaCategory.GODS:
            generator = GodQuestionGenerator(
                random.choice(list(provider.gods.values())), provider
            )
            await generator.generate_skin_question()
            return generator.question
        if category == TriviaCategory.ITEMS:
            generator = ItemQuestionGenerator(
                random.choice(catalogue.items), provider.items
            )
            await generator.generate_tree_question()
            return generator.question
        if category == TriviaCategory.RELICS:
            return ItemQuestionGenerator(
                random.choice(catalogue.relics), provider.items
            ).question
        if category == TriviaCategory.FRIENDS:
            generator = FriendQuestionGenerator(
                self.__FRIENDS, provider, provider.gods
            )
            await generator.init_question_bank()
            return generator.question
        raise ValueError

    @property
    def active_rounds(self) -> int:
        """Trivia rounds currently in progress. Read by the deploy guard."""
        return self.__active_rounds

    async def __smitetrivia(
        self,
        ctx: discord.ApplicationContext,
        question_count: int,
        input_category: str,
        game: str = "",
    ):
        if ctx.author == self.__bot.user:
            return

        # Discord gives an interaction three seconds to say anything at all,
        # and building the first question does not fit inside that: an item
        # tree fetches an icon per node, a god question asks the API for the
        # skin list, and a friend question makes half a dozen sequential Hi-Rez
        # calls in a loop until it finds a player who is not private. Missing
        # the deadline is a 404 Unknown Interaction that takes the whole round
        # down before a single question is posted. Deferring first buys fifteen
        # minutes, which is how every other slow command here already works.
        await ctx.defer()

        self.__active_rounds += 1
        try:
            await self.__run_trivia_round(
                ctx, question_count, input_category, self.providers.for_ctx(ctx, game)
            )
        finally:
            self.__active_rounds -= 1

    async def __run_trivia_round(
        self,
        ctx: discord.ApplicationContext,
        question_count: int,
        input_category: str,
        provider,
    ):

        correct_answers = {}
        was_stopped = False
        asked_questions = set()

        if question_count > 20:
            await ctx.respond(
                embed=discord.Embed(
                    color=discord.Color.red(),
                    description="The maximum allowed questions per round is 20.",
                )
            )
            return

        if input_category is not None and any(input_category):
            try:
                input_category = TriviaCategory[input_category.upper()]
            except KeyError:
                await ctx.respond(
                    embed=discord.Embed(
                        color=discord.Color.red(),
                        description=f"'{input_category}' is not a valid question category.",
                    )
                )
                return
            if input_category not in self.categories_for(provider):
                await ctx.respond(
                    embed=discord.Embed(
                        color=discord.Color.red(),
                        description=(
                            f"There are no **{input_category.name.title()}** "
                            f"questions for {provider.game.display_name}."
                        ),
                    )
                )
                return
        else:
            input_category = None

        answers = {}
        question: TriviaQuestion = None
        for current_question in range(question_count):
            answers.clear()
            category = input_category
            embed: discord.Embed = None
            file: discord.File = None

            while question is None or question.id in asked_questions:
                embed, question, file = await self.__get_next_question(
                    provider, category
                )

            asked_questions.add(question.id)

            embed.title = (
                f"❔ _Question **{current_question+1}** of **{question_count}**_"
                if question_count > 1
                else "❔ _Question_"
            )
            embed.color = discord.Color.blue()
            embed.add_field(name="Time Remaining:", value="_20 seconds_")

            exp = time.time() + 20
            view = _ChoiceView(question, 20) if question.choices else None
            sent = {"embed": embed}
            if file is not None:
                sent["file"] = file
            if view is not None:
                sent["view"] = view
            response = await ctx.respond(**sent)
            task = asyncio.get_running_loop().create_task(
                self.__countdown_loop(response, exp, embed)
            )
            try:
                if view is not None:
                    # Closed out here rather than at the end of the question, so
                    # the options stop being pressable the moment the answer is
                    # known — not five seconds later, after the reveal.
                    try:
                        author = await self.__await_choice(ctx, view, exp)
                    finally:
                        await self.__close_choices(response, view)
                else:
                    msg: discord.Message = await self.__bot.wait_for(
                        "message",
                        check=lambda msg: self.__check_message(msg, answers, question),
                        timeout=20,
                    )
                    author = msg.author
                answer_time = time.time() - (exp - 20)
                task.cancel()
                description = f"✅ Correct, **{author.display_name}**! You got it in {round(answer_time)} seconds. The answer was **{question.get_answer()}**. <:frogchamp:566686914858713108>"
                if current_question < question_count - 1:
                    description += "\n\nNext question coming up in 5 seconds."

                await ctx.respond(
                    embed=discord.Embed(
                        color=discord.Color.green(), description=description
                    ),
                )

                if author.id not in correct_answers:
                    correct_answers[author.id] = 1
                else:
                    correct_answers[author.id] += 1
                if current_question < question_count - 1:
                    await asyncio.sleep(5)
            except asyncio.TimeoutError:
                description = f"❌⏲️ Time's up! The answer was **{question.get_answer()}**. <:killmyself:472184572407447573>"
                if current_question < question_count - 1:
                    description += "\n\nNext question coming up in 5 seconds."

                await ctx.respond(
                    embed=discord.Embed(
                        color=discord.Color.red(), description=description
                    )
                )
                if current_question < question_count - 1:
                    await asyncio.sleep(5)
            except StoppedError:
                was_stopped = True
                task.cancel()
                break

        if not was_stopped and bool(correct_answers):
            description = [
                f'**{idx + 1}**. _{(await self.__bot.fetch_user(u[0])).display_name}_ (Score: **{u[1]}**) {"<:mleh:472905075208093717>" if idx == 0 else ""}'
                for idx, u in enumerate(
                    sorted(correct_answers.items(), key=lambda i: i[1], reverse=True)
                )
            ]
            embed = discord.Embed(
                color=discord.Color.blue(),
                title="**Round Summary:**",
                description=str.join("\n", description),
            )
            await ctx.respond(embed=embed)

            current_scores = {}
            try:
                with open(paths.data_file("scores.json"), "r", encoding="utf-8") as f:
                    current_scores = json.load(f)
            except (FileNotFoundError, JSONDecodeError):
                pass
            if current_scores:
                for u in correct_answers.keys():
                    if str(u) not in current_scores:
                        current_scores[str(u)] = correct_answers[u]
                    else:
                        current_scores[str(u)] += correct_answers[u]
            else:
                current_scores = correct_answers

            with open(paths.data_file("scores.json"), "w", encoding="utf-8") as f:
                json.dump(current_scores, f)

    async def __scores(self, ctx):
        try:
            with open(paths.data_file("scores.json"), "r", encoding="utf-8") as f:
                current_scores = json.load(f)
                current_scores = sorted(
                    current_scores.items(), key=lambda i: i[1], reverse=True
                )
                description = [
                    f'**{idx + 1}**. _{(await self.__bot.fetch_user(u[0])).display_name}_ (Score: **{u[1]}**) {"<:mleh:472905075208093717>" if idx == 0 else ""}'
                    for idx, u in enumerate(current_scores)
                ]
                embed = discord.Embed(
                    color=discord.Color.blue(),
                    title="**Leaderboard:**",
                    description=str.join("\n", description),
                ).set_thumbnail(
                    url=(
                        await self.__bot.fetch_user(current_scores[0][0])
                    ).display_avatar.url
                )
                await ctx.channel.send(embed=embed)
        except (FileNotFoundError, JSONDecodeError):
            await ctx.channel.send(
                embed=discord.Embed(
                    color=discord.Color.blue(), title="No scores recorded yet!"
                )
            )
