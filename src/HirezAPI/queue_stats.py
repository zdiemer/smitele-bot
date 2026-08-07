"""Roll a queue's per-god stat rows into one summary.

Hi-Rez's `getqueuestats` answers per god, not per queue: a hundred-odd rows of
wins, losses, kills and minutes that have to be added up before they mean
anything. This does that addition, and while it is walking the rows it also
picks out the best and worst god — which costs nothing extra there and would
cost a second pass anywhere else.

It lived in the `PlayerStats` cog. It is pure — no Discord, no provider, no
network, just arithmetic over the JSON — so it moves here where the web
snapshot's CronJob can import it too. `src/SmiteBot` is not on the image's
PYTHONPATH; `src/HirezAPI` is.
"""

from __future__ import annotations

import datetime

from god_types import GodId
from HirezAPI import HIREZ_DATE_FORMAT


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
