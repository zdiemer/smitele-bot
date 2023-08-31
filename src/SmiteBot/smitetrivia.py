import asyncio
import json
import math
import random
import re
import time
import uuid
from enum import Enum
from json.decoder import JSONDecodeError
from typing import Dict, List, Tuple

import discord
import edit_distance
from discord.ext import commands
from unidecode import unidecode

from god import God, GodId
from item import Item, ItemType


class StoppedError(Exception):
    pass


class TriviaAnswer:
    valid_answers: List[str]

    def __init__(self, answers: List[str]):
        self.valid_answers = answers

    def check_guess(self, guess: str) -> bool:
        for answer in self.valid_answers:
            answer = str(answer).lower().replace("-", " ")
            correct = answer == unidecode(guess).lower().replace("-", " ").replace(
                "%", ""
            )
            if not correct and not answer.replace("%", "").isdigit():
                if answer.startswith("the") and not guess.lower().startswith("the"):
                    answer = answer.replace("the ", "")
                correct = (
                    edit_distance.SequenceMatcher(a=answer, b=guess.lower()).distance()
                    <= 2
                )
            if correct:
                return True
        return False

    def get_answer(self) -> str:
        if len(self.valid_answers) > 1:
            return f'either {", ".join(self.valid_answers[:-1])}{"," if len(self.valid_answers) > 2 else ""} or {self.valid_answers[-1]}'
        return f"{self.valid_answers[0]}"


class TriviaQuestion:
    answer: TriviaAnswer
    id: uuid
    question: str
    image_url: str

    def __init__(
        self, question: str, answer: str | TriviaAnswer, image_url: str = None
    ):
        self.question = question
        self.answer = (
            answer if isinstance(answer, TriviaAnswer) else TriviaAnswer([answer])
        )
        self.id = uuid.uuid4()
        self.image_url = image_url

    def check_guess(self, guess: str) -> bool:
        return self.answer.check_guess(guess)

    def get_answer(self) -> str:
        return self.answer.get_answer()

    def answer_is_number(self) -> bool:
        return len(self.answer.valid_answers) == 1 and all(
            [a.isdigit() for a in self.answer.valid_answers]
        )


class ItemQuestionGenerator:
    __all_items: Dict[int, Item]
    __item: Item
    __question_bank: Dict[ItemType, List[TriviaQuestion]]

    def __init__(self, item: Item, items: Dict[int, Item]):
        self.__all_items = items
        self.__item = item
        self.__init_question_bank()

    @property
    def question(self) -> Tuple[discord.Embed, TriviaQuestion]:
        question_bank = self.__question_bank[self.__item.type].copy()
        if self.__item.type == ItemType.ITEM:
            question_bank.extend(self.__generate_properties_questions(self.__item))
        question = random.choice(self.__question_bank[self.__item.type])
        embed = discord.Embed(description=question.question)
        if question.image_url is not None:
            embed.set_image(url=question.image_url)
        return (embed, question)

    def __compute_price(self, item: Item) -> int:
        price = item.price
        parent_id = item.parent_item_id
        while parent_id is not None:
            parent = self.__all_items[parent_id]
            price += parent.price
            parent_id = parent.parent_item_id
        return price

    def __init_question_bank(self):
        item = self.__item
        self.__question_bank = {
            ItemType.CONSUMABLE: [
                TriviaQuestion(
                    f"How much does "
                    f'{"an" if item.name[0].lower() in "aeiou" else "a"} **{item.name}** cost?',
                    f"{item.price}",
                ),
                TriviaQuestion(
                    f"Name the consumable with this description: \n\n`{item.passive}`",
                    item.name,
                ),
                TriviaQuestion("What consumable is this?", item.name, item.icon_url),
            ],
            ItemType.RELIC: [
                TriviaQuestion(
                    f"Name the relic with this description: \n\n`{item.passive}`",
                    item.name,
                ),
                TriviaQuestion("What relic is this?", item.name, item.icon_url),
            ],
            ItemType.ITEM: list(
                filter(
                    lambda q: q is not None,
                    [
                        TriviaQuestion(
                            f"How much does **{item.name}** cost?",
                            f"{self.__compute_price(item)}",
                        ),
                        TriviaQuestion(
                            f'Name the item with this {"passive" if item.passive is not None and item.passive != "" else "aura"}'
                            f':\n\n`{item.passive if item.passive is not None and item.passive != "" else item.aura}`',
                            item.name,
                        )
                        if (item.passive is not None and item.passive != "")
                        or (item.aura is not None and item.aura != "")
                        else None,
                        TriviaQuestion(
                            f"How much does it cost to upgrade **{self.__all_items[item.parent_item_id].name}** into **{item.name}**?",
                            f"{item.price}",
                        )
                        if item.parent_item_id is not None and item.price > 0
                        else None,
                        TriviaQuestion("What item is this?", item.name, item.icon_url),
                    ],
                )
            ),
        }

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


