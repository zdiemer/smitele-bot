import asyncio
import json
import math
import random
import time
from json.decoder import JSONDecodeError
from typing import Callable, List

import discord
from discord.ext import commands
import editdistance
from unidecode import unidecode

from HirezAPI import Smite
from item import Item, ItemType

intents = discord.Intents.default()
intents.messages = True
bot = commands.Bot(command_prefix="%", intents=intents)
config = {}

with open("config.json") as f:
    config = json.load(f)

smite_client = Smite(config["hirezAuthKey"], config["hirezDevId"])


async def load_items():
    items = None
    try:
        with open("items.json", "r") as file:
            items = json.load(file)
    except (JSONDecodeError, FileNotFoundError):
        items = await smite_client.get_items()
        with open("items.json", "w") as file:
            json.dump(items, file)
    return [Item.from_json(obj) for obj in items]


consumables_questions: List[Callable[[Item], any]] = [
    lambda c: {
        "question": discord.Embed(
            description=f'How much does {"an" if c.name[0].lower() in "aeiou" else "a"} **{c.nam}** cost?'
        ),
        "answer": c.price,
        "id": f"{c.name}-1",
    },
    # lambda c: ((lambda _c, i: {
    #     "question": discord.Embed(description=f'How much **{_c["effects"][i]["stat"]}** does {"an" if c["name"][0].lower() in "aeiou" else "a"} **{_c["name"]}** provide?'),
    #     "answer": _c['effects'][i]['value'] if _c['effects'][i]['type'] == "flat" else f'{_c["effects"][i]["value"]}%',
    #     "id": f'{_c["name"]}{_c["effects"][i]["stat"]}-1'
    # })(c, random.randrange(len(c['effects'])))) if 'effects' in c.keys() else None,
    lambda c: {
        "question": discord.Embed(
            description=f"Name the consumable with this description: \n\n`{c.description}`"
        ),
        "answer": c.name,
        "id": f"{c.name}-2",
    },
    # lambda c: {
    #     "question": discord.Embed(description=f'How long (in seconds) do the effects of {"an" if c.name[0].lower() in "aeiou" else "a"} **{c.name}** last?'),
    #     "answer": c.duration,
    #     "id": f'{c.name}-3'
    # } if 'duration' in c.keys() else None,
    lambda c: {
        "question": discord.Embed(description="What consumable is this?").set_image(
            url=c.icon_url
        ),
        "answer": c.name,
        "id": f"{c.name}-4",
    },
    lambda _: {
        "question": discord.Embed(description="What is the range of a **Ward**?"),
        "answer": 45,
        "id": "consumables-5",
    },
    lambda _: {
        "question": discord.Embed(
            description="How long does a **Ward** last (in seconds)?"
        ),
        "answer": 180,
        "id": "consumables-6",
    },
    lambda _: {
        "question": discord.Embed(
            description="How much True damage does **Hand of the Gods** do to Jungle Monsters?"
        ),
        "answer": 200,
        "id": "consumables-7",
    },
    # lambda c: {
    #     "question": discord.Embed(description=f'What level must you be to purchase {"an" if c.name[0].lower() in "aeiou" else "a"} **{c.name}**?'),
    #     "answer": c.level,
    #     "id": f'{c.name}-8'
    # } if 'level' in c.keys() else None,
]

relics_questions: List[Callable[[Item], any]] = [
    # lambda relic: ((lambda r, i: {
    #     "question": discord.Embed(
    #         description=f'**{r["name"]} {r["effects"][i]["stat"]}** {"against" if r["effects"][i]["target"] == "enemies" else "of"} {r["effects"][i]["target"]} by how much?'),
    #     "answer": r['effects'][i]['value'] if r['effects'][i]['type'] == 'flat' else f'{r["effects"][i]["value"]}%',
    #     "id": f'{r["name"]}{r["effects"][i]["stat"]}-1'
    # })(relic, random.randrange(len(relic['effects'])))) if 'effects' in relic.keys() else None,
    lambda relic: {
        "question": discord.Embed(
            description=f"Name the relic with this description: \n\n`{relic.name}`"
        ),
        "answer": relic.name,
        "id": f"{relic.name}-1",
    },
    # lambda relic: {
    #     "question": discord.Embed(description=f'What is the range of the relic **{relic["name"]}**?'),
    #     "answer": relic['range'],
    #     "id": f'{relic["name"]}-2'
    # } if 'range' in relic.keys() else None,
    # lambda relic: {
    #     "question": discord.Embed(description=f'What is the cooldown (in seconds) on the relic **{relic["name"]}**?'),
    #     "answer": relic['cooldown'],
    #     "id": f'{relic["name"]}-3'
    # } if 'cooldown' in relic.keys() else None,
    # lambda relic: {
    #     "question": discord.Embed(description=f'How long (in seconds) does the relic **{relic["name"]}** last?'),
    #     "answer": relic['duration'],
    #     "id": f'{relic["name"]}-4'
    # } if 'duration' in relic.keys() else None,
    lambda relic: {
        "question": discord.Embed(description="What relic is this?").set_image(
            url=relic.icon_url
        ),
        "answer": relic.name,
        "id": f"{relic.name}-5",
    },
]

