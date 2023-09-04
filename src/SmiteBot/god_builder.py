from __future__ import annotations
import math
import random
import sys
from enum import Enum
from typing import Dict, List, Tuple

import pandas as pd

from build_optimizer import BuildOptimizer
from god import God
from god_types import GodId, GodRole, GodType
from item import Item, ItemAttribute, ItemType
from player_stats import PlayerStats
from player import Player
from stat_calculator import DamageCalculator, GodBuild
from SmiteProvider import SmiteProvider
from HirezAPI import PlayerRole, QueueId, TierId


class InvalidOptionError(Exception):
    pass


class BuildFailedError(Exception):
    pass


class BuildCommandType(Enum):
    OPTIMIZE = "optimize"
    RANDOM = "random"
    TOP = "top"
    ML = "ml"


class BuildPrioritization(Enum):
    POWER = "power"
    DEFENSE = "defense"


class BuildOptions:
    build_type: BuildCommandType
    god_id: GodId
    prioritization: BuildPrioritization
    queue_id: QueueId
    role: PlayerRole
    stat: ItemAttribute
    allies: List[GodId]
    enemies: List[GodId]
    __random_god: bool = False

    def __init__(
        self,
        god_id: GodId = None,
        build_type: BuildCommandType = BuildCommandType.RANDOM,
        prioritization: BuildPrioritization = None,
        queue_id: QueueId = None,
        role: PlayerRole = None,
        stat: ItemAttribute = None,
        enemies: List[GodId] = None,
    ):
        if god_id is not None:
            self.god_id = god_id
        else:
            self.god_id = random.choice(list(GodId))
            self.__random_god = True
        self.build_type = build_type
        self.prioritization = prioritization
        self.queue_id = queue_id
        self.role = role
        self.stat = stat
        self.enemies = enemies

    def set_option(self, option: str, value: str):
        if option in ("-g", "--god"):
            self.god_id = GodId[
                value.upper().replace(" ", "_").replace("'", "")
            ]  # handles Chang'e case
            self.__random_god = False
        elif option in ("-p", "--prioritize"):
            self.prioritization = BuildPrioritization(value.lower())
        elif option in ("-q", "--queue"):
            self.queue_id = QueueId[value.upper().replace(" ", "_")]
        elif option in ("-r", "--role"):
            self.role = PlayerRole(value.lower())
        elif option in ("-s", "--stat"):
            self.stat = ItemAttribute(value.lower())
        elif option in ("-t", "--type"):
            self.build_type = BuildCommandType(value.lower())
        elif option in ("-e", "--enemies"):
            self.enemies = [
                GodId[g.strip().upper().replace(" ", "_").replace("'", "")]
                for g in value.split(",")
            ]
        else:
            raise InvalidOptionError

    def validate(self) -> str | None:
        if (
            self.build_type != BuildCommandType.RANDOM
            and self.prioritization is not None
        ):
            return "The prioritize option can only be used with the random build type."
        if (
            self.build_type not in (BuildCommandType.TOP, BuildCommandType.ML)
            and self.role is not None
        ):
            return "The role option can only be used with the top or ML build types."
        if self.role is not None and self.queue_id is not None:
            if self.queue_id not in (
                QueueId.CONQUEST,
                QueueId.CUSTOM_CONQUEST,
                QueueId.RANKED_CONQUEST,
                QueueId.RANKED_CONQUEST_CONTROLLER,
            ):
                return (
                    "Cannot specify both role and queue for a non-Conquest game mode!"
                )
        if self.stat is not None and self.build_type in (
            BuildCommandType.TOP,
            BuildCommandType.ML,
        ):
            return (
                "Cannot prioritize a specific stat when pulling "
                "a top player's build or querying match data."
            )
        if (
            self.queue_id is not None
            and self.queue_id != QueueId.RANKED_CONQUEST
            and self.build_type == BuildCommandType.ML
        ):
            return "ML mode only supports Ranked Conquest."
        return None

    def was_random_god(self) -> bool:
        return self.__random_god


