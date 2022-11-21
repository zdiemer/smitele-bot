from datetime import datetime
from typing import List

from HirezAPI import QueueId
from god import GodId
from item import Item

class Build:
    actives: List[Item]
    items: List[Item]

class Match:
    bans: List[GodId]
    queue: QueueId
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