items_questions: List[Callable[[Item], any]] = [
    lambda item: {
        "question": discord.Embed(description=f"How much does **{item.name}** cost?"),
        "answer": item.price,
        "id": f"{item.name}-1",
    },
    lambda item: (
        (
            lambda i, statIdx: {
                "question": discord.Embed(
                    description=f'{"How much" if i.item_properties[statIdx].flat_value is not None else "What percent"} **{i.item_properties[statIdx].attribute.value.title()}** does **{i.name}** provide?'
                ),
                "answer": i.item_properties[statIdx].flat_value
                if i.item_properties[statIdx].flat_value is not None
                else f"{i.item_properties[statIdx].percent_value * 100}%",
                "id": f"{i.name}{i.item_properties[statIdx].attribute.value}-1",
            }
        )(item, random.randrange(len(item.item_properties)))
    ),
    lambda item: (
        (
            lambda i, statIdx: {
                "question": discord.Embed(
                    description=f'{"How much" if i.item_properties[statIdx].flat_value is not None else "What percent"} **{i.item_properties[statIdx].attribute.value.title()}** does this item provide?'
                ).set_image(url=item.icon_url),
                "answer": i.item_properties[statIdx].flat_value
                if i.item_properties[statIdx].flat_value is not None
                else f"{i.item_properties[statIdx].percent_value * 100}%",
                "id": f"{i.name}{i.item_properties[statIdx].attribute.value}-1",
            }
        )(item, random.randrange(len(item.item_properties)))
    ),
    lambda item: {
        "question": discord.Embed(
            description=f"Name the item with this passive:\n\n`{item.passive}`"
        ),
        "answer": item.name,
        "id": f"{item.name}-2",
    }
    if "passive" in item.keys()
    else {
        "question": discord.Embed(
            description=f"Name the item with this description:\n\n`{item.description}`"
        ),
        "answer": item.name,
        "id": f"{item.name}-2",
    }
    if item.description is not None
    else None,
    # lambda item: ((lambda i, upgradeIdx: {
    #     "question": discord.Embed(description=f'How much does it cost to upgrade **{i["name"]}** into **{items[str(i["upgrades"][upgradeIdx]["upgradeId"])]["name"]}**?'),
    #     "answer": i["upgrades"][upgradeIdx]['cost'],
    #     "id": f'{item["name"]}-3'
    # })(item, random.randrange(len(item['upgrades'])))) if 'upgrades' in item.keys() else None,
    lambda item: {
        "question": discord.Embed(description="What item is this?").set_image(
            url=item.icon_url
        ),
        "answer": item.name,
        "id": f"{item.name}-4",
    },
    # lambda item: {
    #     "question": discord.Embed(description=f'How many stacks does it take to fully stack **{item["name"]}**?'),
    #     "answer": item['stacks']['max'],
    #     "id": f'{item["name"]}-5'
    # } if 'stacks' in item else None,
    # lambda item: ((lambda i, stat: {
    #     "question": discord.Embed(description=f'{"How much" if i["stacks"]["per_stack"][stat]["type"] == "flat" else "What percent"} **{i["stacks"]["per_stack"][stat]["stat"]}** does one stack on **{i["name"]}** provide?'),
    #     "answer": i["stacks"]["per_stack"][stat]["value"],
    #     "id": f'{item["name"]}{i["stacks"]["per_stack"][stat]["stat"]}-6'
    # })(item, random.randrange(len(item['stacks']['per_stack'])))) if 'stacks' in item and 'per_stack' in item['stacks'] else None,
    # lambda item: ((lambda i, stat: {
    #     "question": discord.Embed(description=f'{"How much" if i["stacks"]["evolved"]["effects"][stat]["type"] == "flat" else "What percent"} **{i["stacks"]["evolved"]["effects"][stat]["stat"]}** does **Evolved {i["name"]}** provide?'),
    #     "answer": i["stacks"]["evolved"]["effects"][stat]["value"],
    #     "id": f'{item["name"]}{i["stacks"]["evolved"]["effects"][stat]["stat"]}-7'
    # })(item, random.randrange(len(item['stacks']['evolved']["effects"])))) if 'stacks' in item and 'evolved' in item['stacks'] and 'effects' in item['stacks']['evolved'] else None,
    # lambda item: {
    #     "question": discord.Embed(description=f'Name the **Evolved** item with this passive:\n\n`{item["stacks"]["evolved"]["passive"]}`'),
    #     "answer": item['name'],
    #     "id": f'{item["name"]}-8'
    # } if 'stacks' in item and 'evolved' in item['stacks'] and 'passive' in item['stacks']['evolved'] else None,
    # lambda item: ((lambda i, stat: {
    #     "question": discord.Embed(description=f'{"How much" if i["stacks"]["per_stack"][stat]["type"] == "flat" else "What percent"} **{i["stacks"]["per_stack"][stat]["stat"]}** does fully stacked **{i["name"]}** provide (including base stats)?'),
    #     "answer": (float(i["stacks"]["per_stack"][stat]["value"]) * int(i["stacks"]["max"])) + (int([s["value"] for s in i["effects"] if s["stat"] == i["stacks"]["per_stack"][stat]["stat"]][::] or 1) if "effects" in i else 1),
    #     "id": f'{item["name"]}{i["stacks"]["per_stack"][stat]["stat"]}-6'
    # })(item, random.randrange(len(item['stacks']['per_stack'])))) if 'stacks' in item and 'per_stack' in item['stacks'] and 'evolved' not in item['stacks'] else None,
    # lambda item: {
    #     "question": discord.Embed(description=f'What items does {item["name"]} upgrade directly into ({len(item["upgrades"])} answers)?'),
    #     "answer": [items[u["upgradeId"]]["name"] for u in item["upgrades"]]
    # } if 'upgrades' in item else None,
]


