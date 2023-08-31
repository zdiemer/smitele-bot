from datetime import datetime
from enum import Enum
from typing import List, NamedTuple

from player import Player
from HirezAPI import QueueId
from god_types import GodId
from item import Item


class Build:
    actives: List[Item]
    items: List[Item]


class TeamName(Enum):
    ORDER = 1
    CHAOS = 2


class Team:
    members: List[Player]
    name: TeamName


class Teams(NamedTuple):
    order: Team
    chaos: Team


class _MatchBase:
    queue_id: QueueId
    id: int
    teams: Teams

    def __init__(self, queue_id: QueueId, id: int, teams: Teams):
        self.queue_id = queue_id
        self.id = id
        self.teams = teams


class LiveMatch(_MatchBase):
    def __init__(self, queue_id: QueueId, id: int, teams: Teams):
        super().__init__(queue_id, id, teams)


class Match(_MatchBase):
    bans: List[GodId]
    timestamp: datetime
    match_time_seconds: int
    region: str


class DamageDealt:
    god: int
    minion: int
    in_hand: int
    structure: int


class DamageTaken:
    mitigated: int
    magical: int
    physical: int


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
    god: GodId
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
