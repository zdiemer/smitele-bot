"""The one claim `/build` makes about itself that it cannot check at runtime.

The embed can say how much better a build is than its lane for free: that is the
ranker's own arithmetic over tables already in memory. What it cannot do is say
whether the *ranking* works, because that question is only answerable by holding
out days and looking at what happened — and the bot holds the aggregate, not the
corpus behind it. Building a train-window aggregate and scoring it takes minutes
and gigabytes; a slash command has a second and a shared pod.

So the number is produced nightly by `tools/build_eval.py --emit-lift` and read
back here as a fact. Three rules keep that honest:

  * a file naming a strategy the bot does not ship is ignored, so swapping the
    ranking cannot leave the old ranking's number on screen;
  * a file older than `MAX_AGE_DAYS` is ignored, because "beats the meta by 2%"
    is a claim about a meta and the meta moves;
  * a lane with too few decided cells falls back to the overall figure rather
    than quoting a lift measured over four disagreements.

Absent, stale, malformed or unreadable all mean the same thing — the embed says
nothing about held-out lift — which is why every path here returns None instead
of raising. A missing measurement must never cost a build.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Optional

FILE_NAME: str = "ranker_lift.json"

# How old a measurement may be and still be quoted. Long enough to survive a
# failed nightly run or two, short enough that a patch which reshuffles the item
# pool invalidates it before anyone reads it as current.
MAX_AGE_DAYS: int = 14

# Cells in which the ranking and the baseline actually disagreed. Below this a
# lane's own figure is a handful of coin flips, and the overall one — pooled
# across every lane — is the better answer to the same question.
MIN_DECIDED: int = 20


class RankerLift:
    """A nightly holdout result, or nothing."""

    def __init__(self, payload: dict, path: str = ""):
        self.payload = payload
        self.path = path

    @staticmethod
    def load(directory: str, strategy: str = "shrunk_ranker") -> Optional["RankerLift"]:
        """Read the measurement for a game, or None if there isn't a usable one."""
        path = os.path.join(directory, FILE_NAME)
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None

        if payload.get("strategy") != strategy:
            # A file from a different ranking. Not an error — a sweep writes
            # these too — but not something to quote as the shipped one.
            return None
        if not isinstance(payload.get("overall"), dict):
            return None
        return RankerLift(payload, path)

    @property
    def generated(self) -> Optional[datetime.datetime]:
        try:
            stamp = datetime.datetime.fromisoformat(self.payload["generated"])
        except (KeyError, TypeError, ValueError):
            return None
        # Written with a timezone; treated as UTC if some future writer forgets.
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return stamp

    @property
    def fresh(self) -> bool:
        stamp = self.generated
        if stamp is None:
            return False
        age = datetime.datetime.now(datetime.timezone.utc) - stamp
        return age <= datetime.timedelta(days=MAX_AGE_DAYS)

    def measured(self, role: Optional[str] = None) -> Optional[dict]:
        """The figure to quote for a lane, or overall, or None.

        A lane with too little disagreement behind it falls back rather than
        being reported thinly — the alternative is an embed that tells a Solo
        player their lane is worth +12% on the strength of four cells.
        """
        if not self.fresh:
            return None
        if role:
            lane = (self.payload.get("by_lane") or {}).get(str(role).title())
            if lane and int(lane.get("decided") or 0) >= MIN_DECIDED:
                return {**lane, "scope": str(role).title()}
        overall = self.payload.get("overall") or {}
        if not overall:
            return None
        return {**overall, "scope": "overall"}

    def describe(self, role: Optional[str] = None, currency: str = "win rate") -> str:
        """One sentence for the build embed, or nothing at all.

        Says what was measured and over how much, because the number is only
        worth printing if the reader can tell it is a measurement rather than a
        claim. Ten words is the budget — it sits under a build, not a paper.
        """
        found = self.measured(role)
        if found is None:
            return ""
        lift = float(found.get("lift") or 0.0)
        scope = (
            f"in {found['scope']}" if found.get("scope") != "overall" else "across lanes"
        )
        return (
            f"_Builds picked this way have run **{lift:+.1%}** {currency} above "
            f"the most-played build on days the ranking hadn't seen, "
            f"{scope} ({int(found.get('decided') or 0)} cells)._"
        )
