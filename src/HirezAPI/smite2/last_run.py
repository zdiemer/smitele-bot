"""What the last crawl did, kept somewhere that outlives the Job.

`collect.py` finishes by printing a good report — requests, megabytes, new
matches, rate limits, the pace it ended on, per-day coverage — and then the pod
is garbage collected and all of it is gone. `successfulJobsHistoryLimit: 2`
means the answer to "did last night work?" has a shelf life of about two days
and requires `kubectl`.

So the same numbers are written here as well. The print stays: a person reading
Job logs wants prose, and a dashboard wants fields, and neither is a good
substitute for the other.

Deliberately a *separate* file from `tracker_cooldown.json` and
`clearance.json`, and for the opposite reason those two are separate from each
other. Those hold state the crawl reads back and acts on — a ban deadline, a
cookie, a mint budget — so a corrupt or truncated one changes what the next run
*does*. This holds a report nothing reads back. Keeping it apart means the file
that is rewritten every single night, in full, is the one where a failed write
costs nothing but a blank card on a web page.

Not bucketed per egress either, for the same reason: there is one crawl at a
time and this describes that crawl, not a standing fact about an address.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

FILE_NAME = "last_run.json"

SCHEMA_VERSION = 1

# Why the run stopped, mirroring collect.py's exit codes so the two cannot drift
# into describing different things:
#
#   ok            0  ran out of roster with budget and wall clock to spare
#   budget        0  spent every request it was allowed
#   deadline      0  ran until the wall clock said stop
#   coverage      0  hit the coverage target and stopped early
#   running       -  still going; written by a mid-run checkpoint
#   no_gods       1  the wiki catalogue would not load, so nothing was crawled
#   blocked       2  TrackerBlocked or ClearanceUnavailable mid-crawl
#   standdown     3  refused to start; a recorded ban was still in force
#   terminated    4  SIGTERM — saved what it had and stopped
#
# "standdown" is the one worth having. A night that never ran looks exactly like
# a night that has not happened yet unless something says otherwise.
#
# "ok" used to cover the first four. They wanted separating once a run was meant
# to last until its deadline: finishing on "budget" now means the ceiling was
# set too low, where finishing on "deadline" means the night went as intended,
# and a single "ok" cannot say which. Kept in the tuple so an old record still
# validates.
REASONS = (
    "ok",
    "budget",
    "deadline",
    "coverage",
    "running",
    "blocked",
    "standdown",
    "no_gods",
    "terminated",
)


def path_for(state_dir: str) -> str:
    return os.path.join(state_dir, FILE_NAME)


def write(state_dir: str, record: Dict[str, Any]) -> None:
    """Record one run. Never raises — a report is not worth failing a run over."""
    document = dict(record)
    document["version"] = SCHEMA_VERSION
    document.setdefault("finished", time.time())

    target = path_for(state_dir)
    partial = f"{target}.partial"
    try:
        os.makedirs(state_dir or ".", exist_ok=True)
        with open(partial, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.replace(partial, target)
    except OSError as error:
        print(f"last_run: could not persist to {target}: {error}", flush=True)


def read(state_dir: str) -> Optional[Dict[str, Any]]:
    """The last recorded run, or None if there has never been one."""
    try:
        with open(path_for(state_dir), "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None
