import datetime

import discord
from discord.ext import commands

from god import GodId
from player import Player, PlayerId
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
            god_wins = int(god['Wins'])
            god_losses = int(god['Losses'])
            god_matches = god_wins + god_losses
            queue_stats.total_kills += int(god['Kills'])
            queue_stats.total_assists += int(god['Assists'])
            queue_stats.total_deaths += int(god['Deaths'])
            queue_stats.total_gold += int(god['Gold'])
            queue_stats.total_wins += god_wins
            queue_stats.total_losses += god_losses
            queue_stats.total_minutes += int(god['Minutes'])
            god_last_played = \
                datetime.datetime.strptime(god['LastPlayed'], HIREZ_DATE_FORMAT)
            queue_stats.last_played = max(god_last_played, queue_stats.last_played)

            if god_matches >= 10:
                god_win_percent = god_wins / god_matches
                if queue_stats.best_god_win_percent < god_win_percent or \
                        (queue_stats.best_god_win_percent == god_win_percent \
                            and queue_stats.best_god_matches < god_matches):
                    queue_stats.best_god_win_percent = god_win_percent
                    queue_stats.best_god = GodId(int(god['GodId']))
                    queue_stats.best_god_matches = god_matches
                elif queue_stats.worst_god_win_percent > god_win_percent or \
                        (queue_stats.worst_god_win_percent == god_win_percent \
                            and queue_stats.worst_god_matches < god_matches):
                    queue_stats.worst_god_win_percent = god_win_percent
                    queue_stats.worst_god = GodId(int(god['GodId']))
                    queue_stats.worst_god_matches = god_matches

        return queue_stats

    @property
    def matches(self) -> int:
        return self.total_wins + self.total_losses

    @property
    def total_avg_kda(self) -> float:
        return (self.total_kills + (self.total_assists / 2)) / \
            (self.total_deaths if self.total_deaths > 0 else 1)

    @property
    def win_percent(self) -> float:
        return self.total_wins / (self.matches if self.matches > 0 else 1)

