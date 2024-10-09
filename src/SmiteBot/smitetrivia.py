import asyncio
import io
import json
import math
import random
import re
import time
import uuid
from enum import Enum
from json.decoder import JSONDecodeError
from typing import Dict, List, Optional, Tuple

import discord
import edit_distance
from discord.ext import commands
from unidecode import unidecode

from player import Player, PlayerId
from player_stats import QueueStats
from SmiteProvider import SmiteProvider
from god import God, GodId
from item import Item, ItemType
from skin import Skin
from HirezAPI import QueueId
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
                answer = unidecode(str(answer)).lower().replace("-", " ")
                correct = answer == unidecode(guess).lower().replace("-", " ").replace(
                    "%", ""
                )
                if not correct and not answer.replace("%", "").isdigit():
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
    image_url_or_bytes: str | io.BytesIO

    def __init__(
        self,
        question: str,
        answer: str | TriviaAnswer,
        image_url_or_bytes: str | io.BytesIO = None,
    ):
        self.question = question
        self.answer = (
            answer if isinstance(answer, TriviaAnswer) else TriviaAnswer([answer])
        )
        self.id = uuid.uuid4()
        self.image_url_or_bytes = image_url_or_bytes

    def check_guess(self, guess: str) -> bool:
        return self.answer.check_guess(guess)

    def get_answer(self) -> str:
        return self.answer.get_answer()

    def answer_is_number(self) -> bool:
        return (
            self.answer.valid_answers is not None
            and len(self.answer.valid_answers) == 1
            and all([a.isdigit() for a in self.answer.valid_answers])
        ) or self.answer.answer_range is not None


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
        if self.__item.type == ItemType.ITEM and any(self.__item.item_properties):
            question_bank.extend(self.__generate_properties_questions(self.__item))
        question = random.choice(self.__question_bank[self.__item.type])
        embed = discord.Embed(description=question.question)
        if question.image_url_or_bytes is not None:
            if isinstance(question.image_url_or_bytes, io.BytesIO):
                file = discord.File(question.image_url_or_bytes, filename="tree.png")
                embed.set_image(url="attachment://tree.png")
                return (embed, question, file)
            embed.set_image(url=question.image_url_or_bytes)
        return (embed, question, None)

    def __compute_price(self, item: Item) -> int:
        price = item.price
        parent_id = item.parent_item_id
        while parent_id is not None:
            parent = self.__all_items[parent_id]
            price += parent.price
            parent_id = parent.parent_item_id
        return price

    async def generate_tree_question(self):
        tree_builder = ItemTreeBuilder(self.__all_items)
        tree_image = await tree_builder.generate_build_tree(
            self.__item, trivia_mode=True
        )

        self.__question_bank[ItemType.ITEM].append(
            TriviaQuestion(
                "What item has been replaced by a question mark in this tree?",
                tree_builder.trivia_item.name,
                tree_image,
            )
        )

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
                            f'Name the item with this {"passive" if item.passive is not None and item.passive.strip() != "" else "aura"}'
                            f':\n\n`{item.passive if item.passive is not None and item.passive.strip() != "" else item.aura.strip()}`',
                            item.name,
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
        question = random.choice(question_bank)
        embed = discord.Embed(description=question.question)
        if question.image_url_or_bytes is not None:
            embed.set_image(url=question.image_url_or_bytes)
        return (embed, question, None)

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
            TriviaQuestion(f"What role is **{god.name}**?", god.role.name.title()),
        ]

    async def generate_skin_question(self):
        skins = list(
            filter(
                lambda s: s.obtainability not in ("Normal"),
                [
                    Skin.from_json(skin)
                    for skin in await self.__provider.get_god_skins(self.__god.id)
                ],
            )
        )

        if not any(skins):
            return

        skin = random.choice(skins)

        self.__question_bank.append(
            TriviaQuestion(
                "Which god is this a skin for?", self.__god.name, skin.card_url
            )
        )

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
        ability_or_passive = f'**{"ability" if not ability.is_passive else "passive"}**'

        return list(
            filter(
                lambda q: q is not None,
                [
                    TriviaQuestion(
                        f'Name **{god.name}**\'s {ability_or_passive} with this description: \n\n`{pattern.sub("_____", ability.description)}`',
                        ability.name,
                    ),
                    TriviaQuestion(
                        f"What {ability_or_passive} is this?",
                        ability.name,
                        ability.icon_url,
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
            god_win_percent = stats[god_id]["wins"] / (
                stats[god_id]["wins"] + stats[god_id]["losses"]
            )
            self.__question_bank.extend(
                [
                    TriviaQuestion(
                        f"How many worshippers (+/- 30) does {player_display_name} have on **{self.__gods[god_id].name}**?",
                        TriviaAnswer(
                            answer_range=AnswerRange(
                                max(stats[god_id]["worshippers"] - 30, 0),
                                stats[god_id]["worshippers"] + 30,
                                stats[god_id]["worshippers"],
                            )
                        ),
                        player.avatar_url,
                    ),
                    TriviaQuestion(
                        f"What is {player_display_name}'s overall win percent (+/- 5%) on **{self.__gods[god_id].name}**?",
                        TriviaAnswer(
                            answer_range=AnswerRange(
                                int(max(god_win_percent - 0.05, 0) * 100),
                                int(min(god_win_percent + 0.05, 1) * 100),
                                int(god_win_percent * 100),
                                is_percent=True,
                            )
                        ),
                        player.avatar_url,
                    ),
                ]
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
                            TriviaQuestion(
                                f"What is {player_display_name}'s best god in {queue_id.display_name}?",
                                self.__gods[queue_stats.best_god].name,
                                player.avatar_url,
                            )
                            if queue_stats.best_god is not None
                            else None,
                            TriviaQuestion(
                                f"What is {player_display_name}'s worst god in {queue_id.display_name}?",
                                self.__gods[queue_stats.worst_god].name,
                                player.avatar_url,
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

        multi_kill_name, multi_kill_count = random.choice(multi_kills)
        spree_name, spree_count = random.choice(sprees)
        objective_name, objective_count = random.choice(objectives)

        self.__question_bank.extend(
            list(
                filter(
                    lambda q: q is not None,
                    [
                        TriviaQuestion(
                            f"How many **{multi_kill_name}** (within +/- 5%) has {player_display_name} gotten?",
                            TriviaAnswer(
                                answer_range=AnswerRange(
                                    max(
                                        math.ceil(
                                            multi_kill_count - (multi_kill_count * 0.05)
                                        ),
                                        0,
                                    ),
                                    math.ceil(
                                        multi_kill_count + (multi_kill_count * 0.05)
                                    ),
                                    multi_kill_count,
                                )
                            ),
                        )
                        if multi_kill_count > 0
                        else None,
                        TriviaQuestion(
                            f"How many **{spree_name}** (within +/- 5%) has {player_display_name} been on?",
                            TriviaAnswer(
                                answer_range=AnswerRange(
                                    max(
                                        math.ceil(spree_count - (spree_count * 0.05)), 0
                                    ),
                                    math.ceil(spree_count + (spree_count * 0.05)),
                                    spree_count,
                                )
                            ),
                        )
                        if spree_count > 0
                        else None,
                        TriviaQuestion(
                            f"How many **{objective_name}** (within +/- 5%) has {player_display_name} killed?",
                            TriviaAnswer(
                                answer_range=AnswerRange(
                                    max(
                                        math.ceil(
                                            objective_count - (objective_count * 0.05)
                                        ),
                                        0,
                                    ),
                                    math.ceil(
                                        objective_count + (objective_count * 0.05)
                                    ),
                                    objective_count,
                                )
                            ),
                        )
                        if objective_count > 0
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

    def __init__(self, bot: commands.Bot, provider: SmiteProvider):
        self.__bot = bot
        self.__gods = provider.gods
        self.__all_items = provider.items
        self.__provider = provider

        active_item_list = list(
            filter(lambda i: i.active, list(self.__all_items.values()))
        )

        self.__consumables = list(
            filter(lambda i: i.type == ItemType.CONSUMABLE, active_item_list)
        )
        self.__items = list(filter(lambda i: i.type == ItemType.ITEM, active_item_list))
        self.__relics = list(
            filter(lambda i: i.type == ItemType.RELIC, active_item_list)
        )

    @commands.slash_command(
        name="trivia",
        description="Start a game of Smite trivia",
        guild_ids=[845718807509991445, 396874836250722316, 480512578779611146],
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
    async def smitetrivia(
        self, ctx: discord.ApplicationContext, question_count: int, category: str
    ):
        await self.__smitetrivia(ctx, question_count, category)

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
            answer_number = int(question.get_answer().replace("%", ""))
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
            if isinstance(message, discord.Interaction):
                interaction_message = await message.original_response()
                await interaction_message.edit(embed=embed)
                continue
            await message.edit(embed=embed)

    async def __get_next_question(
        self, category: TriviaCategory = None
    ) -> Tuple[discord.Embed, TriviaQuestion, discord.File]:
        if category is None:
            category = random.choices(
                list(TriviaCategory), weights=[1, 5, 5, 2, 5], k=1
            )[0]
        if category == TriviaCategory.CONSUMABLES:
            return ItemQuestionGenerator(
                random.choice(self.__consumables), self.__all_items
            ).question
        if category == TriviaCategory.GODS:
            generator = GodQuestionGenerator(
                random.choice(list(self.__gods.values())), self.__provider
            )
            await generator.generate_skin_question()
            return generator.question
        if category == TriviaCategory.ITEMS:
            generator = ItemQuestionGenerator(
                random.choice(self.__items), self.__all_items
            )
            await generator.generate_tree_question()
            return generator.question
        if category == TriviaCategory.RELICS:
            return ItemQuestionGenerator(
                random.choice(self.__relics), self.__all_items
            ).question
        if category == TriviaCategory.FRIENDS:
            generator = FriendQuestionGenerator(
                self.__FRIENDS, self.__provider, self.__gods
            )
            await generator.init_question_bank()
            return generator.question
        raise ValueError

    async def __smitetrivia(
        self, ctx: discord.ApplicationContext, question_count: int, input_category: str
    ):
        if ctx.author == self.__bot.user:
            return

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
                embed, question, file = await self.__get_next_question(category)

            asked_questions.add(question.id)

            embed.title = (
                f"❔ _Question **{current_question+1}** of **{question_count}**_"
                if question_count > 1
                else "❔ _Question_"
            )
            embed.color = discord.Color.blue()
            embed.add_field(name="Time Remaining:", value="_20 seconds_")

            exp = time.time() + 20
            response = (
                await ctx.respond(embed=embed)
                if file is None
                else await ctx.respond(file=file, embed=embed)
            )
            task = asyncio.get_running_loop().create_task(
                self.__countdown_loop(response, exp, embed)
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

                await ctx.respond(
                    embed=discord.Embed(
                        color=discord.Color.green(), description=description
                    ),
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
