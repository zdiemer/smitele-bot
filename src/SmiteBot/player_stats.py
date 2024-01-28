import datetime
from itertools import groupby
from typing import Any, Dict, List

import discord
from discord.ext import commands

from god import GodId, GodRole
from player import Player, PlayerId, StatusId
from SmiteProvider import SmiteProvider
from HirezAPI import HIREZ_DATE_FORMAT, PortalId, QueueId, TierId


class PlayerPrivacyError(Exception):
    pass


class QueueStats:
    total_kills: int
    total_assists: int
    total_deaths: int
    total_gold: int
    total_wins: int
    total_losses: int
    total_minutes: int
    last_played: datetime
    best_god: GodId
    best_god_win_percent: float
    best_god_matches: int
    worst_god: GodId
    worst_god_win_percent: float
    worst_god_matches: int

    def __init__(self):
        self.total_kills = 0
        self.total_assists = 0
        self.total_deaths = 0
        self.total_gold = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_minutes = 0
        self.last_played = datetime.datetime.min
        self.best_god_win_percent = -1
        self.best_god_matches = 0
        self.best_god = None
        self.worst_god_win_percent = 2
        self.worst_god_matches = 0
        self.worst_god = None

    @staticmethod
    def from_json(value):
        queue_stats = QueueStats()

        for god in value:
            god_wins = int(god["Wins"])
            god_losses = int(god["Losses"])
            god_matches = god_wins + god_losses
            queue_stats.total_kills += int(god["Kills"])
            queue_stats.total_assists += int(god["Assists"])
            queue_stats.total_deaths += int(god["Deaths"])
            queue_stats.total_gold += int(god["Gold"])
            queue_stats.total_wins += god_wins
            queue_stats.total_losses += god_losses
            queue_stats.total_minutes += int(god["Minutes"])
            last_played_str = god["LastPlayed"]
            if last_played_str != "":
                god_last_played = datetime.datetime.strptime(
                    god["LastPlayed"], HIREZ_DATE_FORMAT
                )
                queue_stats.last_played = max(god_last_played, queue_stats.last_played)

            if god_matches >= 10:
                god_win_percent = god_wins / god_matches
                if queue_stats.best_god_win_percent < god_win_percent or (
                    queue_stats.best_god_win_percent == god_win_percent
                    and queue_stats.best_god_matches < god_matches
                ):
                    queue_stats.best_god_win_percent = god_win_percent
                    queue_stats.best_god = GodId(int(god["GodId"]))
                    queue_stats.best_god_matches = god_matches
                elif queue_stats.worst_god_win_percent > god_win_percent or (
                    queue_stats.worst_god_win_percent == god_win_percent
                    and queue_stats.worst_god_matches < god_matches
                ):
                    queue_stats.worst_god_win_percent = god_win_percent
                    queue_stats.worst_god = GodId(int(god["GodId"]))
                    queue_stats.worst_god_matches = god_matches

        return queue_stats

    @property
    def matches(self) -> int:
        return self.total_wins + self.total_losses

    @property
    def total_avg_kda(self) -> float:
        return (self.total_kills + (self.total_assists / 2)) / (
            self.total_deaths if self.total_deaths > 0 else 1
        )

    @property
    def win_percent(self) -> float:
        return self.total_wins / (self.matches if self.matches > 0 else 1)


