from datetime import datetime
from enum import Enum
from typing import Dict, List
from SmiteProvider import SmiteProvider
from match import PlayerMatch
from HirezAPI import HIREZ_DATE_FORMAT, PortalId, QueueId, TierId


class StatusId(Enum):
    OFFLINE = 0
    IN_LOBBY = 1
    GOD_SELECTION = 2
    IN_GAME = 3
    ONLINE = 4
    UNKNOWN = 5


class PlayerAchievements:
    assisted_kills: int
    camps_cleared: int
    deaths: int
    divine_spree: int
    double_kills: int
    fire_giant_kills: int
    first_bloods: int
    god_like_spree: int
    gold_fury_kills: int
    immortal_spree: int
    killing_spree: int
    minion_kills: int
    penta_kills: int
    phoenix_kills: int
    player_kills: int
    quadra_kills: int
    rampage_spree: int
    shutdown_spree: int
    siege_juggernaut_kills: int
    tower_kills: int
    triple_kills: int
    unstoppable_spree: int
    wild_juggernaut_kills: int

    @staticmethod
    def from_json(value):
        player_achievements = PlayerAchievements()

        player_achievements.assisted_kills = value["AssistedKills"]
        player_achievements.camps_cleared = value["CampsCleared"]
        player_achievements.deaths = value["Deaths"]
        player_achievements.divine_spree = value["DivineSpree"]
        player_achievements.double_kills = value["DoubleKills"]
        player_achievements.fire_giant_kills = value["FireGiantKills"]
        player_achievements.first_bloods = value["FirstBloods"]
        player_achievements.god_like_spree = value["GodLikeSpree"]
        player_achievements.gold_fury_kills = value["GoldFuryKills"]
        player_achievements.immortal_spree = value["ImmortalSpree"]
        player_achievements.killing_spree = value["KillingSpree"]
        player_achievements.minion_kills = value["MinionKills"]
        player_achievements.penta_kills = value["PentaKills"]
        player_achievements.phoenix_kills = value["PhoenixKills"]
        player_achievements.player_kills = value["PlayerKills"]
        player_achievements.quadra_kills = value["QuadraKills"]
        player_achievements.rampage_spree = value["RampageSpree"]
        player_achievements.shutdown_spree = value["ShutdownSpree"]
        player_achievements.siege_juggernaut_kills = value["SiegeJuggernautKills"]
        player_achievements.tower_kills = value["TowerKills"]
        player_achievements.triple_kills = value["TripleKills"]
        player_achievements.unstoppable_spree = value["UnstoppableSpree"]
        player_achievements.wild_juggernaut_kills = value["WildJuggernautKills"]

        return player_achievements


class PlayerStatus:
    match_id: int | None
    queue_id: QueueId | None
    status: StatusId

    def __init__(self, status: StatusId, match_id: int, queue_id: QueueId):
        self.match_id = match_id
        self.queue_id = queue_id
        self.status = status

    @staticmethod
    def from_json(value):
        match_id = int(value["Match"])
        queue_id: QueueId | None = None
        if match_id != 0:
            queue_id = QueueId(int(value["match_queue_id"]))
        return PlayerStatus(
            StatusId(int(value["status"])),
            match_id if match_id != 0 else None,
            queue_id,
        )


class RankedStat:
    mmr: float
    leaves: int
    losses: int
    points: int
    tier: TierId
    variance: int
    round: int
    season: int
    trend: int
    wins: int

    def __init__(self):
        pass

    @staticmethod
    def from_json(value):
        ranked_stat = RankedStat()
        ranked_stat.leaves = int(value["Leaves"])
        ranked_stat.losses = int(value["Losses"])
        ranked_stat.points = int(value["Points"])
        ranked_stat.mmr = float(value["Rank_Stat"])
        ranked_stat.variance = int(value["Rank_Variance"])
        ranked_stat.round = int(value["Round"])
        ranked_stat.season = int(value["Season"])
        ranked_stat.tier = TierId(int(value["Tier"]))
        ranked_stat.trend = int(value["Trend"])
        ranked_stat.wins = int(value["Wins"])
        return ranked_stat