class GodQuestionGenerator:
    __all_gods: Dict[GodId, God]
    __god: God
    __question_bank: List[TriviaQuestion]

    def __init__(self, god: God, gods: Dict[GodId, God]):
        self.__all_gods = gods
        self.__god = god
        self.__init_question_bank()

    @property
    def question(self) -> Tuple[discord.Embed, TriviaQuestion]:
        question_bank = self.__question_bank.copy()
        question_bank.extend(self.__generate_abilities_questions(self.__god))
        question = random.choice(question_bank)
        embed = discord.Embed(description=question.question)
        if question.image_url is not None:
            embed.set_image(url=question.image_url)
        return (embed, question)

    def __init_question_bank(self):
        god = self.__god
        lore = god.lore.replace(god.name, "_____").replace("\\n", "\n")
        self.__question_bank = [
            TriviaQuestion(f"Name the god with this lore: \n\n```{lore}```", god.name),
            TriviaQuestion(f"What pantheon is **{god.name}** a part of?", god.pantheon),
            TriviaQuestion(f"Which god has the title **{god.title}**?", god.name),
            TriviaQuestion(
                f'Name {"one listed" if len(god.pros) > 1 else "the listed"} _pro_ for **{god.name}**.',
                TriviaAnswer([pro.value.title() for pro in god.pros]),
            ),
        ]

    @staticmethod
    def __generate_abilities_questions(god: God) -> List[TriviaQuestion]:
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

        return list(
            filter(
                lambda q: q is not None,
                [
                    TriviaQuestion(
                        f'Name **{god.name}**\'s ability with this description: \n\n`{pattern.sub("_____", ability.description)}`',
                        ability.name,
                    ),
                    TriviaQuestion(
                        "What ability is this?", ability.name, ability.icon_url
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
                        f"What is the Mana (or Omi, Rage, etc.) cost for **{god.name}'s** **{ability.name}** at **rank {cost_rank + 1}**?",
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


class TriviaCategory(Enum):
    CONSUMABLES = 1
    GODS = 2
    ITEMS = 3
    RELICS = 4


class SmiteTrivia(commands.Cog):
    __all_items: Dict[int, Item]
    __bot: commands.Bot
    __consumables: List[Item]
    __gods: Dict[GodId, God]
    __items: List[Item]
    __relics: List[Item]

    def __init__(
        self, bot: commands.Bot, gods: Dict[GodId, God], items: Dict[int, Item]
    ):
        self.__bot = bot
        self.__gods = gods
        self.__all_items = items

        active_item_list = list(filter(lambda i: i.active, list(items.values())))

        self.__consumables = list(
            filter(lambda i: i.type == ItemType.CONSUMABLE, active_item_list)
        )
        self.__items = list(filter(lambda i: i.type == ItemType.ITEM, active_item_list))
        self.__relics = list(
            filter(lambda i: i.type == ItemType.RELIC, active_item_list)
        )

    @commands.command(aliases=["trivia"])
    async def smitetrivia(self, message: discord.Message, *args: tuple):
        await self.__smitetrivia(message, *args)

    @commands.command()
    async def scores(self, ctx: commands.Context):
        await self.__scores(ctx)

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
        if (
            not correct
            and question.answer_is_number()
            and message.content.replace("%", "").isdigit()
            and attempted_answers[message.author]["answered"] < 3
        ):
            guess = int(message.content.replace("%", ""))
            answer_number = int(question.get_answer())
            loop = asyncio.get_running_loop()

            if guess < answer_number:
                loop.create_task(
                    message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.blue(),
                            description=f"Not quite, {message.author.mention}, try a higher guess. ↗️",
                        )
                    )
                )
            else:
                loop.create_task(
                    message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.blue(),
                            description=f"Not quite, {message.author.mention}, try a lower guess. ↘️",
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
        self, message: discord.Message, exp: float, embed: discord.Embed
    ):
        while time.time() < exp:
            await asyncio.sleep(1)
            rem = math.ceil(exp - time.time())
            embed.set_field_at(
                0,
                name="Time Remaining:",
                value=f'_{rem} second{"s" if rem != 1 else ""}_',
            )
            await message.edit(embed=embed)

    def __get_next_question(
        self, category: TriviaCategory = None
    ) -> Tuple[discord.Embed, TriviaQuestion]:
        if category is None:
            category = random.choices(
                list(TriviaCategory), weights=[1, 10, 10, 2], k=1
            )[0]
        if category == TriviaCategory.CONSUMABLES:
            return ItemQuestionGenerator(
                random.choice(self.__consumables), self.__all_items
            ).question
        if category == TriviaCategory.GODS:
            return GodQuestionGenerator(
                random.choice(list(self.__gods.values())), self.__gods
            ).question
        if category == TriviaCategory.ITEMS:
            return ItemQuestionGenerator(
                random.choice(self.__items), self.__all_items
            ).question
        if category == TriviaCategory.RELICS:
            return ItemQuestionGenerator(
                random.choice(self.__relics), self.__all_items
            ).question
        raise ValueError

    async def __smitetrivia(self, message: discord.Message, *args: tuple):
        if message.author == self.__bot.user:
            return

        question_count = 1
        input_category = None
        correct_answers = {}
        was_stopped = False
        asked_questions = set()

        if args is not None:
            args = ["".join(arg) for arg in args]
            if len(args) > 2:
                await message.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.red(),
                        description="Invalid command! This bot accepts "
                        "the command `$smitetrivia` (or `$st`) with "
                        "optional count and category arguments, e.g. "
                        "`$smitetrivia 10 items`",
                    )
                )
                return
            if len(args) == 2:
                try:
                    question_count = int(args[0])
                    if question_count > 20:
                        await message.channel.send(
                            embed=discord.Embed(
                                color=discord.Color.red(),
                                description="The maximum allowed questions per round is 20.",
                            )
                        )
                        return
                except ValueError:
                    await message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.red(),
                            description="Question count must be a number.",
                        )
                    )
                    return

                try:
                    input_category = TriviaCategory[args[1].upper()]
                except KeyError:
                    await message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.red(),
                            description=f"'{args[1]}' is not a valid question category.",
                        )
                    )
                    return
            elif len(args) == 1:
                try:
                    question_count = int(args[0])
                    if question_count > 20:
                        await message.channel.send(
                            embed=discord.Embed(
                                color=discord.Color.red(),
                                description="The maximum allowed questions per round is 20.",
                            )
                        )
                        return
                except ValueError:
                    await message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.red(),
                            description="Question count must be a number.",
                        )
                    )
                    return
        answers = {}
        question: TriviaQuestion = None
        for current_question in range(question_count):
            answers.clear()
            category = input_category
            embed: discord.Embed = None

            while question is None or question.id in asked_questions:
                embed, question = self.__get_next_question(category)

            asked_questions.add(question.id)

            embed.title = (
                f"❔ _Question **{current_question+1}** of **{question_count}**_"
                if question_count > 1
                else "❔ _Question_"
            )
            embed.color = discord.Color.blue()
            embed.add_field(name="Time Remaining:", value="_20 seconds_")

            exp = time.time() + 20
            task = asyncio.get_running_loop().create_task(
                self.__countdown_loop(
                    await message.channel.send(embed=embed), exp, embed
                )
            )
            try:
                msg: discord.Message = await self.__bot.wait_for(
                    "message",
                    check=lambda msg: self.__check_message(msg, answers, question),
                    timeout=20,
                )
                answer_time = time.time() - (exp - 20)
                task.cancel()
                description = f"✅ Correct, **{msg.author.display_name}**! You got it in {round(answer_time)} seconds. The answer was **{question.get_answer()}**. <:frogchamp:566686914858713108>"
                if current_question < question_count - 1:
                    description += "\n\nNext question coming up in 5 seconds."

                await message.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.green(), description=description
                    )
                )

                if msg.author.id not in correct_answers:
                    correct_answers[msg.author.id] = 1
                else:
                    correct_answers[msg.author.id] += 1
                if current_question < question_count - 1:
                    await asyncio.sleep(5)
            except asyncio.TimeoutError:
                description = f"❌⏲️ Time's up! The answer was **{question.get_answer()}**. <:killmyself:472184572407447573>"
                if current_question < question_count - 1:
                    description += "\n\nNext question coming up in 5 seconds."

                await message.channel.send(
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
            await message.channel.send(embed=embed)

            current_scores = {}
            try:
                with open("scores.json", "r", encoding="utf-8") as f:
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

            with open("scores.json", "w", encoding="utf-8") as f:
                json.dump(current_scores, f)

    async def __scores(self, ctx):
        try:
            with open("scores.json", "r", encoding="utf-8") as f:
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