class PlayerStats(commands.Cog):
    __provider: SmiteProvider

    __user_id_to_smite_username: Dict[int, str] = {
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
        254016582244630540: "PlŠŠŠŠTwink",
        267050303902187520: "mehtev4s",
        250146567011434506: "doyleville",
    }

    def __init__(self, provider: SmiteProvider):
        self.__provider = provider

    async def __send_invalid(
        self,
        ctx_or_message: discord.ApplicationContext | discord.Message,
        base: str = "",
        error_info: str = "Invalid command!",
        include_command_info: bool = False,
    ):
        desc = f"{error_info}"
        if include_command_info:
            desc += base
        await self.__send_response_or_message_embed(
            ctx_or_message,
            embed=discord.Embed(
                color=discord.Color.red(),
                description=desc,
            ),
        )

    async def __get_non_pc_player_ids(self, gamertag: str) -> list:
        for portal_id in list(PortalId):
            player_ids = await self.__provider.get_player_ids_by_gamer_tag(
                portal_id, gamertag
            )
            if not any(player_ids):
                continue
            return player_ids
        return []

    async def __get_player(
        self,
        username: str,
        ctx_or_message: discord.ApplicationContext | discord.Message,
    ) -> Player | None:
        player_ids = await self.__provider.get_player_id_by_name(username)
        if not any(player_ids):
            await self.__send_response_or_message_embed(
                ctx_or_message,
                discord.Embed(
                    color=discord.Color.yellow(),
                    description="Couldn't find a PC player of that name, checking consoles...",
                ),
            )
            player_ids = await self.__get_non_pc_player_ids(username)
            if not any(player_ids):
                return None
        player_id_info = PlayerId.from_json(player_ids[0], self.__provider)
        if player_id_info.private:
            raise PlayerPrivacyError
        player = await player_id_info.get_player()
        if player is not None and player.active_player_id != player.id:
            player = await player_id_info.get_player(
                id_override=player.active_player_id
            )
        if player is None:
            return None
        return player

    async def __get_player_or_return_invalid(
        self,
        username: str,
        ctx_or_message: discord.ApplicationContext | discord.Message,
    ) -> Player | None:
        player: Player | None = None
        try:
            player = await self.__get_player(username, ctx_or_message)
            if player is None:
                await self.__send_invalid(
                    ctx_or_message,
                    error_info="No players with that name found!",
                )
                return None
        except PlayerPrivacyError:
            await self.__send_invalid(
                ctx_or_message,
                error_info=f"{username} has their profile hidden... "
                "<:reeratbig:849771936509722634>",
            )
            return None
        return player

    @staticmethod
    def get_tier_string(tier_id: TierId, mmr: float) -> str:
        emoji = (
            "🥉"
            if tier_id.value <= 5
            else "🥈"
            if tier_id.value <= 10
            else "🥇"
            if tier_id.value <= 15
            else "🏅"
            if tier_id.value <= 20
            else "💎"
            if tier_id.value <= 25
            else "🏆"
            if tier_id.value == 26
            else "💯"
        )
        return f"{emoji} **{tier_id.display_name}** ({int(round(mmr))} MMR)"

    async def __send_response_or_message_embed(
        self,
        ctx_or_message: discord.ApplicationContext | discord.Message,
        embed: discord.Embed,
    ):
        if isinstance(ctx_or_message, discord.ApplicationContext):
            await ctx_or_message.respond(embed=embed, ephemeral=True)
        else:
            await ctx_or_message.channel.send(embed=embed)

    async def __livematch_lookup(
        self,
        player: Player,
        ctx_or_message: discord.ApplicationContext | discord.Message,
    ):
        if player is None:
            return
        try:
            player_status = await player.get_player_status()
        except (KeyError, ValueError) as ex:
            print(f"Unsupported queue type: {ex}")
            await self.__send_invalid(
                ctx_or_message,
                error_info="Unfortunately, the match type this player is playing is not currently supported.",
            )
            return
        invalid_msg = ""
        if (
            player_status is None
            or player_status.status == StatusId.UNKNOWN
            or (
                player_status.status == StatusId.IN_GAME
                and player_status.match_id is None
            )
        ):
            invalid_msg = f"You must've broken something. I can't tell what {player.name} is doing right now."
        elif player_status.status == StatusId.OFFLINE:
            invalid_msg = f"{player.name} is currently offline."
        elif player_status.status in (StatusId.IN_LOBBY, StatusId.ONLINE):
            invalid_msg = f"{player.name} is online, but not currently in a game."
        elif player_status.status == StatusId.GOD_SELECTION:
            invalid_msg = f"{player.name} is in god select, try again shortly to get live match details!"
        if invalid_msg != "":
            await self.__send_invalid(ctx_or_message, error_info=invalid_msg)
            return
        live_match = await self.__provider.get_match_player_details(
            player_status.match_id
        )

        teams: Dict[int, List[Any]] = {}
        for p in live_match:
            team = int(p["taskForce"])
            if team in teams:
                teams[team].append(p)
            else:
                teams[team] = [p]

        def create_team_output(team_list: list) -> str:
            output = ""
            for member in team_list:
                member_info = ""
                if QueueId.is_ranked(player_status.queue_id):
                    member_info += f' - {self.get_tier_string(TierId(int(member["Tier"])), float(member["Rank_Stat"]))}'
                else:
                    member_info += f' - Level {member["Account_Level"]} (God Mastery {member["GodLevel"]})'
                player_name = member["playerName"]
                if player_name == "":
                    player_name = "Hidden Player"
                output += f'• **{player_name}** ({member["GodName"]}){member_info}\n'
            return output

        players_embed = discord.Embed(
            color=discord.Color.blue(),
            title=f"{player.name}'s Live "
            f"{player_status.queue_id.display_name} Details",
        )

        if len(teams) == 2:
            players_embed.add_field(
                name="🔵 Order Side", value=create_team_output(teams[1])
            )
            players_embed.add_field(
                name="🔴 Chaos Side", value=create_team_output(teams[2])
            )
        else:
            for team_id, players in sorted(teams.items(), key=lambda t: t[0]):
                players_embed.add_field(
                    name=f"Team {team_id}", value=create_team_output(players)
                )

        await self.__send_response_or_message_embed(ctx_or_message, embed=players_embed)

    @commands.user_command(
        name="Smite Live Match",
        guild_ids=[396874836250722316, 845718807509991445],
    )
    async def livematch_lookup(
        self, ctx: discord.ApplicationContext, member: discord.Member
    ):
        if member.id not in self.__user_id_to_smite_username:
            await self.__send_invalid(
                ctx,
                error_info="Unable to find that player.",
            )
            return

        player = await self.__get_player_or_return_invalid(
            self.__user_id_to_smite_username[member.id], ctx
        )
        await self.__livematch_lookup(
            player,
            ctx,
        )

    @commands.user_command(
        name="Smite Queue Stats",
        guild_ids=[396874836250722316, 845718807509991445],
    )
    async def queue_stats_lookup(
        self, ctx: discord.ApplicationContext, member: discord.Member
    ):
        if member.id not in self.__user_id_to_smite_username:
            await self.__send_invalid(
                ctx,
                error_info="Unable to find that player.",
            )
            return

        player = await self.__get_player_or_return_invalid(
            self.__user_id_to_smite_username[member.id], ctx
        )

        stats_embed = discord.Embed(
            color=discord.Color.blue(),
            title=f"{player.name}'s Overall Stats",
        )
        stats_embed.set_thumbnail(url=player.avatar_url)

        await self.__queue_stats_lookup(ctx, player, stats_embed)

    @commands.slash_command(
        name="live_match",
        description="Look up a Smite player's live match details",
        guild_ids=[845718807509991445, 396874836250722316],
    )
    @discord.option(
        name="player_name",
        type=str,
        description="The player name of the person to look up",
        required=True,
    )
    async def livematch(self, ctx: discord.ApplicationContext, player_name: str):
        if not any(player_name):
            await self.__send_invalid(
                ctx,
                error_info="Player name cannot be empty",
            )
            return

        player = await self.__get_player_or_return_invalid(player_name, ctx)
        await self.__livematch_lookup(player, ctx)

    @commands.slash_command(
        name="queue_stats",
        description="Look up a Smite player's stats for a given queue type",
        guild_ids=[845718807509991445, 396874836250722316],
    )
    @discord.option(
        name="player_name",
        type=str,
        description="The player name of the person to look up",
        required=True,
    )
    @discord.option(
        name="queue",
        type=str,
        description="The queue to get stats for",
        choices=[
            q.display_name
            for q in list(
                filter(
                    lambda _q: QueueId.is_normal(_q) or QueueId.is_ranked(_q),
                    list(QueueId),
                )
            )
        ],
        default="",
    )
    async def queuestats(
        self, ctx: discord.ApplicationContext, player_name: str, queue: str
    ):
        if not any(player_name):
            await self.__send_invalid(ctx, error_info="Player name cannot be empty")
            return

        queue_id: QueueId | None = None

        if queue is not None and any(queue):
            try:
                queue_id = QueueId[queue.upper().replace(" ", "_").replace("'", "")]
            except KeyError:
                await self.__send_invalid(
                    ctx, error_info=f"{queue} is not a valid queue!"
                )
                return

        player = await self.__get_player_or_return_invalid(player_name, ctx)
        if player is None:
            return

        stats_embed = discord.Embed(
            color=discord.Color.blue(),
            title=f'{player.name}\'s {queue_id.display_name if queue_id is not None else "Overall"} Stats',
        )
        stats_embed.set_thumbnail(url=player.avatar_url)

        if queue_id is not None:
            queue_list = await self.__provider.get_queue_stats(player.id, queue_id)
            if not any(queue_list):
                await self.__send_invalid(
                    ctx,
                    error_info=f"{player.name} doesn't have any playtime for {queue_id.display_name}!",
                )
                return
            queue_stats = QueueStats.from_json(queue_list)

            total_kda = (
                f"• _Total Kills_: {queue_stats.total_kills:,}\n"
                f"• _Total Deaths_: {queue_stats.total_deaths:,}\n"
                f"• _Total Assists_: {queue_stats.total_assists:,}\n"
                f"• _Overall Avg. KDA_: {queue_stats.total_avg_kda:.2f}\n"
                f"• _Total Gold_: {queue_stats.total_gold:,}"
            )

            leave_stats = ""
            if QueueId.is_ranked(queue_id) and queue_id in player.ranked_stats:
                leave_stats = (
                    f"• _Total Disconnects_: {player.ranked_stats[queue_id].leaves}\n"
                )

            total_wlr = (
                f"• _Total Wins_: {queue_stats.total_wins:,}\n"
                f"• _Total Losses_: {queue_stats.total_losses:,}\n"
                f"{leave_stats}"
                f"• _Overall Win Rate_: {int(queue_stats.win_percent * 100)}%"
            )

            time_stats = (
                f'• _Total Time Played ({"Minutes" if queue_stats.total_minutes < 60 else "Hours"})_: '
                f"{(queue_stats.total_minutes if queue_stats.total_minutes < 60 else queue_stats.total_minutes / 60):,.2f}\n"
                f'• _Last Played_: {datetime.datetime.strftime(queue_stats.last_played, "%B %d, %Y")}'
            )

            if queue_stats.best_god is not None:
                worst_god_stats = ""
                if queue_stats.worst_god is not None:
                    worst_god_stats = (
                        f" Their worst god for {queue_id.display_name} is "
                        f"{self.__provider.gods[queue_stats.worst_god].name} with a pitiful win rate of "
                        f"{int(queue_stats.worst_god_win_percent * 100)}% ({queue_stats.worst_god_matches} matches)."
                    )
                best_god_queue_stats = (
                    f"{player.name}'s best god for {queue_id.display_name} is "
                    f"{self.__provider.gods[queue_stats.best_god].name} with a win rate of "
                    f"{int(queue_stats.best_god_win_percent * 100)}% ({queue_stats.best_god_matches} matches)."
                    f"{worst_god_stats}"
                )
                stats_embed.set_footer(text=best_god_queue_stats)

            stats_embed.add_field(name="Overall KDA", value=total_kda)
            stats_embed.add_field(name="Overall Win/Loss Ratio", value=total_wlr)
            stats_embed.add_field(name="Playtime", value=time_stats)
            await ctx.respond(embed=stats_embed, ephemeral=True)
            return

        await self.__queue_stats_lookup(ctx, player, stats_embed)

    async def __queue_stats_lookup(
        self,
        ctx: discord.ApplicationContext,
        player: Player,
        stats_embed: discord.Embed,
    ):
        total_kills = 0
        total_assists = 0
        total_deaths = 0
        total_gold = 0
        total_wins = 0
        total_losses = 0
        total_minutes = 0
        last_played = datetime.datetime.min
        best_win_percent = -1
        best_queue: str | None = None
        best_queue_matches = 0
        worst_win_percent = 2
        worst_queue: str | None = None
        worst_queue_matches = 0

        await ctx.respond(
            embed=discord.Embed(
                color=discord.Color.blue(),
                description=f"Calculating {player.name}'s overall stats across all queues. Please wait...",
            ),
            ephemeral=True,
        )
        async with ctx.channel.typing():
            all_queues = list(QueueId)
            for i in range(0, len(all_queues), 20):
                queue_list = await self.__provider.get_queue_stats_batch(
                    player.id, (str(q.value) for q in all_queues[i : i + 20])
                )

                if not any(queue_list):
                    continue

                for q, value in groupby(queue_list, key=lambda _q: _q["Queue"]):
                    queue_stats = QueueStats.from_json(value)

                    total_kills += queue_stats.total_kills
                    total_assists += queue_stats.total_assists
                    total_deaths += queue_stats.total_deaths
                    total_gold += queue_stats.total_gold

                    total_wins += queue_stats.total_wins
                    total_losses += queue_stats.total_losses
                    if queue_stats.matches >= 10:
                        if queue_stats.win_percent > best_win_percent or (
                            queue_stats.win_percent == best_win_percent
                            and best_queue_matches < queue_stats.matches
                        ):
                            best_win_percent = queue_stats.win_percent
                            best_queue = q
                            best_queue_matches = queue_stats.matches
                        if queue_stats.win_percent < worst_win_percent or (
                            queue_stats.win_percent == worst_win_percent
                            and worst_queue_matches < queue_stats.matches
                        ):
                            worst_win_percent = queue_stats.win_percent
                            worst_queue = q
                            worst_queue_matches = queue_stats.matches

                    total_minutes += queue_stats.total_minutes
                    last_played = max(queue_stats.last_played, last_played)

            total_avg_kda = (total_kills + (total_assists / 2)) / (
                total_deaths if total_deaths > 0 else 1
            )
            total_kda = (
                f"• _Total Kills_: {total_kills:,}\n• _Total Deaths_: {total_deaths:,}\n• _Total Assists_: {total_assists:,}"
                f"\n• _Overall Avg. KDA_: {total_avg_kda:.2f}\n• _Total Gold_: {total_gold:,}"
            )

            matches = total_wins + total_losses
            win_percent = int((total_wins / (matches if matches > 0 else 1)) * 100)
            total_wlr = (
                f"• _Total Wins_: {total_wins:,}\n"
                f"• _Total Losses_: {total_losses:,}\n"
                f"• _Total Disconnects_: {player.leaves}\n"
                f"• _Overall Win Rate_: {win_percent}%"
            )

            time_stats = (
                f'• _Total Time Played ({"Minutes" if total_minutes < 60 else "Hours"})_: '
                f"{(total_minutes if total_minutes < 60 else total_minutes / 60):,.2f}\n"
                f'• _Account Create Date_: {datetime.datetime.strftime(player.created_datetime, "%B %d, %Y")}\n'
                f'• _Last Played_: {datetime.datetime.strftime(last_played, "%B %d, %Y")}'
            )

            if best_queue is not None:
                worst_queue_stats = ""
                if worst_queue is not None:
                    worst_queue_stats = (
                        f" Their worst queue is {worst_queue} "
                        f"with a pitiful win rate of {int(worst_win_percent * 100)}% "
                        f'({worst_queue_matches} match{"es" if worst_queue_matches > 1 else ""}).'
                    )
                best_queue_stats = (
                    f"{player.name}'s best queue is "
                    f"{best_queue} with a win rate of {int(best_win_percent * 100)}% "
                    f'({best_queue_matches} match{"es" if best_queue_matches > 1 else ""}).'
                    f"{worst_queue_stats}"
                )
                stats_embed.set_footer(text=best_queue_stats)

            stats_embed.add_field(name="Overall KDA", value=total_kda)
            stats_embed.add_field(name="Overall Win/Loss Ratio", value=total_wlr)
            stats_embed.add_field(name="Playtime", value=time_stats)

            await ctx.respond(embed=stats_embed, ephemeral=True)

    async def __rank_lookup(
        self,
        player: Player,
        ctx_or_message: discord.ApplicationContext | discord.Message,
    ):
        if player is None:
            return

        def get_rank_string(
            queue_id: QueueId,
            tier_id: TierId,
            mmr: float,
            points: int,
            wins: int,
            losses: int,
        ) -> str:
            points_str = ""
            if tier_id.value < 25:
                points_str = f" {points}/100 TP"
            return (
                f'• {queue_id.display_name.replace("Controller", "🎮")}: '
                f"{self.get_tier_string(tier_id, mmr)}{points_str} - "
                f"{wins} wins / {losses} losses ({wins + losses} total)\n"
            )

        rank_string = ""
        for queue, stats in sorted(
            player.ranked_stats.items(), key=lambda q: q[0].name
        ):
            rank_string += get_rank_string(
                queue, stats.tier, stats.mmr, stats.points, stats.wins, stats.losses
            )
        if rank_string == "":
            await self.__send_response_or_message_embed(
                ctx_or_message,
                discord.Embed(
                    color=discord.Color.yellow(),
                    description=f"{player.name} has no ranks...",
                ),
            )
            return
        await self.__send_response_or_message_embed(
            ctx_or_message,
            discord.Embed(
                color=discord.Color.blue(),
                description=rank_string,
                title=f"{player.name} Ranks:",
            ),
        )

    @commands.user_command(
        name="Smite Rank Stats",
        guild_ids=[396874836250722316, 845718807509991445],
    )
    async def rank_lookup(
        self, ctx: discord.ApplicationContext, member: discord.Member
    ) -> None:
        if member.id not in self.__user_id_to_smite_username:
            await self.__send_invalid(
                ctx,
                error_info="Unable to find that player.",
            )
            return

        player = await self.__get_player_or_return_invalid(
            self.__user_id_to_smite_username[member.id], ctx
        )
        await self.__rank_lookup(player, ctx)

    @commands.slash_command(
        name="rank",
        description="Look up a Smite player's ranked stats",
        guild_ids=[845718807509991445, 396874836250722316],
    )
    @discord.option(
        name="player_name",
        type=str,
        description="The player name of the person to look up",
        required=True,
    )
    async def rank(self, ctx: discord.ApplicationContext, player_name: str) -> None:
        if not any(player_name):
            await self.__send_invalid(ctx, error_info="Player name cannot be empty")
            return
        player = await self.__get_player_or_return_invalid(player_name, ctx)
        await self.__rank_lookup(player, ctx)

    @commands.slash_command(
        name="worshippers",
        description="Look up a Smite player's god stats",
        guild_ids=[845718807509991445, 396874836250722316],
    )
    @discord.option(
        name="player_name",
        type=str,
        description="The player name of the person to look up",
        required=True,
    )
    @discord.option(
        name="god_name",
        type=str,
        description="The god to look up worshippers for",
        default="",
    )
    @discord.option(
        name="role_name",
        type=str,
        description="The god role to look up worshippers for",
        choices=[r.name.title() for r in list(GodRole)],
        default="",
    )
    async def worshippers(
        self,
        ctx: discord.ApplicationContext,
        player_name: str,
        god_name: str,
        role_name: str,
    ):
        if not any(player_name):
            await self.__send_invalid(ctx, error_info="Player name cannot be empty")
            return
        if (
            god_name is not None
            and any(god_name)
            and role_name is not None
            and any(role_name)
        ):
            await self.__send_invalid(
                ctx, error_info="Can only specify one of either god or role"
            )
            return

        god_id: GodId | None = None
        god_role: GodRole | None = None
        if god_name is not None and any(god_name):
            cleaned_god_name = god_name.upper().replace(" ", "_").replace("'", "")
            if cleaned_god_name in list(g.name for g in list(GodId)):
                god_id = GodId[cleaned_god_name]
            else:
                await self.__send_invalid(
                    ctx,
                    error_info=f"{god_name} is not a valid god!",
                )
                return
        if role_name is not None and any(role_name):
            cleaned_role_name = role_name.upper().replace(" ", "_").replace("'", "")
            if cleaned_role_name in list(g.name for g in list(GodRole)):
                god_role = GodRole[cleaned_role_name]
            else:
                await self.__send_invalid(
                    ctx,
                    error_info=f"{role_name} is not a valid role!",
                )
                return

        player = await self.__get_player_or_return_invalid(player_name, ctx)
        if player is None:
            return

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

        stats_embed = discord.Embed(
            color=discord.Color.blue(),
            title=f"{player.name}'s "
            f'{self.__provider.gods[god_id].name if god_id is not None else god_role.name.title() if god_role is not None else "Overall"} Stats',
        )
        if god_id is not None:
            if god_id not in stats:
                await self.__send_invalid(
                    ctx,
                    error_info=f"{player.name} doesn't have any worshippers for {self.__provider.gods[god_id].name}!",
                )
                return

            god_stats = stats[god_id]
            kills = god_stats["kills"]
            assists = god_stats["assists"]
            deaths = god_stats["deaths"]
            avg_kda = (kills + (assists / 2)) / (deaths if deaths > 0 else 1)
            kda = (
                f"• _Kills_: {kills:,}\n• _Deaths_: {deaths:,}\n• _Assists_: {assists:,}"
                f'\n• _Avg. KDA_: {avg_kda:.2f}\n• _Minion Kills_: {god_stats["minions"]:,}'
            )
            wins = god_stats["wins"]
            losses = god_stats["losses"]
            wlr = f"• _Wins_: {wins:,}\n• _Losses_: {losses:,}\n• _Win Rate_: {int((wins / (wins + losses)) * 100)}%"
            worshippers = f'_Worshippers_: {god_stats["worshippers"]:,} (_Rank {god_stats["rank"]:,}_)'

            stats_embed.add_field(name="KDA", value=kda)
            stats_embed.add_field(name="Win/Loss Ratio", value=wlr)
            stats_embed.add_field(name="Worshippers", value=worshippers)
            stats_embed.set_thumbnail(url=self.__provider.gods[god_id].icon_url)

            await ctx.respond(embed=stats_embed, ephemeral=True)
            return

        if god_role is not None:
            stats = dict(
                filter(
                    lambda g: self.__provider.gods[g[0]].role == god_role, stats.items()
                )
            )

        total_kills = 0
        total_assists = 0
        total_deaths = 0
        total_minions = 0
        total_wins = 0
        total_losses = 0
        total_worshippers = 0
        for _, god in stats.items():
            total_kills += god["kills"]
            total_assists += god["assists"]
            total_deaths += god["deaths"]
            total_minions += god["minions"]
            total_wins += god["wins"]
            total_losses += god["losses"]
            total_worshippers += god["worshippers"]
        total_avg_kda = (total_kills + (total_assists / 2)) / (
            total_deaths if total_deaths > 0 else 1
        )
        total_kda = (
            f"• _Total Kills_: {total_kills:,}\n• _Total Deaths_: {total_deaths:,}\n• _Total Assists_: {total_assists:,}"
            f"\n• _Overall Avg. KDA_: {total_avg_kda:.2f}\n• _Total Minion Kills_: {total_minions:,}"
        )

        total_wlr = f"• _Total Wins_: {total_wins:,}\n• _Total Losses_: {total_losses:,}\n• _Overall Win Rate_: {int((total_wins / (total_wins + total_losses)) * 100)}%"

        total_worshippers_str = f"_Total Worshippers_: {total_worshippers:,}"

        stats_embed.add_field(name="Overall KDA", value=total_kda)
        stats_embed.add_field(name="Overall Win/Loss Ratio", value=total_wlr)
        stats_embed.add_field(name="Overall Worshippers", value=total_worshippers_str)
        stats_embed.set_thumbnail(url=player.avatar_url)

        await ctx.respond(embed=stats_embed, ephemeral=True)

    @commands.user_command(
        name="Smite Total Worshipper Stats",
        guild_ids=[396874836250722316, 845718807509991445],
    )
    async def worshipper_lookup(
        self, ctx: discord.ApplicationContext, member: discord.Member
    ) -> None:
        if member.id not in self.__user_id_to_smite_username:
            await self.__send_invalid(
                ctx,
                error_info="Unable to find that player.",
            )
            return

        player = await self.__get_player_or_return_invalid(
            self.__user_id_to_smite_username[member.id], ctx
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

        stats_embed = discord.Embed(
            color=discord.Color.blue(),
            title=f"{player.name}'s Overall Stats",
        )

        total_kills = 0
        total_assists = 0
        total_deaths = 0
        total_minions = 0
        total_wins = 0
        total_losses = 0
        total_worshippers = 0
        for _, god in stats.items():
            total_kills += god["kills"]
            total_assists += god["assists"]
            total_deaths += god["deaths"]
            total_minions += god["minions"]
            total_wins += god["wins"]
            total_losses += god["losses"]
            total_worshippers += god["worshippers"]
        total_avg_kda = (total_kills + (total_assists / 2)) / (
            total_deaths if total_deaths > 0 else 1
        )
        total_kda = (
            f"• _Total Kills_: {total_kills:,}\n• _Total Deaths_: {total_deaths:,}\n• _Total Assists_: {total_assists:,}"
            f"\n• _Overall Avg. KDA_: {total_avg_kda:.2f}\n• _Total Minion Kills_: {total_minions:,}"
        )

        total_wlr = f"• _Total Wins_: {total_wins:,}\n• _Total Losses_: {total_losses:,}\n• _Overall Win Rate_: {int((total_wins / (total_wins + total_losses)) * 100)}%"

        total_worshippers_str = f"_Total Worshippers_: {total_worshippers:,}"

        stats_embed.add_field(name="Overall KDA", value=total_kda)
        stats_embed.add_field(name="Overall Win/Loss Ratio", value=total_wlr)
        stats_embed.add_field(name="Overall Worshippers", value=total_worshippers_str)
        stats_embed.set_thumbnail(url=player.avatar_url)

        await ctx.respond(embed=stats_embed, ephemeral=True)