class MergedPlayer:
    merge_datetime: datetime
    id: int
    portal_id: PortalId

    def __init__(self, merge_datetime: datetime, id: int, portal_id: int):
        self.merge_datetime = merge_datetime
        self.id = id
        self.portal_id = portal_id

    @staticmethod
    def from_json(value):
        return MergedPlayer(
            datetime.strptime(value["merge_datetime"], "%b %d %Y %I:%M%p"),
            value["playerId"],
            PortalId(int(value["portalId"])),
        )


class Player:
    active_player_id: int
    id: int
    avatar_url: str
    created_datetime: datetime
    hours_played: int
    minutes_played: int
    last_login_datetime: datetime
    leaves: int
    level: int
    losses: int
    mastery_level: int
    merged_players: List[MergedPlayer]
    account_name: str
    status_message: str
    platform: str
    region: str
    clan_id: int
    clan_name: str
    total_achievements: int
    total_worshippers: int
    wins: int
    name: str
    ranked_stats: Dict[QueueId, RankedStat]
    __provider: SmiteProvider

    def __init__(self, provider: SmiteProvider = None):
        self.__provider = provider

    @staticmethod
    def from_json(value, provider: SmiteProvider = None):
        player = Player(provider)
        player.active_player_id = int(value["ActivePlayerId"])
        player.id = int(value["Id"])
        player.avatar_url = value["Avatar_URL"]
        player.created_datetime = datetime.strptime(
            value["Created_Datetime"], HIREZ_DATE_FORMAT
        )
        player.hours_played = int(value["HoursPlayed"])
        player.minutes_played = int(value["MinutesPlayed"])
        player.last_login_datetime = datetime.strptime(
            value["Last_Login_Datetime"], HIREZ_DATE_FORMAT
        )
        player.leaves = int(value["Leaves"])
        player.level = int(value["Level"])
        player.losses = int(value["Losses"])
        player.mastery_level = int(value["MasteryLevel"])
        merged_players = value["MergedPlayers"]
        player.merged_players = (
            [MergedPlayer.from_json(obj) for obj in merged_players]
            if merged_players is not None and merged_players != "None"
            else []
        )
        player.account_name = value["Name"]
        player.status_message = value["Personal_Status_Message"]
        player.platform = value["Platform"]
        player.ranked_stats = {}
        for queue in list(QueueId):
            if not QueueId.is_ranked(queue):
                continue
            queue_name = queue.name.lower().replace("_", " ").title().replace(" ", "")
            if value[queue_name]["Tier"] == 0:
                continue
            player.ranked_stats[queue] = RankedStat.from_json(value[queue_name])
        player.region = value["Region"]
        player.clan_id = int(value["TeamId"])
        player.clan_name = value["Team_Name"]
        player.total_achievements = int(value["Total_Achievements"])
        player.total_worshippers = int(value["Total_Worshippers"])
        player.wins = int(value["Wins"])
        player.name = value["hz_player_name"] or player.account_name
        return player

    async def get_player_status(self) -> PlayerStatus | None:
        player_statuses = await self.__provider.get_player_status(self.id)
        if not any(player_statuses):
            return None
        return PlayerStatus.from_json(player_statuses[0])

    async def get_player_achievements(self) -> PlayerAchievements:
        player_achievements = await self.__provider.get_player_achievements(self.id)
        return PlayerAchievements.from_json(player_achievements)

    async def get_match_history(self) -> List[PlayerMatch]:
        match_history = await self.__provider.get_match_history(self.id)

        if any(
            match["ret_msg"] is not None
            and match["ret_msg"].startswith("No Match History")
            for match in match_history
        ):
            return []

        return [
            PlayerMatch.from_json(match, self.__provider) for match in match_history
        ]


class PlayerId:
    id: int
    portal_id: PortalId
    private: bool
    __provider: SmiteProvider

    def __init__(
        self,
        id: int,
        private: bool,
        portal_id: PortalId = None,
        provider: SmiteProvider = None,
    ):
        self.id = id
        self.portal_id = portal_id
        self.private = private
        self.__provider = provider

    @staticmethod
    def from_json(value, provider: SmiteProvider = None):
        return PlayerId(
            int(value["player_id"]),
            value["privacy_flag"] == "y",
            PortalId(int(value["portal_id"])),
            provider,
        )

    async def get_player(self, id_override: int = None) -> Player | None:
        id = id_override or self.id
        players = await self.__provider.get_player(id)
        if not any(players):
            return None
        return Player.from_json(players[0], self.__provider)