class PlayerStats(commands.Cog):
    __bot: commands.Bot
    __provider: SmiteProvider

    def __init__(self, bot: commands.Bot, provider: SmiteProvider):
        self.__bot = bot
        self.__provider = provider

    @staticmethod
    async def __send_invalid(
            message: discord.Message,
            base: str = '',
            error_info: str = 'Invalid command!',
            include_command_info: bool = True):
        desc = f'{error_info}'
        if include_command_info:
            desc += base
        await message.channel.send(embed=discord.Embed(color=discord.Color.red(), \
            description=desc))

    async def __get_non_pc_player_ids(self, gamertag: str) -> list:
        for portal_id in list(PortalId):
            player_ids = await self.__provider.get_player_ids_by_gamer_tag(portal_id, gamertag)
            if not any(player_ids):
                continue
            return player_ids
        return []

    async def __get_player_from_id(self, id: int) -> Player | None:
        players = await self.__provider.get_player(id)
        if not any(players):
            return None
        return Player.from_json(players[0])

    async def __get_player(self, username: str) -> Player | None:
        player_ids = await self.__provider.get_player_id_by_name(username)
        if not any(player_ids):
            player_ids = await self.__get_non_pc_player_ids(username)
            if not any(player_ids):
                return None
        player_id_info = PlayerId.from_json(player_ids[0])
        if player_id_info.private:
            raise PlayerPrivacyError
        player = await self.__get_player_from_id(player_id_info.id)
        if player is not None and player.active_player_id != player.id:
            player = await self.__get_player_from_id(player.active_player_id)
        if player is None:
            return None
        return player

    async def __get_player_or_return_invalid(
            self, username: str, message: discord.Message) -> Player | None:
        player: Player | None = None
        try:
            player = await self.__get_player(username)
            if player is None:
                await self.__send_invalid(
                    message,
                    error_info='No players with that name found!',
                    include_command_info=False)
                return None
        except PlayerPrivacyError:
            await self.__send_invalid(
                message,
                error_info=f'{username} has their profile hidden... '\
                            '<:reeratbig:849771936509722634>',
                include_command_info=False)
            return None
        return player

    @staticmethod
    def __get_tier_string(tier_id: TierId, mmr: float) -> str:
        emoji = '🥉' if tier_id.value <= 5 \
            else '🥈' if tier_id.value <= 10 \
            else '🥇' if tier_id.value <= 15 \
            else '🏅' if tier_id.value <= 20 \
            else '💎' if tier_id.value <= 25 \
            else '🏆' if tier_id.value == 26 else '💯'
        return f'{emoji} **{tier_id.display_name}** ({int(mmr)} MMR)'

    @commands.command(aliases=['live'])
    async def livematch(self, message: discord.Message, *args: tuple):
        base = f' {self.__bot.user.mention} accepts the command `$livematch [playername]` '\
                '(or `$live [playername]`)'
        flatten_args = [''.join(arg) for arg in args]
        if not any(flatten_args):
            await self.__send_invalid(message, base)
            return

        player_name = ' '.join(flatten_args)
        player = await self.__get_player_or_return_invalid(player_name, message)
        if player is None:
            return
        player_status = await self.__provider.get_player_status(player.id)
        if int(player_status[0]['status']) != 3:
            await self.__send_invalid(
                message, base,
                f'{player.name} is not currently in a game!', False)
            return
        live_match = await self.__provider.get_match_player_details(player_status[0]['Match'])
        try:
            queue_id = QueueId(int(live_match[0]['Queue']))
        except (KeyError, ValueError):
            print(f'Unsupported queue type: {live_match[0]["Queue"]}')
            await self.__send_invalid(message, base,
                'Unfortunately, the match type this player is playing is not currently supported.', False)
            return
        team_order = list(filter(lambda p: int(p['taskForce']) == 1, live_match))
        team_chaos = list(filter(lambda p: int(p['taskForce']) == 2, live_match))

        def create_team_output(team_list: list) -> str:
            output = ''
            for member in team_list:
                ranked = ''
                if QueueId.is_ranked(queue_id):
                    ranked += f' - {self.__get_tier_string(TierId(int(member["Tier"])), float(member["Rank_Stat"]))}'
                player_name = member['playerName']
                if player_name == '':
                    player_name = 'Hidden Player'
                output += f'• **{player_name}** ({member["GodName"]}){ranked}\n'
            return output

        players_embed = discord.Embed(
                color=discord.Color.blue(),
                title=f'{player.name}\'s Live '\
                      f'{queue_id.name.replace("_", " ").title()} Details')

        players_embed.add_field(name='🔵 Order Side', value=create_team_output(team_order))
        players_embed.add_field(name='🔴 Chaos Side', value=create_team_output(team_chaos))

        await message.channel.send(embed=players_embed)

    @commands.command(aliases=['q'])
    async def queuestats(self, message: discord.Message, *args: tuple):
        base = f' {self.__bot.user.mention} accepts the command `$queuestats [playername] [queuename]` '\
                    '(or `$q [playername] [queuename]`)'
        flatten_args = [''.join(arg) for arg in args]
        if not any(flatten_args):
            await self.__send_invalid(message, base)
            return

        player_name = flatten_args[0]
        queue_name: str | None = None
        queue_id: QueueId | None = None
        if len(flatten_args) > 1:
            queue_name = ' '.join(flatten_args[1:])
            try:
                queue_id = QueueId[queue_name.upper().replace(' ', '_')\
                    .replace("'", '')]
            except KeyError:
                await self.__send_invalid(message, base, f'{queue_name} is not a valid queue!')
                return
        player = await self.__get_player_or_return_invalid(player_name, message)
        if player is None:
            return
        stats_embed = discord.Embed(
                color=discord.Color.blue(),
                title=f'{player.name}\'s {queue_id.display_name if queue_id is not None else "Overall"} Stats')
        stats_embed.set_thumbnail(url=player.avatar_url)
        if queue_id is not None:
            queue_list = await self.__provider.get_queue_stats(player.id, queue_id)
            if not any(queue_list):
                await self.__send_invalid(
                    message,
                    base,
                    f'{player_name} doesn\'t have any playtime for {queue_id.display_name}!',
                    False)
                return
            queue_stats = QueueStats.from_json(queue_list)

            total_kda = f'• _Total Kills_: {queue_stats.total_kills:,}\n'\
                f'• _Total Deaths_: {queue_stats.total_deaths:,}\n'\
                f'• _Total Assists_: {queue_stats.total_assists:,}\n'\
                f'• _Overall Avg. KDA_: {queue_stats.total_avg_kda:.2f}\n'\
                f'• _Total Gold_: {queue_stats.total_gold:,}'

            leave_stats = ''
            if QueueId.is_ranked(queue_id) and queue_id in player.ranked_stats:
                leave_stats = f'• _Total Disconnects_: {player.ranked_stats[queue_id].leaves}\n'

            total_wlr = f'• _Total Wins_: {queue_stats.total_wins:,}\n'\
                f'• _Total Losses_: {queue_stats.total_losses:,}\n'\
                f'{leave_stats}'\
                f'• _Overall Win Rate_: {int(queue_stats.win_percent * 100)}%'

            time_stats = f'• _Total Time Played ({"Minutes" if queue_stats.total_minutes < 60 else "Hours"})_: '\
                f'{(queue_stats.total_minutes if queue_stats.total_minutes < 60 else queue_stats.total_minutes / 60):,.2f}\n'\
                f'• _Last Played_: {datetime.datetime.strftime(queue_stats.last_played, "%B %d, %Y")}'

            if queue_stats.best_god is not None:
                worst_god_stats = ''
                if queue_stats.worst_god is not None:
                    worst_god_stats = f' Their worst god for {queue_id.display_name} is '\
                    f'{self.__provider.gods[queue_stats.worst_god].name} with a pitiful win rate of '\
                    f'{int(queue_stats.worst_god_win_percent * 100)}% ({queue_stats.worst_god_matches} matches).'
                best_god_queue_stats = f'{player.name}\'s best god for {queue_id.display_name} is '\
                    f'{self.__provider.gods[queue_stats.best_god].name} with a win rate of '\
                    f'{int(queue_stats.best_god_win_percent * 100)}% ({queue_stats.best_god_matches} matches).'\
                    f'{worst_god_stats}'
                stats_embed.set_footer(text=best_god_queue_stats)

            stats_embed.add_field(name='Overall KDA', value=total_kda)
            stats_embed.add_field(name='Overall Win/Loss Ratio', value=total_wlr)
            stats_embed.add_field(name='Playtime', value=time_stats)
            await message.channel.send(embed=stats_embed)
            return

        total_kills = 0
        total_assists = 0
        total_deaths = 0
        total_gold = 0
        total_wins = 0
        total_losses = 0
        total_minutes = 0
        last_played = datetime.datetime.min
        best_win_percent = -1
        best_queue: QueueId | None = None
        best_queue_matches = 0
        worst_win_percent = 2
        worst_queue: QueueId | None = None
        worst_queue_matches = 0

        await message.channel.send(
            embed=discord.Embed(
                color=discord.Color.blue(),
                description='Calculating your overall stats across all queues. Please wait...'))
        await message.channel.typing()

        for queue in list(QueueId):
            queue_list = await self.__provider.get_queue_stats(player.id, queue)
            if not any(queue_list):
                continue
            queue_stats = QueueStats.from_json(queue_list)

            total_kills += queue_stats.total_kills
            total_assists += queue_stats.total_assists
            total_deaths += queue_stats.total_deaths
            total_gold += queue_stats.total_gold

            total_wins += queue_stats.total_wins
            total_losses += queue_stats.total_losses
            if queue_stats.matches >= 10:
                if queue_stats.win_percent > best_win_percent or \
                        (queue_stats.win_percent == best_win_percent \
                            and best_queue_matches < queue_stats.matches):
                    best_win_percent = queue_stats.win_percent
                    best_queue = queue
                    best_queue_matches = queue_stats.matches
                if queue_stats.win_percent < worst_win_percent or \
                        (queue_stats.win_percent == worst_win_percent \
                            and worst_queue_matches < queue_stats.matches):
                    worst_win_percent = queue_stats.win_percent
                    worst_queue = queue
                    worst_queue_matches = queue_stats.matches

            total_minutes += queue_stats.total_minutes
            last_played = max(queue_stats.last_played, last_played)

        total_avg_kda = (total_kills + (total_assists / 2)) / (total_deaths if total_deaths > 0 else 1)
        total_kda = f'• _Total Kills_: {total_kills:,}\n• _Total Deaths_: {total_deaths:,}\n• _Total Assists_: {total_assists:,}'\
            f'\n• _Overall Avg. KDA_: {total_avg_kda:.2f}\n• _Total Gold_: {total_gold:,}'

        matches = total_wins + total_losses
        win_percent = int((total_wins / (matches if matches > 0 else 1)) * 100)
        total_wlr = f'• _Total Wins_: {total_wins:,}\n'\
            f'• _Total Losses_: {total_losses:,}\n'\
            f'• _Total Disconnects_: {player.leaves}\n'\
            f'• _Overall Win Rate_: {win_percent}%'

        time_stats = f'• _Total Time Played ({"Minutes" if total_minutes < 60 else "Hours"})_: '\
            f'{(total_minutes if total_minutes < 60 else total_minutes / 60):,.2f}\n'\
            f'• _Account Create Date_: {datetime.datetime.strftime(player.created_datetime, "%B %d, %Y")}\n'\
            f'• _Last Played_: {datetime.datetime.strftime(last_played, "%B %d, %Y")}'

        if best_queue is not None:
            worst_queue_stats = ''
            if worst_queue is not None:
                worst_queue_stats = f' Their worst queue is {worst_queue.display_name} '\
                    f'with a pitiful win rate of {int(worst_win_percent * 100)}% '\
                    f'({worst_queue_matches} match{"es" if worst_queue_matches > 1 else ""}).'
            best_queue_stats = f'{player.name}\'s best queue is '\
                f'{best_queue.display_name} with a win rate of {int(best_win_percent * 100)}% '\
                f'({best_queue_matches} match{"es" if best_queue_matches > 1 else ""}).'\
                f'{worst_queue_stats}'
            stats_embed.set_footer(text=best_queue_stats)

        stats_embed.add_field(name='Overall KDA', value=total_kda)
        stats_embed.add_field(name='Overall Win/Loss Ratio', value=total_wlr)
        stats_embed.add_field(name='Playtime', value=time_stats)

        await message.channel.send(embed=stats_embed)

    @commands.command(aliases=["sr"])
    async def rank(self, message: discord.Message, *args: tuple) -> None:
        base = f' {self.__bot.user.mention} '\
            f'accepts the command `$rank [playername]` (or `$sr [playername]`)'
        if not any(args) or len(args) > 1:
            await self.__send_invalid(message, base)
            return
        player_name = ''.join(args[0])
        player = await self.__get_player_or_return_invalid(player_name, message)
        if player is None:
            return
        def get_rank_string(queue_id: QueueId, tier_id: TierId, mmr: float) -> str:
            return f'• {queue_id.display_name.replace("Controller", "🎮")}: '\
                   f'{self.__get_tier_string(tier_id, mmr)}\n'
        rank_string = ''
        for queue, stats in sorted(player.ranked_stats.items(), key=lambda q: q[0].name):
            rank_string += get_rank_string(queue, stats.tier, stats.mmr)
        if rank_string == '':
            await message.channel.send(embed=discord.Embed(color=discord.Color.yellow(), \
            description=f'{player.name} has no ranks...'))
            return
        await message.channel.send(embed=discord.Embed(color=discord.Color.blue(), \
            description=rank_string, title=f'{player.name} Ranks:'))

    @commands.command(aliases=['w'])
    async def worshippers(self, message: discord.Message, *args: tuple):
        base = f' {self.__bot.user.mention} accepts the command '\
                '`$worshippers [playername] [godname]` '\
                '(or `$w [playername] [godname]`)'
        flatten_args = [''.join(arg) for arg in args]
        if not any(flatten_args):
            await self.__send_invalid(message, base)
            return

        player_name = flatten_args[0]
        god_name: str | None = None
        god_id: GodId | None = None
        if len(flatten_args) > 1:
            god_name = ' '.join(flatten_args[1:])
            try:
                god_id = GodId[god_name.upper().replace(' ', '_')\
                    .replace("'", '')]
            except KeyError:
                await self.__send_invalid(message, base, f'{god_name} is not a valid god!')
                return

        player = await self.__get_player_or_return_invalid(player_name, message)
        if player is None:
            return

        god_ranks = await self.__provider.get_god_ranks(player.id)
        stats = {
            GodId(int(god['god_id'])):{
                'assists': int(god['Assists']),
                'deaths': int(god['Deaths']),
                'kills': int(god['Kills']),
                'losses': int(god['Losses']),
                'rank': int(god['Rank']),
                'wins': int(god['Wins']),
                'worshippers': int(god['Worshippers']),
                'minions': int(god['MinionKills']),
            } for god in god_ranks
        }

        stats_embed = discord.Embed(
                color=discord.Color.blue(), title=f'{player.name}\'s {self.__provider.gods[god_id].name if god_id is not None else "Overall"} Stats')
        if god_id is not None:
            if god_id not in stats:
                await self.__send_invalid(message, base, f'{player.name} doesn\'t have any worshippers for {self.__provider.gods[god_id].name}!', False)
                return

            god_stats = stats[god_id]
            kills = god_stats['kills']
            assists = god_stats['assists']
            deaths = god_stats['deaths']
            avg_kda = (kills + (assists / 2)) / (deaths if deaths > 0 else 1)
            kda = f'• _Kills_: {kills:,}\n• _Deaths_: {deaths:,}\n• _Assists_: {assists:,}'\
                f'\n• _Avg. KDA_: {avg_kda:.2f}\n• _Minion Kills_: {god_stats["minions"]:,}'
            wins = god_stats['wins']
            losses = god_stats['losses']
            wlr = f'• _Wins_: {wins:,}\n• _Losses_: {losses:,}\n• _Win Rate_: {int((wins / (wins + losses)) * 100)}%'
            worshippers = f'_Worshippers_: {god_stats["worshippers"]:,} (_Rank {god_stats["rank"]:,}_)'

            stats_embed.add_field(name='KDA', value=kda)
            stats_embed.add_field(name='Win/Loss Ratio', value=wlr)
            stats_embed.add_field(name='Worshippers', value=worshippers)
            stats_embed.set_thumbnail(url=self.__provider.gods[god_id].icon_url)

            await message.channel.send(embed=stats_embed)
            return
        
        total_kills = sum(god['kills'] for _, god in stats.items())
        total_assists = sum(god['assists'] for _, god in stats.items())
        total_deaths = sum(god['deaths'] for _, god in stats.items())
        total_avg_kda = (total_kills + (total_assists / 2)) / (total_deaths if total_deaths > 0 else 1)
        total_minions = sum(god['minions'] for _, god in stats.items())
        total_kda = f'• _Total Kills_: {total_kills:,}\n• _Total Deaths_: {total_deaths:,}\n• _Total Assists_: {total_assists:,}'\
            f'\n• _Overall Avg. KDA_: {total_avg_kda:.2f}\n• _Total Minion Kills_: {total_minions:,}'

        total_wins = sum(god['wins'] for _, god in stats.items())
        total_losses = sum(god['losses'] for _, god in stats.items())
        total_wlr = f'• _Total Wins_: {total_wins:,}\n• _Total Losses_: {total_losses:,}\n• _Overall Win Rate_: {int((total_wins / (total_wins + total_losses)) * 100)}%'

        total_worshippers = sum(god['worshippers'] for _, god in stats.items())
        total_worshippers_str = f'_Total Worshippers_: {total_worshippers:,}'

        stats_embed.add_field(name='Overall KDA', value=total_kda)
        stats_embed.add_field(name='Overall Win/Loss Ratio', value=total_wlr)
        stats_embed.add_field(name='Overall Worshippers', value=total_worshippers_str)
        stats_embed.set_thumbnail(url=player.avatar_url)

        await message.channel.send(embed=stats_embed)