class StoppedError(Exception):
    pass


@bot.event
async def on_ready():
    activity = discord.Game(name="Smite", type=3)
    await bot.change_presence(status=discord.Status.online, activity=activity)
    print("Smite Trivia Bot is ready!")


async def countdown_loop(message, exp, embed):
    while time.time() < exp:
        await asyncio.sleep(1)
        rem = math.ceil(exp - time.time())
        embed.set_field_at(
            0, name="Time Remaining:", value=f'_{rem} second{"s" if rem != 1 else ""}_'
        )
        await message.edit(embed=embed)


@bot.command()
@commands.max_concurrency(number=1)
async def smitetrivia(message, *args):
    if message.author == bot.user:
        return

    items = await load_items()

    question_mapping = {
        "items": {
            "values": filter(lambda item: item.type == ItemType.ITEM, items),
            "questions": items_questions,
        },
        "consumables": {
            "values": filter(lambda item: item.type == ItemType.CONSUMABLE, items),
            "questions": consumables_questions,
        },
        "relics": {
            "values": filter(lambda item: item.type == ItemType.RELIC, items),
            "questions": relics_questions,
        },
    }

    question_count = 1
    input_category = None
    correct_answers = {}
    was_stopped = False
    asked_questions = set()

    if args is not None:
        if len(args) > 2:
            await message.channel.send(
                embed=discord.Embed(
                    color=discord.Color.red(),
                    description="Invalid command! This bot accepts the command `$smitetrivia` (or `$st`) with optional count and category arguments, e.g. `$smitetrivia 10 items`",
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
            except ValueError:
                await message.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.red(),
                        description="Question count must be a number.",
                    )
                )
                return

            if args[1] not in question_mapping.keys():
                await message.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.red(),
                        description=f"'{args[1]}' is not a valid question category.",
                    )
                )
                return
            input_category = args[1]
        elif len(args) == 1:
            try:
                question_count = int(args[0])
            except TypeError:
                await message.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.red(),
                        description="Question count must be a number.",
                    )
                )
                return
    for q in range(question_count):
        question = None
        category = input_category

        if category is None:
            category = random.choices(
                list(question_mapping.keys()), weights=[10, 1, 2]
            )[0]

        question_pool = question_mapping[category]["questions"]
        input_objects = question_mapping[category]["values"]
        input_object = random.choice(list(input_objects.values()))

        while question is None or question["id"] in asked_questions:
            question = random.choice(question_pool)(input_object)

        asked_questions.add(question["id"])

        question["question"].title = (
            f"❔ _Question **{q+1}** of **{question_count}**_"
            if question_count > 1
            else "❔ _Question_"
        )
        question["question"].color = discord.Color.blue()
        question["question"].add_field(name="Time Remaining:", value="_20 seconds_")
        answers = {}

        def check(m):
            correct = False
            if m.author == bot.user:
                return False

            if m.content.startswith("$stop"):
                loop = asyncio.get_running_loop()
                loop.create_task(
                    message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.red(),
                            description="Trivia round canceled!",
                        )
                    )
                )
                raise StoppedError

            if m.author not in answers.keys():
                answers[m.author] = {"answered": 1, "warned": False}
            else:
                answers[m.author]["answered"] += 1

            answer = str(question["answer"]).lower().replace("-", " ")
            correct = answer == unidecode(m.content).lower().replace("-", " ")

            if not correct and not answer.replace("%", "").isdigit():
                if answer.startswith("the") and not m.content.lower().startswith("the"):
                    answer = answer.replace("the ", "")

                correct = editdistance.eval(answer, m.content.lower()) <= 2
            elif (
                not correct
                and answer.replace("%", "").isdigit()
                and m.content.replace("%", "").isdigit()
                and answers[m.author]["answered"] < 2
            ):
                guess = int(m.content.replace("%", ""))
                answer_number = int(answer.replace("%", ""))
                loop = asyncio.get_running_loop()

                if guess < answer_number:
                    loop.create_task(
                        message.channel.send(
                            embed=discord.Embed(
                                color=discord.Color.blue(),
                                description=f"Not quite, {m.author.mention}, try a higher guess. ↗️",
                            )
                        )
                    )
                else:
                    loop.create_task(
                        message.channel.send(
                            embed=discord.Embed(
                                color=discord.Color.blue(),
                                description=f"Not quite, {m.author.mention}, try a lower guess. ↘️",
                            )
                        )
                    )

            if correct and answers[m.author]["answered"] <= 2:
                return correct

            if answers[m.author]["answered"] >= 2 and not answers[m.author]["warned"]:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    message.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.red(),
                            description=f"{m.author.mention}, you've reached your maximum number of guesses. <:noshot:782396496104128573> Try again next question!",
                        )
                    )
                )
                answers[m.author]["warned"] = True
                return False

        exp = time.time() + 20
        task = asyncio.get_running_loop().create_task(
            countdown_loop(
                await message.channel.send(embed=question["question"]),
                exp,
                question["question"],
            )
        )
        try:
            msg = await bot.wait_for("message", check=check, timeout=20)
            answer_time = time.time() - (exp - 20)
            task.cancel()
            description = f'✅ Correct, **{msg.author.display_name}**! You got it in {round(answer_time)} seconds. The answer was **{question["answer"]}**. <:frogchamp:566686914858713108>'
            if q < question_count - 1:
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
            if q < question_count - 1:
                await asyncio.sleep(5)
        except asyncio.TimeoutError:
            description = f'❌⏲️ Time\'s up! The answer was **{question["answer"]}**. <:killmyself:472184572407447573>'
            if q < question_count - 1:
                description += "\n\nNext question coming up in 5 seconds."

            await message.channel.send(
                embed=discord.Embed(color=discord.Color.red(), description=description)
            )
            if q < question_count - 1:
                await asyncio.sleep(5)
        except StoppedError:
            was_stopped = True
            task.cancel()
            break

    if not was_stopped and bool(correct_answers):
        description = [
            f'**{idx + 1}**. _{(await bot.fetch_user(u[0])).display_name}_ (Score: **{u[1]}**) {"<:mleh:472905075208093717>" if idx == 0 else ""}'
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
        with open("scores.json", "r") as f:
            try:
                current_scores = json.load(f)
            except JSONDecodeError:
                pass
        if current_scores:
            for u in correct_answers.keys():
                if str(u) not in current_scores:
                    current_scores[str(u)] = correct_answers[u]
                else:
                    current_scores[str(u)] += correct_answers[u]
        else:
            current_scores = correct_answers

        with open("scores.json", "w") as f:
            json.dump(current_scores, f)


@bot.command()
async def st(message, *args):
    await smitetrivia(message, *args)


@bot.command()
async def scores(ctx):
    with open("scores.json", "r") as f:
        try:
            current_scores = json.load(f)
            current_scores = sorted(
                current_scores.items(), key=lambda i: i[1], reverse=True
            )
            description = [
                f'**{idx + 1}**. _{(await bot.fetch_user(u[0])).display_name}_ (Score: **{u[1]}**) {"<:mleh:472905075208093717>" if idx == 0 else ""}'
                for idx, u in enumerate(current_scores)
            ]
            embed = discord.Embed(
                color=discord.Color.blue(),
                title="**Leaderboard:**",
                description=str.join("\n", description),
            ).set_thumbnail(url=(await bot.fetch_user(current_scores[0][0])).avatar_url)
            await ctx.channel.send(embed=embed)
        except JSONDecodeError:
            await ctx.channel.send(
                embed=discord.Embed(
                    color=discord.Color.blue(), title="No scores recorded yet!"
                )
            )


bot.run(config["discordToken"])
