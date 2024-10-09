from datetime import datetime
from enum import Enum
from typing import List, NamedTuple

from SmiteProvider import SmiteProvider
from god import God
from god_types import GodId
from item import Item
from HirezAPI import QueueId


class Build:
    actives: List[Item]
    items: List[Item]

    def __init__(self):
        self.actives = []
        self.items = []


class TeamName(Enum):
    ORDER = 1
    CHAOS = 2


class Team:
    members: List[int]
    name: TeamName

    def __init__(self):
        self.members = []
        self.name = None


class Teams(NamedTuple):
    order: Team
    chaos: Team

    @staticmethod
    def default():
        return Teams(Team(), Team())


class _MatchBase:
    queue_id: QueueId
    id: int
    teams: Teams

    def __init__(self, queue_id: QueueId, id: int, teams: Teams = None):
        self.queue_id = queue_id
        self.id = id
        self.teams = teams or Teams.default()


class LiveMatch(_MatchBase):
    def __init__(self, queue_id: QueueId, id: int):
        super().__init__(queue_id, id)


class Match(_MatchBase):
    bans: List[GodId]
    timestamp: datetime
    match_time_seconds: int
    region: str
    first_ban_side: str

    def __init__(self, queue_id: QueueId, id: int):
        self.bans = []

        super().__init__(queue_id, id)


class DamageDealt:
    god: int
    minion: int
    in_hand: int
    structure: int


class DamageTaken:
    mitigated: int
    magical: int
    physical: int

    @property
    def total(self) -> int:
        return self.magical + self.physical


class Healing:
    teammates: int
    minions: int
    self: int


class PlayerMatch:
    build: Build
    assists: int
    minions_killed: int
    damage_dealt: DamageDealt
    damage_taken: DamageTaken
    deaths: int
    distance_traveled: int
    god: God
    gold: int
    healing: Healing
    max_killing_spree: int
    kills: int
    level: int
    max_multi_kill: int
    objective_assists: int
    role: str
    skin_id: int
    wards_placed: int
    player_id: int
    won: bool
    match: Match

    @staticmethod
    def from_json(value, provider: SmiteProvider):
        player_match = PlayerMatch()

        build = Build()

        for i in range(1, 3):
            item_id = int(value[f"ActiveId{i}"] or 0)
            if item_id != 0 and item_id in provider.items:
                build.actives.append(provider.items[item_id])

        for i in range(1, 7):
            item_id = int(value[f"ItemId{i}"] or 0)
            if item_id in provider.items:
                build.items.append(provider.items[item_id])

        player_match.build = build
        player_match.assists = int(value["Assists"] or 0)
        player_match.minions_killed = int(value["Creeps"] or 0)
        player_match.deaths = int(value["Deaths"] or 0)
        player_match.distance_traveled = int(value["Distance_Traveled"] or 0)
        player_match.gold = int(value["Gold"] or 0)
        player_match.max_killing_spree = int(value["Killing_Spree"] or 0)
        player_match.kills = int(value["Kills"] or 0)
        player_match.level = int(value["Level"] or 0)
        player_match.max_multi_kill = int(value["Multi_kill_Max"] or 0)
        player_match.objective_assists = int(value["Objective_Assists"] or 0)
        player_match.role = value["Role"]
        player_match.wards_placed = int(value["Wards_Placed"] or 0)

        player_match.god = provider.gods[GodId(int(value["GodId"]))]
        player_match.skin_id = int(value["SkinId"] or 0)
        player_match.won = value["Win_Status"] == "Win"

        damage_dealt = DamageDealt()
        damage_dealt.god = int(value["Damage"] or 0)
        damage_dealt.minion = int(value["Damage"] or 0)
        damage_dealt.in_hand = int(value["Damage_Done_In_Hand"] or 0)
        damage_dealt.structure = int(value["Damage_Structure"] or 0)

        player_match.damage_dealt = damage_dealt

        damage_taken = DamageTaken()
        damage_taken.mitigated = int(value["Damage_Mitigated"] or 0)
        damage_taken.magical = int(value["Damage_Taken_Magical"] or 0)
        damage_taken.physical = int(value["Damage_Taken_Physical"] or 0)

        player_match.damage_taken = damage_taken

        healing = Healing()
        healing.teammates = int(value["Healing"] or 0)
        healing.minions = int(value["Healing_Bot"] or 0)
        healing.self = int(value["Healing_Player_Self"] or 0)

        player_match.healing = healing

        match = Match(QueueId(value["Match_Queue_Id"]), int(value["Match"]))
        match.first_ban_side = value["First_Ban_Side"]
        match.timestamp = datetime.strptime(value["Match_Time"], "%m/%d/%Y %I:%M:%S %p")
        match.match_time_seconds = int(value["Time_In_Match_Seconds"] or 0)
        match.region = value["Region"]

        for i in range(1, 13):
            god_id = int(value[f"Ban{i}Id"] or 0)
            if GodId.has_value(god_id):
                match.bans.append(GodId(god_id))

        player_match.match = match

        return player_match
