"""How much of a day the crawl actually saw.

The feasibility tables this collector replaces were built on an estimate of how
many matches a day the game produces, and say so. Rather than inherit that, the
crawl measures its own coverage as it goes, which is what lets a budget be set
against a coverage target instead of against a guess.

Capture-recapture, the way ecologists count fish. Split the queried roster in
two by hash; each half independently "captures" some of the day's matches. If
the halves overlap a lot, the day is nearly exhausted; if they barely overlap,
most of it is still unseen.

Chapman's correction rather than plain Lincoln-Petersen, because early in a
crawl the overlap is small and the uncorrected estimator is badly biased — and
undefined outright when nothing overlaps.

The assumption capture-recapture makes is that the two halves sample
independently. Premades violate it: two people who always queue together are not
independent draws. Suppressing them in the frontier is what makes this
approximately true, and where it still fails the estimated total comes out too
low, so the coverage figure this reports is an **upper bound**.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, Optional, Set


def half_of(player_key: str) -> int:
    """Which half of the roster a player belongs to.

    By hash of the key rather than by arrival order, so the split is stable
    across runs and does not correlate with anything about the player.
    """
    return hashlib.blake2b(player_key.encode(), digest_size=2).digest()[0] % 2


@dataclass
class DayCoverage:
    date: str
    first: Set[str] = field(default_factory=set)
    second: Set[str] = field(default_factory=set)

    def observe(self, match_id: str, player_key: str) -> None:
        (self.first if half_of(player_key) == 0 else self.second).add(match_id)

    @property
    def seen(self) -> int:
        return len(self.first | self.second)

    @property
    def both(self) -> int:
        return len(self.first & self.second)

    @property
    def estimated_total(self) -> Optional[float]:
        """Chapman-corrected Lincoln-Petersen.

        None when either half is too small to say anything — reporting a wild
        number from three observations is worse than reporting nothing.
        """
        n1, n2 = len(self.first), len(self.second)
        if n1 < 5 or n2 < 5:
            return None
        return (n1 + 1) * (n2 + 1) / (self.both + 1) - 1

    @property
    def coverage(self) -> Optional[float]:
        total = self.estimated_total
        if not total:
            return None
        return min(1.0, self.seen / total)


class CoverageTracker:
    """Coverage per calendar day, across one run."""

    def __init__(self) -> None:
        self.days: Dict[str, DayCoverage] = {}

    def observe(self, date: str, match_id: str, player_key: str) -> None:
        if not date:
            return
        self.days.setdefault(date, DayCoverage(date)).observe(match_id, player_key)

    def for_date(self, date: str) -> Optional[DayCoverage]:
        return self.days.get(date)

    def best_estimate(self, recent: int = 3) -> Optional[float]:
        """Coverage of the most recent days that can be estimated.

        The newest days are the ones a crawl is actually trying to fill; older
        ones are only reachable through players who happened to play then, so
        their coverage says little about how the run is doing.
        """
        estimates = [
            day.coverage
            for _date, day in sorted(self.days.items(), reverse=True)[:recent]
            if day.coverage is not None
        ]
        return sum(estimates) / len(estimates) if estimates else None

    def report(self, limit: int = 8) -> str:
        lines = [
            f"  {'day':<12}{'seen':>7}{'half A':>8}{'half B':>8}"
            f"{'both':>6}{'est.':>9}{'coverage':>10}"
        ]
        for date, day in sorted(self.days.items(), reverse=True)[:limit]:
            total = day.estimated_total
            coverage = day.coverage
            lines.append(
                f"  {date:<12}{day.seen:>7}{len(day.first):>8}{len(day.second):>8}"
                f"{day.both:>6}"
                + (f"{total:>9.0f}" if total else f"{'-':>9}")
                + (f"{coverage:>9.1%}" if coverage else f"{'-':>10}")
            )
        lines.append(
            "  Coverage is an upper bound: premades break the independence the "
            "estimator assumes, which biases the total low."
        )
        return "\n".join(lines)