class GodBuilder:
    __gods: Dict[GodId, God]
    __items: Dict[int, Item]
    __player_matches: pd.DataFrame
    __provider: SmiteProvider

    def __init__(
        self,
        gods: Dict[GodId, God],
        items: Dict[int, Item],
        player_matches: pd.DataFrame,
        provider: SmiteProvider,
    ):
        self.__gods = gods
        self.__items = items
        self.__player_matches = player_matches
        self.__provider = provider

    def get_valid_items_for_god(self, god: God) -> List[Item]:
        return list(
            filter(
                lambda item: item.type == ItemType.ITEM and item.active and
                # Filter out acorns from non-Ratatoskr gods
                (item.root_item_id != 18703 or god.id == GodId.RATATOSKR) and
                # Filter out Odysseus' Bow from non-physical gods
                (item.id != 10482 or god.type == GodType.PHYSICAL) and
                # Filter out any items that have restricted roles that intersect
                # with the current god's role
                (
                    not any(item.restricted_roles)
                    or god.role not in item.restricted_roles
                )
                and
                # Elucidate god type from item properties and check intersection
                (
                    any(p.attribute.god_type == god.type for p in item.item_properties)
                    or all(p.attribute.god_type is None for p in item.item_properties)
                ),
                self.__items.values(),
            )
        )

    def random(self, build_options: BuildOptions) -> Tuple[List[Item], str]:
        god = self.__gods[build_options.god_id]
        items_for_god = self.get_valid_items_for_god(god)
        optimizer = BuildOptimizer(god, items_for_god, self.__items)

        if build_options.queue_id is not None:
            items_for_god = optimizer.filter_queue_items(
                items_for_god, build_options.queue_id
            )
        build = []

        unfiltered_items = items_for_god  # Needed for Ratatoskr
        if build_options.prioritization is not None:
            items_for_god = optimizer.filter_prioritize(
                items_for_god, build_options.prioritization.value
            )
        if build_options.stat is not None:
            items_for_god = optimizer.filter_by_stat(items_for_god, build_options.stat)
        # Filter to just tier 3 items
        items = optimizer.filter_evolution_parents(
            optimizer.filter_acorns(optimizer.filter_tiers(items_for_god))
        )

        # Ratatoskr always has his acorn!
        should_include_starter = int(
            not QueueId.is_duel(build_options.queue_id) and bool(random.randint(0, 1))
        )
        should_include_glyph = bool(random.randint(0, 1))
        is_ratatoskr = god.id == GodId.RATATOSKR
        build_size = 6 - should_include_starter - int(is_ratatoskr)

        glyphs = optimizer.get_glyphs(items_for_god)
        starters = optimizer.get_starters(items_for_god)

        if bool(should_include_starter) and not any(starters):
            build_size += 1
            should_include_starter = 0

        # Add a glyph... maybe!!
        if should_include_glyph and any(glyphs):
            glyph = random.choice(glyphs)
            build.append(glyph)
            build_size = build_size - 1
            items = optimizer.filter_glyph_parent(items, glyph)

        if len(items) < build_size:
            raise BuildFailedError

        # Add build_size random items from our tier 3 items, then shuffle the build order
        build.extend(random.sample(items, build_size))
        random.shuffle(build)

        # Special casing Ratatoskr's acorn. Gotta have it!
        if is_ratatoskr:
            acorns = optimizer.filter_tiers(
                optimizer.get_ratatoskr_acorn(unfiltered_items)
            )
            build.insert(0, random.choice(acorns))

        # Adding a starter to the beginning of the build if random demands it
        if bool(should_include_starter):
            build.insert(0 + int(is_ratatoskr), random.choice(starters))

        # If we decided to not have a random glyph, but still included a
        # direct parent of a glyph... upgrade it anyway! Random!
        if not should_include_glyph:
            parent_idx, glyph = optimizer.get_glyph_parent_if_no_glyphs(build)
            if glyph is not None:
                build[parent_idx] = glyph

        prioritize_str = (
            f", with only {build_options.prioritization.value} items"
            if build_options.prioritization is not None
            else ""
        )
        desc = (
            f"here's your random build{prioritize_str}!\n\n"
            f"{optimizer.get_build_stats_string(build)}"
        )

        return (build, desc)

    def ml(self, build_options: BuildOptions) -> Tuple[List[Item], str]:
        pm = self.__player_matches

        god_matches = (
            pm.loc[pm["GodId"] == build_options.god_id.value]
            if not build_options.was_random_god()
            else pm
        )

        winner_matches = god_matches.loc[god_matches["Win_Status"] == "Winner"]

        if build_options.role is not None:
            god_matches = god_matches.loc[
                god_matches["Role"] == build_options.role.value.capitalize()
            ]

            winner_matches = winner_matches.loc[
                winner_matches["Role"] == build_options.role.value.capitalize()
            ]

        found_team_match: bool = False
        found_role_match: bool = False
        found_type_match: bool = False

        if build_options.enemies is not None:
            enemy_str = ",".join(
                sorted([str(id.value) for id in build_options.enemies])
            )

            team_role_matches = god_matches.loc[god_matches["EnemyGodIds"] == enemy_str]

            team_winner_matches = winner_matches.loc[
                winner_matches["EnemyGodIds"] == enemy_str
            ]

            if team_winner_matches.shape[0] > 0:
                found_team_match = True
                god_matches = team_role_matches
                winner_matches = team_winner_matches

            if not found_team_match:
                role_str = ",".join(
                    sorted(self.__gods[g].role.value[0] for g in build_options.enemies)
                )

                team_role_matches = god_matches.loc[
                    god_matches["EnemyGodRoles"] == role_str
                ]

                team_winner_role_matches = winner_matches.loc[
                    winner_matches["EnemyGodRoles"] == role_str
                ]

                if team_winner_role_matches.shape[0] > 0:
                    found_role_match = True
                    god_matches = team_role_matches
                    winner_matches = team_winner_role_matches

            if not found_team_match and not found_role_match:
                type_str = ",".join(
                    sorted(self.__gods[g].type.value[0] for g in build_options.enemies)
                )

                team_type_matches = god_matches.loc[
                    god_matches["EnemyGodTypes"] == type_str
                ]

                team_winner_type_matches = winner_matches.loc[
                    winner_matches["EnemyGodTypes"] == type_str
                ]

                if team_winner_type_matches.shape[0] > 0:
                    found_type_match = True
                    god_matches = team_type_matches
                    winner_matches = team_winner_type_matches

        group_by = [
            "ItemId1",
            "ItemId2",
            "ItemId3",
            "ItemId4",
            "ItemId5",
            "ItemId6",
        ]

        if build_options.was_random_god():
            group_by.insert(0, "GodId")

        build_matches = winner_matches.loc[
            (winner_matches["ItemId1"] != 0)
            & (winner_matches["ItemId2"] != 0)
            & (winner_matches["ItemId3"] != 0)
            & (winner_matches["ItemId4"] != 0)
            & (winner_matches["ItemId5"] != 0)
            & (winner_matches["ItemId6"] != 0)
        ]

        if build_matches.shape[0] == 0:
            raise BuildFailedError

        best_build = build_matches.groupby(group_by).size().idxmax()

        build = []

        for idx, i in enumerate(best_build):
            if i == 0 or (idx == 0 and build_options.was_random_god()):
                continue
            build.append(self.__items[int(i)])

        if build_options.was_random_god():
            build_options.god_id = GodId(int(best_build[0]))

        god = self.__gods[build_options.god_id]
        items_for_god = self.get_valid_items_for_god(god)
        optimizer = BuildOptimizer(god, items_for_god, self.__items)
        win_count = winner_matches.shape[0]

        common_roles = (
            pm.loc[pm["GodId"] == build_options.god_id.value]["Role"].mode().values
        )

        common_role_str = (
            f" {god.name}'s most common role is **{common_roles[0]}**."
            if len(common_roles) > 0
            else ""
        )

        role_str = (
            f"**{build_options.role.value.capitalize()}** "
            if build_options.role is not None
            else ""
        )

        median_mmr = winner_matches["Rank_Stat_Conquest"].median()
        median_tier = winner_matches["Conquest_Tier"].median()

        mmr_str = (
            f"{'These winners have' if win_count > 1 else 'This winner has'} a "
            f"{'median ' if win_count > 1 else ''}rank of "
            f"**{PlayerStats.get_tier_string(TierId(math.floor(median_tier)), median_mmr)}**."
        )

        win_rate = float(winner_matches.shape[0]) / god_matches.shape[0]

        team_match_str = ""
        against_team_str = ""

        if build_options.enemies is not None:
            if found_team_match:
                team_match_str = (
                    "*I was able to find the exact team composition you requested!*"
                )
                against_team_str = " against that exact team"
            elif found_role_match:
                team_match_str = (
                    "*I couldn't find that exact team*, but I found "
                    f"{'some' if winner_matches.shape[0] > 1 else 'one'} "
                    f"that matched their God roles."
                )
                against_team_str = " against teams matching those roles"
            elif found_type_match:
                team_match_str = (
                    f"*I couldn't find that exact team*, but I found "
                    f"{'some' if winner_matches.shape[0] > 1 else 'one'} "
                    f"that matched their damage types."
                )
                against_team_str = " against teams matching those damage types"
            else:
                team_match_str = (
                    "I couldn't find a team matching your request, "
                    "so I fetched overall stats."
                )
            team_match_str = f"{team_match_str}\n\n"

        god_name_str = f" {god.name}" if not build_options.was_random_god() else ""

        desc = (
            f"here's your {role_str}build, generated from **{win_count:,}**"
            f" {'different ' if win_count > 1 else ''} winning{god_name_str} "
            f"{role_str}build{'s' if win_count > 1 else ''} in Ranked Conquest. "
            f"{mmr_str}\n\n{common_role_str} "
            f"Their {'overall ' if build_options.role is None else role_str}"
            f"win percentage is **{(win_rate*100):,.2f}%**{against_team_str}.\n\n"
            f"{team_match_str}"
            f"{optimizer.get_build_stats_string(build)}"
        )

        return (build, desc)

    async def top(self, build_options: BuildOptions) -> Tuple[List[Item], str]:
        god = self.__gods[build_options.god_id]
        items_for_god = self.get_valid_items_for_god(god)
        optimizer = BuildOptimizer(god, items_for_god, self.__items)
        role = build_options.role
        build = []
        leaderboard_queue = QueueId.RANKED_CONQUEST
        if build_options.queue_id is not None and QueueId.is_ranked(
            build_options.queue_id
        ):
            leaderboard_queue = build_options.queue_id
        god_leaderboard = await self.__provider.get_god_leaderboard(
            god.id, leaderboard_queue
        )

        build_match = None
        match_player_id = None
        while len(build) == 0:
            try:
                # Fetching a random player from the leaderboard
                random_player = random.choice(god_leaderboard)
                god_leaderboard.remove(random_player)
            except IndexError as exc:
                raise BuildFailedError from exc

            # Scraping their recent match history to try and find a current build
            match_history = await self.__provider.get_match_history(
                int(random_player["player_id"])
            )
            for match in match_history:
                if len(build) != 0:
                    break
                if role is not None:
                    match_role = match["Role"]
                    if match_role is not None and match_role.lower() != role.value:
                        continue
                if build_options.queue_id is not None:
                    if int(match["Match_Queue_Id"]) != build_options.queue_id.value:
                        continue
                build_match = match
                match_player_id = int(match["playerId"])
                # Get a full build for this god
                if int(match["GodId"]) == god.id.value and int(match["ItemId6"]) != 0:
                    for i in range(1, 7):
                        # Luckily `getmatchhistory` includes build info!
                        item_id = int(match[f"ItemId{i}"])
                        if item_id == 0:
                            break
                        item = self.__items[item_id]
                        if item.tier < 3 and (
                            item.parent_item_id is None
                            or not self.__items[item.parent_item_id].is_starter
                        ):
                            build = []
                            break
                        build.append(self.__items[item_id])
        playing_str = (
            f'playing {QueueId(int(build_match["Match_Queue_Id"])).display_name}'
        )
        if role is not None and build_options.queue_id is not None:
            playing_str = (
                f"playing {role.value.title()} in "
                f"{build_options.queue_id.display_name}"
            )
        elif role is not None:
            playing_str = f"playing {role.value.title()}"
        elif build_options.queue_id is not None:
            playing_str = f"playing {build_options.queue_id.display_name}"
        if QueueId.is_duel(build_options.queue_id):
            match_details = await self.__provider.get_match_details(
                int(build_match["Match"])
            )
            enemy_player_god = None
            for detail in match_details:
                if int(detail["playerId"]) != match_player_id:
                    enemy_player_god = self.__gods[GodId(int(detail["GodId"]))]
                    break
            if enemy_player_god is not None:
                playing_str += f" against {enemy_player_god.name}"
        rank_str = ""
        if QueueId.is_ranked(leaderboard_queue):
            players = await self.__provider.get_player(match_player_id)
            if any(players):
                player = Player.from_json(players[0])
                if leaderboard_queue in player.ranked_stats:
                    rank_stat = player.ranked_stats[leaderboard_queue]
                    rank_str = (
                        ", who has a rank of "
                        f"{PlayerStats.get_tier_string(rank_stat.tier, rank_stat.mmr)}"
                    )

        desc = (
            f"here's your build, "
            f'courtesy of #{random_player["rank"]} {god.name} '
            f'**{random_player["player_name"]}**{rank_str}! '
            f'{"They won!" if build_match["Win_Status"] == "Win" else "They lost..."}\n\n'
            f"They were {playing_str} "
            f'and they went {build_match["Kills"]}/'
            f'{build_match["Deaths"]}/{build_match["Assists"]}!\n\n'
            f"{optimizer.get_build_stats_string(build)}"
        )

        return (build, desc)

    async def optimize(self, build_options: BuildOptions) -> Tuple[List[Item], str]:
        god = self.__gods[build_options.god_id]
        items_for_god = self.get_valid_items_for_god(god)
        optimizer = BuildOptimizer(god, items_for_god, self.__items)
        builds, iterations = await optimizer.optimize()

        if not any(builds):
            raise BuildFailedError

        min_ttk = sys.maxsize
        build: List[Item] = None

        team_killed_str = ""
        if god.role == GodRole.HUNTER:
            random_assassin = await self._get_random_god_by_role(
                GodRole.ASSASSIN, build_options.queue_id
            )
            random_guardian = await self._get_random_god_by_role(
                GodRole.GUARDIAN, build_options.queue_id
            )
            random_hunter = await self._get_random_god_by_role(
                GodRole.HUNTER, build_options.queue_id
            )
            random_mage = await self._get_random_god_by_role(
                GodRole.MAGE, build_options.queue_id
            )
            random_warrior = await self._get_random_god_by_role(
                GodRole.WARRIOR, build_options.queue_id
            )
            for bld in builds:
                total_ttk = (
                    self._get_basic_attack_ttk(build_options, bld, random_assassin)
                    + self._get_basic_attack_ttk(build_options, bld, random_guardian)
                    + self._get_basic_attack_ttk(build_options, bld, random_hunter)
                    + self._get_basic_attack_ttk(build_options, bld, random_mage)
                    + self._get_basic_attack_ttk(build_options, bld, random_warrior)
                )
                if total_ttk < min_ttk:
                    min_ttk = total_ttk
                    build = bld
            team_killed_str = (
                f"This build was tested against {random_assassin.god.name}, "
                f"{random_guardian.god.name}, {random_hunter.god.name}, "
                f"{random_mage.god.name}, and {random_warrior.god.name}. "
            )
        else:
            build = random.choice(builds)

        ttk_str = ""
        if min_ttk < sys.maxsize:
            ttk_str = (
                f"This build had a team total TTK "
                f"(time to kill) of **{min_ttk:.2f} seconds**, "
                f"beating **{len(builds):,}** other builds."
            )
        viable_str = (
            f"I tried **{iterations:,}** builds and found "
            f"**{len(builds):,}** viable builds."
        )

        desc = (
            f"here's your number crunched build! "
            f'{ttk_str if ttk_str != "" else viable_str} '
            f"{team_killed_str}"
            f"Hopefully it's a winner!\n\n"
            f"{optimizer.get_build_stats_string(build)}"
        )

        return (build, desc)

    async def _get_random_god_by_role(
        self, role: GodRole, queue_id: QueueId
    ) -> GodBuild:
        god = random.choice(
            list(
                filter(
                    lambda g: g.role == role and not g.latest_god, self.__gods.values()
                )
            )
        )
        print(f"Finding build for {god.name}...")
        build, _ = await self.top(BuildOptions(god.id, queue_id=queue_id))
        return GodBuild(god, build, 20)

    def _get_basic_attack_ttk(
        self, build_options: BuildOptions, build: List[Item], against_god: GodBuild
    ) -> float:
        calc = DamageCalculator()

        return calc.calculate_basic_ttk(
            GodBuild(self.__gods[build_options.god_id], build, 20), against_god
        )
