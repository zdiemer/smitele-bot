"""How long tracker.gg has told this address to stay away.

A 429 carrying `Retry-After: 3600` is not a pacing signal. It is an hour-long
refusal, and the only useful response is to stop and not come back until it has
elapsed. The stopping part already worked; the not-coming-back part did not,
because the number died with the process. A nightly firing into a live ban
learns nothing, collects nothing, and spends reputation to do it.

So the deadline outlives the run. Kept beside the clearance state and keyed the
same way — per egress, because a ban is issued to an address and moving to a
different one is precisely how you stop being subject to it.

Deliberately *not* folded into the clearance file. That holds cookies and the
mint breaker, which is about solving Cloudflare challenges; this is about the
API refusing to serve us, and the two fail and recover independently. Conflating
them would mean `--reset-clearance` quietly clearing a WAF ban, which is exactly
the button someone reaches for when the crawl will not run.

Stored as wall-clock epoch rather than a monotonic deadline: it has to survive
the process, and a monotonic clock does not.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from smite2 import egress as egress_module

FILE_NAME = "tracker_cooldown.json"

SCHEMA_VERSION = 1

# Anything longer than this is almost certainly a clock problem or a bad header
# rather than a real ban, and standing down for a week on a parse error would be
# a worse failure than ignoring it.
MAX_COOLDOWN_SECONDS = 24 * 60 * 60


@dataclass
class Standdown:
    """A recorded refusal: when it lifts, and what said so."""

    until: float = 0.0
    reason: str = ""
    armed_at: float = 0.0

    @property
    def remaining(self) -> float:
        return max(self.until - time.time(), 0.0)

    @property
    def active(self) -> bool:
        return self.remaining > 0.0


class Cooldown:
    """The stand-down deadline for one egress.

    Same read-modify-write discipline as the clearance store, for the same
    reason: the bot and the collector share the file and must not erase each
    other's bucket. Losing a race costs one crawl that should have waited, which
    is the same order of mistake as one extra solve.
    """

    def __init__(self, path: str, egress: Optional[str] = None):
        self.path = path
        self.egress = egress_module.identity() if egress is None else egress

    def __document(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return {"version": SCHEMA_VERSION, "egress": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("egress"), dict):
            return {"version": SCHEMA_VERSION, "egress": {}}
        return raw

    def read(self) -> Standdown:
        bucket = self.__document()["egress"].get(self.egress) or {}
        try:
            return Standdown(
                until=float(bucket.get("until", 0.0)),
                reason=str(bucket.get("reason", "")),
                armed_at=float(bucket.get("armed_at", 0.0)),
            )
        except (TypeError, ValueError):
            return Standdown()

    def arm(self, seconds: float, reason: str) -> Standdown:
        """Record a refusal. Never shortens one already in force."""
        seconds = min(max(seconds, 0.0), MAX_COOLDOWN_SECONDS)
        now = time.time()
        existing = self.read()
        standdown = Standdown(
            until=max(existing.until, now + seconds), reason=reason, armed_at=now
        )
        self.__write(standdown)
        return standdown

    def clear(self) -> None:
        self.__write(Standdown())

    def __write(self, standdown: Standdown) -> None:
        document = self.__document()
        document.setdefault("egress", {})[self.egress] = {
            "until": standdown.until,
            "reason": standdown.reason,
            "armed_at": standdown.armed_at,
        }
        document["version"] = SCHEMA_VERSION

        partial = f"{self.path}.partial"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(partial, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            os.replace(partial, self.path)
        except OSError as error:
            # Failing to record a stand-down must not also fail the stop that
            # prompted it — the run is ending either way.
            print(f"cooldown: could not persist to {self.path}: {error}", flush=True)


def describe(seconds: float) -> str:
    """A duration a human reads without converting anything."""
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f}s"
