#!/usr/bin/env python3
"""Everything smite.diemer.codes shows, gathered once by a CronJob.

The site is public, and the two things it reports on are metered: Hi-Rez allows
75,000 requests and 500 sessions a day, and a Cloudflare clearance cookie costs
one of twelve daily solves to mint. A web tier that fetched on demand would put
both budgets behind an anonymous URL, where a shared link is indistinguishable
from an attack. So nothing public ever calls a third party. This runs on a
schedule, writes JSON, and `serve.py` reads the file.

Two modes, because the two halves cost wildly different amounts:

  default    liveness. Two Hi-Rez calls plus some stat()s. Cheap enough to run
             every fifteen minutes, which is what makes "is the crawl blocked
             right now?" a question the page can actually answer.
  --players  the roster's stats, both games. About a hundred and twenty Hi-Rez
             calls plus one tracker.gg request per Smite 2 member, for numbers
             that move over weeks. Every six hours is generous.
  --stats    the corpus broken down by queue, god and role. Two small Parquet
             tables and a god catalogue per game.

They write separate files on purpose. One schedule must never be able to blank
the other's data by running first.

    python src/web/snapshot.py --out /matchdata/web
    python src/web/snapshot.py --players --out /matchdata/web
    python src/web/snapshot.py --stats --out /matchdata/web

THREE RULES FOR ANYTHING ADDED HERE.

First: read clearance state through `ClearanceStore`, never `ClearanceManager`.
The manager mints. A monitor that spends the budget it is reporting on is worse
than no monitor, and the two class names are one word apart.

Second: every section catches its own exceptions and degrades to an `error`
field. The corpus is on an SMB share that goes away sometimes, and a share
wobble must cost one card on the page, not the whole page.

Third: check the tracker.gg stand-down before making a tracker.gg request. This
job and the nightly crawl leave from one address and share one reputation, so a
refresh that fires into a live ban can extend it — and the cost lands on the
crawl, which loses a night, rather than here, which loses ten player cards.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "HirezAPI"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "match_data_collector"))

import manifest as manifest_module  # noqa: E402
import match_storage  # noqa: E402
import paths  # noqa: E402
import roster  # noqa: E402
from game import Game  # noqa: E402
from god_types import GodId  # noqa: E402
from HirezAPI import QueueId  # noqa: E402
from queue_stats import QueueStats  # noqa: E402
from smite2 import clearance as clearance_module  # noqa: E402
from smite2 import cooldown as cooldown_module  # noqa: E402
from smite2 import egress as egress_module  # noqa: E402
from smite2 import last_run as last_run_module  # noqa: E402
from smite2.provider import CLEARANCE_FILE  # noqa: E402

SCHEMA_VERSION = 1

STATUS_FILE = "status.json"
PLAYERS_FILE = "players.json"
STATS_FILE = "stats.json"

# How many gods to name in the breakdown. The tail is a long thin line of gods
# with a handful of plays each and says nothing a reader would act on.
TOP_GODS = 20

# Where the snapshots are written, and where serve.py looks for them. The same
# env var on both sides so a deployment sets it once.
SNAPSHOT_DIR_ENV = "SMITELE_WEB_SNAPSHOT_DIR"

# Which scheduled jobs the chart actually rendered, as a comma-separated list.
#
# Without this the site can report how old data is and nothing about whether
# anything is *supposed* to be refreshing it — so a job that was never enabled
# looks exactly like a job that is failing. That is not hypothetical: the Smite 2
# crawl has never been scheduled, and the first person to read the page took
# "19h stale" to mean the crawl had broken.
#
# It comes from Helm rather than from the Kubernetes API on purpose. The web tier
# and this job both run without cluster credentials, and giving a public site's
# data path the ability to list workloads to improve a label would be a bad
# trade. The chart already knows; it can just say so.
SCHEDULED_ENV = "SMITELE_SCHEDULED_JOBS"

# Between roster members in `--players`. Fourteen players at five batched calls
# each is a burst Hi-Rez answers with error pages; this spreads it over half a
# minute, which a six-hourly job does not notice.
PLAYER_PACING_SECONDS = 2.0


def snapshot_dir() -> str:
    return os.environ.get(SNAPSHOT_DIR_ENV) or os.path.join(paths.MODEL_DIR, "web")


def scheduled_jobs() -> set:
    """The job names the chart enabled, or an empty set if it said nothing.

    An empty set is reported as "unknown", never as "nothing is scheduled" —
    running this script from a checkout sets no such variable, and a local run
    must not make the site claim every pipeline is switched off.
    """
    raw = os.environ.get(SCHEDULED_ENV, "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def section(name: str, produce: Callable[[], Any]) -> Any:
    """Run one section, or record why it could not run.

    Broad by design. A section is a read of somebody else's filesystem or API,
    and the list of ways those fail is not one worth enumerating — what matters
    is that the failure lands in the field it belongs to instead of in the exit
    code.
    """
    try:
        return produce()
    except Exception as error:  # noqa: BLE001
        print(f"snapshot: {name} failed: {error}", flush=True)
        return {"error": f"{type(error).__name__}: {error}"}


# --- data liveness ---------------------------------------------------------


def corpus_section(*directories: str) -> Dict[str, Any]:
    """How deep the corpus is and when it last grew.

    Only the newest file is stat()ed. `corpus_paths` already walks the tree —
    3,300 files on a network share — and stat()ing every one of them four times
    an hour to produce a total byte count nobody reads would turn a cheap job
    into a slow one.
    """
    found = match_storage.corpus_paths(*directories)
    if not found:
        return {"files": 0, "newest": None, "newest_at": None}

    # Sorted by basename, and a basename is `match_details_YYYY-MM-DD.parquet`,
    # so the last one is the most recent day rather than the most recent write.
    newest = found[-1]
    try:
        stat = os.stat(newest)
        newest_at, size = stat.st_mtime, stat.st_size
    except OSError:
        newest_at, size = None, None

    return {
        "files": len(found),
        "newest": os.path.basename(newest),
        "newest_at": newest_at,
        "newest_bytes": size,
    }


def aggregate_section(directory: str) -> Dict[str, Any]:
    """What the last aggregate run folded in, from the manifest it left."""
    stored = manifest_module.read(directory)
    if stored is None:
        return {"built": None, "newest": None, "files": 0, "rows": 0}

    counted = [e.rows for e in stored.entries if e.rows > 0]
    unknown = sum(1 for e in stored.entries if e.rows <= 0)

    return {
        "built": stored.built.isoformat() if stored.built else None,
        "newest": stored.newest.isoformat() if stored.newest else None,
        "files": len(stored.entries),
        # None, not 0, when nothing could be counted. Smite 1's manifest predates
        # row counting and carries UNKNOWN (-1) on all 3,306 entries, which summed
        # to a confident "0 rows" — indistinguishable from an aggregate built over
        # an empty corpus, and the one number on that card anybody would act on.
        "rows": sum(counted) if counted else None,
        "unknown_rows": unknown,
    }


def model_section(directory: str) -> Dict[str, Any]:
    """When the trainer last wrote a model, if it ever has."""
    out: Dict[str, Any] = {}
    for name in ("model.npz", "candidates.npz"):
        try:
            stat = os.stat(os.path.join(directory, name))
            out[name] = {"at": stat.st_mtime, "bytes": stat.st_size}
        except OSError:
            out[name] = None
    return out


def crawl_section(state_dir: str) -> Dict[str, Any]:
    """The Smite 2 crawl's own state: who it knows and what it has seen.

    Read with `read_frame_columns` rather than whole — the frontier carries ten
    columns and this needs three of them, and it is read every fifteen minutes.
    """
    out: Dict[str, Any] = {}

    frontier_path = os.path.join(state_dir, "frontier.parquet")
    if os.path.exists(frontier_path):
        frame = match_storage.read_frame_columns(
            frontier_path, ["last_queried", "visits", "barren_visits"]
        )
        queried = frame["last_queried"].dropna()
        queried = queried[queried != ""]
        out["frontier"] = {
            "players": int(len(frame)),
            "unvisited": int((frame["visits"] == 0).sum()),
            "last_queried": str(queried.max()) if len(queried) else None,
        }
    else:
        out["frontier"] = None

    seen_path = os.path.join(state_dir, "seen_matches.parquet")
    if os.path.exists(seen_path):
        seen = match_storage.read_frame_columns(seen_path, ["date"])
        out["matches_collected"] = int(len(seen))
    else:
        out["matches_collected"] = 0

    return out


# --- API liveness ----------------------------------------------------------


async def hirez_section(provider) -> Dict[str, Any]:
    """Hi-Rez's own two answers about itself.

    `gethirezserverstatus` has been defined in the client since the beginning
    and called by nothing. `getdataused` was reachable only through an
    owner-only `$usage` text command. Both are one request.
    """
    out: Dict[str, Any] = {}

    try:
        used = await provider.get_data_used()
        row = used[0] if isinstance(used, list) and used else used or {}
        out["quota"] = {
            "requests_today": int(row.get("Total_Requests_Today", 0)),
            "requests_limit": int(row.get("Request_Limit_Daily", 0)),
            "sessions_today": int(row.get("Total_Sessions_Today", 0)),
            "sessions_limit": int(row.get("Session_Cap", 0)),
            "active_sessions": int(row.get("Active_Sessions", 0)),
            "concurrent_limit": int(row.get("Concurrent_Sessions", 0)),
        }
    except Exception as error:  # noqa: BLE001
        out["quota"] = {"error": f"{type(error).__name__}: {error}"}

    try:
        status = await provider.get_hirez_server_status()
        out["servers"] = [
            {
                "platform": row.get("platform"),
                "environment": row.get("environment"),
                "status": row.get("status"),
                "version": row.get("version"),
                "limited_access": bool(row.get("limited_access")),
                "entry_datetime": row.get("entry_datetime"),
            }
            for row in (status or [])
        ]
    except Exception as error:  # noqa: BLE001
        out["servers"] = {"error": f"{type(error).__name__}: {error}"}

    return out


def tracker_section(state_dir: str) -> Dict[str, Any]:
    """Whether tracker.gg is currently refusing us, and why.

    Two independent things, kept apart here exactly as they are kept apart on
    disk. A stand-down is the API refusing to serve us and lifts on a deadline;
    a clearance backoff is the challenge solver having failed too often and is
    about cookies. Either one stops a crawl, and the fix for one is not the fix
    for the other, so a page that merged them would send someone to the wrong
    lever.

    Every read here is a `load()`/`read()`. Nothing in this function mints.
    """
    identity = egress_module.identity()

    cooldown = cooldown_module.Cooldown(
        os.path.join(state_dir, cooldown_module.FILE_NAME), egress=identity
    )
    standdown = cooldown.read()

    store = clearance_module.ClearanceStore(
        os.path.join(state_dir, CLEARANCE_FILE), egress=identity
    )
    state = store.load()
    cookie = state.clearance

    now = time.time()
    return {
        "egress": identity,
        "standdown": {
            "active": standdown.active,
            "until": standdown.until,
            "remaining_seconds": standdown.remaining,
            "reason": standdown.reason,
            "armed_at": standdown.armed_at,
        },
        "clearance": {
            # `mints` is already trimmed to the last 24h by the store, so this
            # is the number the daily cap is actually compared against.
            "mints_today": len(state.mints),
            "mints_limit": clearance_module.MAX_MINTS_PER_DAY,
            "blocked": state.blocked_until > now,
            "blocked_until": state.blocked_until,
            "cookie": (
                {
                    "issued_at": cookie.issued_at,
                    "age_seconds": cookie.age_seconds,
                    "last_ok": cookie.last_ok,
                    "observed_ip": cookie.observed_ip,
                }
                if cookie
                else None
            ),
        },
    }


# --- what is actually in the corpus ----------------------------------------


def _god_stats(model_dir: str):
    """The aggregate's per-god table, or None if it has never been built.

    83KB for Smite 1 and 37KB for Smite 2 — the whole point of reading this
    rather than the corpus, which is 3,300 files and tens of gigabytes.
    """
    path = os.path.join(model_dir, "god_stats.parquet")
    if not os.path.exists(path):
        return None
    import pandas as pd  # noqa: PLC0415

    return pd.read_parquet(path)


def stats_section(game: Game, model_dir: str, names, icon=None) -> Dict[str, Any]:
    """How the corpus breaks down by queue, by god and by role.

    A note on what `plays` counts, because it is easy to report wrong: the
    aggregate groups by (god, queue, role, mmr) and counts *player records*, not
    matches. Ten of them come from one game. Summing gives "times a god was
    played", which is the honest label and also the more interesting number for
    the god breakdown — so nothing here divides by ten and calls it matches.
    """
    frame = _god_stats(model_dir)
    if frame is None or frame.empty:
        return {"built": False}

    god_name, queue_name = names

    def rollup(column: str, label) -> List[Dict[str, Any]]:
        grouped = frame.groupby(column, observed=True)[["plays", "wins"]].sum()
        rows = [
            {
                "key": str(key),
                "name": label(key),
                "plays": int(row.plays),
                "wins": int(row.wins),
                "win_percent": round(float(row.wins) / float(row.plays), 4)
                if row.plays
                else None,
            }
            for key, row in grouped.iterrows()
        ]
        return sorted(rows, key=lambda r: r["plays"], reverse=True)

    def with_icon(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if icon is None:
            return rows
        for row in rows:
            url = icon(row["key"])
            if url:
                row["icon"] = url
        return rows

    gods = with_icon(rollup("GodId", god_name))
    queues = rollup("match_queue_id", queue_name)
    roles = rollup("Role", lambda role: str(role))

    # The same god ranking again, once per queue. This is what makes the page
    # filterable rather than one undifferentiated list of 130 gods: the most
    # played god in Arena is not the most played in Ranked Conquest, and a
    # single global ranking hides exactly that.
    #
    # Capped per queue rather than sent whole — 130 gods × 9 queues is a table
    # nobody scrolls, and the tail is gods with a handful of plays.
    by_queue: Dict[str, List[Dict[str, Any]]] = {}
    for queue_id, rows in frame.groupby("match_queue_id", observed=True):
        grouped = rows.groupby("GodId", observed=True)[["plays", "wins"]].sum()
        ranked = sorted(
            (
                {
                    "key": str(god_id),
                    "name": god_name(god_id),
                    "plays": int(row.plays),
                    "wins": int(row.wins),
                    "win_percent": round(float(row.wins) / float(row.plays), 4)
                    if row.plays
                    else None,
                }
                for god_id, row in grouped.iterrows()
            ),
            key=lambda r: r["plays"],
            reverse=True,
        )
        by_queue[str(queue_id)] = with_icon(ranked[:TOP_GODS])

    total_plays = int(frame["plays"].sum())
    high_mmr = int(frame.loc[frame["HighMmr"], "plays"].sum())

    return {
        "built": True,
        "total_plays": total_plays,
        "high_mmr_plays": high_mmr,
        "distinct_gods": len(gods),
        "distinct_queues": len(queues),
        "queues": queues,
        "roles": roles,
        "gods": gods[:TOP_GODS],
        "gods_total": len(gods),
        "gods_by_queue": by_queue,
    }


def matches_per_day(corpus_dir: str, archive_dir: str, queue_name) -> Dict[str, Any]:
    """Smite 2's collected matches per day, in total and per queue.

    Read from the corpus rather than from `seen_matches.parquet`, which knows
    the day but not the mode. Two columns per file and the date comes from the
    filename, so this is 78 small reads rather than a scan — and the rows are
    per *player*, so each file is deduplicated on match id before counting or
    every match would count ten times.

    Affordable for both games, which was worth measuring rather than assuming:
    Smite 1 is 3,308 files but a two-column Parquet read never touches the rest
    of the file, and a full pass measured **31 ms/file — 1.7 minutes**. The
    aggregate job takes three hours over the same corpus because it reads twenty
    columns and folds a running total; this reads two and counts.
    """
    found = match_storage.corpus_paths(corpus_dir, archive_dir)
    if not found:
        return {"all": [], "by_queue": {}, "queues": []}

    totals: Dict[str, int] = {}
    per_queue: Dict[str, Dict[str, int]] = {}

    for path in found:
        date = _day_from(os.path.basename(path))
        if not date:
            continue
        try:
            frame = match_storage.read_frame_columns(
                path, ["Match", "match_queue_id"]
            )
        except Exception as error:  # noqa: BLE001
            print(f"snapshot: could not read {path}: {error}", flush=True)
            continue
        if frame.empty or "Match" not in frame.columns:
            continue

        unique = frame.drop_duplicates(subset="Match")
        totals[date] = totals.get(date, 0) + int(len(unique))
        for queue_id, count_ in unique["match_queue_id"].value_counts().items():
            bucket = per_queue.setdefault(str(queue_id), {})
            bucket[date] = bucket.get(date, 0) + int(count_)

    def series(counts: Dict[str, int]) -> List[Dict[str, Any]]:
        return [
            {"date": date, "matches": counts[date]} for date in sorted(counts)
        ]

    # Ordered by how much of the corpus each queue is, so the filter's first
    # options are the ones with a line worth looking at.
    ordered = sorted(per_queue, key=lambda key: -sum(per_queue[key].values()))

    return {
        "all": series(totals),
        "by_queue": {key: series(per_queue[key]) for key in ordered},
        "queues": [
            {"key": key, "name": queue_name(key), "matches": sum(per_queue[key].values())}
            for key in ordered
        ],
    }


def _day_from(basename: str) -> Optional[str]:
    """`match_details_2026-08-07.parquet` → `2026-08-07`."""
    import re  # noqa: PLC0415

    found = re.search(r"(\d{4}-\d{2}-\d{2})", basename)
    return found.group(1) if found else None


async def build_stats() -> Dict[str, Any]:
    """Both games' corpus breakdowns.

    Its own mode and its own schedule. The liveness snapshot runs every fifteen
    minutes and is two API calls; this reads two Parquet tables and — for the
    names — a god catalogue per game. The aggregate behind it rebuilds once a
    day, so running this any faster than a few hours would be work for nothing.
    """
    document: Dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "games": {},
    }

    # Smite 1: names from the Hi-Rez catalogue, cached on disk between runs.
    try:
        from SmiteProvider import SmiteProvider  # noqa: PLC0415

        provider = SmiteProvider(silent=True)
        await provider.create()

        def god_name(god_id) -> str:
            try:
                return provider.gods[GodId(int(god_id))].name
            except (KeyError, ValueError):
                return f"#{god_id}"

        def queue_name(queue_id) -> str:
            try:
                return QueueId(int(queue_id)).display_name
            except ValueError:
                return f"queue {queue_id}"

        def god_icon(god_id) -> Optional[str]:
            try:
                return provider.gods[GodId(int(god_id))].icon_url or None
            except (KeyError, ValueError):
                return None

        smite_stats = section(
            "smite stats",
            lambda: stats_section(
                Game.SMITE,
                paths.game_model_dir(Game.SMITE),
                (god_name, queue_name),
                god_icon,
            ),
        )
        if isinstance(smite_stats, dict) and "error" not in smite_stats:
            smite_stats["matches_per_day"] = section(
                "smite per-day",
                lambda: matches_per_day(
                    paths.MATCH_DATA_DIR, paths.MATCH_ARCHIVE_DIR, queue_name
                ),
            )
        document["games"][Game.SMITE.value] = smite_stats
    except Exception as error:  # noqa: BLE001
        print(f"snapshot: smite stats failed: {error}", flush=True)
        document["games"][Game.SMITE.value] = {
            "error": f"{type(error).__name__}: {error}"
        }

    # Smite 2: names from the wiki, also cached on disk. Failing here must not
    # cost the Smite 1 breakdown, which is why this is a second try block and
    # not a second statement in the first one.
    try:
        from smite2.provider import Smite2Provider  # noqa: PLC0415
        from smite2.queues import Smite2QueueId  # noqa: PLC0415

        provider2 = Smite2Provider(silent=True)
        await provider2.create()

        def god_name2(god_id) -> str:
            god = provider2.gods.get(int(god_id))
            return god.name if god else f"#{god_id}"

        def queue_name2(queue_id) -> str:
            try:
                return Smite2QueueId(int(queue_id)).display_name
            except ValueError:
                return f"queue {queue_id}"

        def god_icon2(god_id) -> Optional[str]:
            god = provider2.gods.get(int(god_id))
            return getattr(god, "icon_url", None) or None if god else None

        state_dir = paths.game_model_dir(Game.SMITE_2)
        stats = section(
            "smite2 stats",
            lambda: stats_section(
                Game.SMITE_2, state_dir, (god_name2, queue_name2), god_icon2
            ),
        )
        if isinstance(stats, dict) and "error" not in stats:
            stats["matches_per_day"] = section(
                "smite2 per-day",
                lambda: matches_per_day(
                    paths.game_match_data_dir(Game.SMITE_2),
                    paths.game_match_archive_dir(Game.SMITE_2),
                    queue_name2,
                ),
            )
        document["games"][Game.SMITE_2.value] = stats
    except Exception as error:  # noqa: BLE001
        print(f"snapshot: smite2 stats failed: {error}", flush=True)
        document["games"][Game.SMITE_2.value] = {
            "error": f"{type(error).__name__}: {error}"
        }

    return document


# --- the two documents -----------------------------------------------------


async def build_status() -> Dict[str, Any]:
    smite1_model = paths.game_model_dir(Game.SMITE)
    smite2_model = paths.game_model_dir(Game.SMITE_2)

    jobs = scheduled_jobs()

    def schedule_for(collector: str, aggregate: str) -> Dict[str, Any]:
        """Whether this game's two pipelines are on a schedule at all.

        None rather than False when the chart told us nothing, so "we don't
        know" and "it is switched off" stay distinguishable — they call for
        different reactions and the page renders them differently.
        """
        if not jobs:
            return {"collector": None, "aggregate": None}
        return {"collector": collector in jobs, "aggregate": aggregate in jobs}

    document: Dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "scheduled": sorted(jobs) if jobs else None,
        "games": {
            Game.SMITE.value: {
                "corpus": section(
                    "smite corpus",
                    lambda: corpus_section(
                        paths.MATCH_DATA_DIR, paths.MATCH_ARCHIVE_DIR
                    ),
                ),
                "aggregate": section(
                    "smite aggregate", lambda: aggregate_section(smite1_model)
                ),
                "model": section("smite model", lambda: model_section(smite1_model)),
                "scheduled": schedule_for("collector", "aggregate"),
            },
            Game.SMITE_2.value: {
                "corpus": section(
                    "smite2 corpus",
                    lambda: corpus_section(
                        paths.game_match_data_dir(Game.SMITE_2),
                        paths.game_match_archive_dir(Game.SMITE_2),
                    ),
                ),
                "aggregate": section(
                    "smite2 aggregate", lambda: aggregate_section(smite2_model)
                ),
                "model": section("smite2 model", lambda: model_section(smite2_model)),
                "crawl": section("smite2 crawl", lambda: crawl_section(smite2_model)),
                "last_run": section(
                    "smite2 last run", lambda: last_run_module.read(smite2_model)
                ),
                "scheduled": schedule_for("smite2.collector", "smite2.aggregate"),
            },
        },
        "tracker": section("tracker", lambda: tracker_section(smite2_model)),
    }

    # Last, and only this one needs credentials — so a checkout with no Hi-Rez
    # keys still produces every other section rather than nothing.
    try:
        from SmiteProvider import SmiteProvider  # noqa: PLC0415

        # No teardown: the client opens a ClientSession per request rather than
        # holding one, so there is nothing here to close.
        document["hirez"] = await hirez_section(SmiteProvider(silent=True))
    except Exception as error:  # noqa: BLE001
        print(f"snapshot: hirez failed: {error}", flush=True)
        document["hirez"] = {"error": f"{type(error).__name__}: {error}"}

    return document


def _date(value) -> Optional[str]:
    """A Hi-Rez timestamp as ISO 8601 *with* its offset.

    Hi-Rez publishes UTC and `strptime` produces a naive datetime, so the offset
    has to be attached here — and it matters, because JavaScript reads a naive
    date-time as *local*. Emitting "2014-11-18T04:54:00" meant every browser
    rendered the raw UTC clock as though it were the reader's own, which is how
    a roster of account-creation times ended up looking like everyone signed up
    in the middle of the night.
    """
    if not value or value == datetime.datetime.min:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.isoformat()


async def player_document(provider, username: str) -> Dict[str, Any]:
    """One roster member's Smite 1 stats.

    Walks the same path `player_stats.py` does for a `/queue_stats` with no
    queue: resolve the name, follow the merged-account redirect, then batch the
    per-queue god rows twenty queues at a time. Deliberately the same walk —
    two implementations of "which account is this?" would eventually disagree.
    """
    ids = await provider.get_player_id_by_name(username)
    if not any(ids):
        return {"name": username, "found": False}
    if ids[0].get("privacy_flag") == "y":
        # Not an error. A hidden profile is a choice, and the page should say
        # so rather than show a name with an exception next to it.
        return {"name": username, "found": True, "private": True}

    from player import Player, PlayerId  # noqa: PLC0415

    player_id = PlayerId.from_json(ids[0], provider)
    player = await player_id.get_player()
    if player is not None and player.active_player_id != player.id:
        player = await player_id.get_player(id_override=player.active_player_id)
    if player is None:
        return {"name": username, "found": False}

    totals = QueueStats()
    best_queue: Optional[str] = None
    best_win_percent = -1.0
    best_queue_matches = 0

    all_queues = list(QueueId)
    for start in range(0, len(all_queues), 20):
        rows = await provider.get_queue_stats_batch(
            player.id, (str(q.value) for q in all_queues[start : start + 20])
        )
        # A batch can come back as a list with null *elements* — not a null
        # response, which the client already retries, but individual rows Hi-Rez
        # declined to fill in. Grouping over one of those subscripts None and
        # takes the whole player down.
        rows = [row for row in (rows or []) if row]
        if not rows:
            continue
        from itertools import groupby  # noqa: PLC0415

        for queue_name, group in groupby(rows, key=lambda row: row["Queue"]):
            stats = QueueStats.from_json(group)
            # Totals count everything the account has ever done. "Best queue"
            # does not: the honest winner is otherwise "Training: Easy Bots" at
            # 100%, which is true and tells a reader nothing. The same
            # normal-or-ranked filter the `/queue_stats` command uses to build
            # its own choice list.
            competitive = _competitive(queue_name)
            totals.total_kills += stats.total_kills
            totals.total_deaths += stats.total_deaths
            totals.total_assists += stats.total_assists
            totals.total_gold += stats.total_gold
            totals.total_wins += stats.total_wins
            totals.total_losses += stats.total_losses
            totals.total_minutes += stats.total_minutes
            totals.last_played = max(totals.last_played, stats.last_played)
            if (
                competitive
                and stats.matches >= 10
                and (
                    stats.win_percent > best_win_percent
                    or (
                        stats.win_percent == best_win_percent
                        and stats.matches > best_queue_matches
                    )
                )
            ):
                best_win_percent = stats.win_percent
                best_queue = queue_name
                best_queue_matches = stats.matches

    gods = [god for god in (await provider.get_god_ranks(player.id) or []) if god]
    top_gods = sorted(
        (
            {
                "god_id": int(god["god_id"]),
                "god": _god_name(provider, int(god["god_id"])),
                "icon": _god_icon(provider, int(god["god_id"])),
                "worshippers": int(god["Worshippers"]),
                "rank": int(god["Rank"]),
                "wins": int(god["Wins"]),
                "losses": int(god["Losses"]),
                "kills": int(god["Kills"]),
                "deaths": int(god["Deaths"]),
                "assists": int(god["Assists"]),
            }
            for god in gods
        ),
        key=lambda row: row["worshippers"],
        reverse=True,
    )[:10]

    return {
        "name": player.name or username,
        "found": True,
        "private": False,
        "avatar_url": _secure(player.avatar_url),
        "level": player.level,
        "platform": player.platform,
        "region": player.region,
        "clan": player.clan_name or None,
        "created_at": _date(player.created_datetime),
        "last_login_at": _date(player.last_login_datetime),
        "last_played_at": _date(totals.last_played),
        "leaves": player.leaves,
        "total_worshippers": player.total_worshippers,
        "totals": {
            "kills": totals.total_kills,
            "deaths": totals.total_deaths,
            "assists": totals.total_assists,
            "gold": totals.total_gold,
            "wins": totals.total_wins,
            "losses": totals.total_losses,
            "matches": totals.matches,
            "minutes": totals.total_minutes,
            "kda": round(totals.total_avg_kda, 3),
            "win_percent": round(totals.win_percent, 4),
        },
        "best_queue": (
            {
                "queue": best_queue,
                "win_percent": round(best_win_percent, 4),
                "matches": best_queue_matches,
            }
            if best_queue
            else None
        ),
        "ranked": [
            {
                "queue": queue.display_name,
                "tier": stat.tier.display_name,
                "tier_id": stat.tier.value,
                "mmr": round(stat.mmr, 1),
                "points": stat.points,
                "wins": stat.wins,
                "losses": stat.losses,
                "leaves": stat.leaves,
            }
            for queue, stat in sorted(
                player.ranked_stats.items(), key=lambda pair: pair[0].name
            )
        ],
        "top_gods": top_gods,
    }


# `getqueuestats` labels each row `"<category>: <mode>"` — "Normal: Conquest",
# "Custom: Joust", "Training: Easy Bots (Solo)". The category is the whole
# signal, and it is not the enum's `display_name` ("Conquest"), so the name
# cannot be parsed back through `QueueId` the way a slash command option can.
#
# Excluded rather than allow-listed on purpose: a mode that ships next patch
# should count toward someone's best queue without a code change, where a new
# *bot* mode is the rarer event and shows up under one of these prefixes.
UNCOMPETITIVE_QUEUE_PREFIXES = ("custom", "training", "practice", "tutorial", "bot")


def _competitive(queue_name: str) -> bool:
    """Whether a win rate in this queue means anything.

    Totals count every match the account has played. "Best queue" should not:
    left unfiltered the honest answer is "Training: Easy Bots (Solo), 100%",
    which is true and useless.
    """
    category, _, _mode = (queue_name or "").partition(":")
    return category.strip().lower() not in UNCOMPETITIVE_QUEUE_PREFIXES


def _secure(url: Optional[str]) -> Optional[str]:
    """Hi-Rez hands back avatar URLs on plain http.

    The site is served over https, so a browser blocks those as mixed content
    and the image silently never loads — which looks exactly like a player who
    has no avatar at all. Their CDN serves the same path over TLS, so this is a
    scheme swap and not a proxy.
    """
    if not url:
        return None
    return url.replace("http://", "https://", 1) if url.startswith("http://") else url


def _god_icon(provider, god_id: int) -> Optional[str]:
    """That god's portrait, for players with no avatar of their own.

    Only one of the fourteen has ever set one, so without a fallback the roster
    is thirteen empty squares and a photograph.
    """
    try:
        return provider.gods[GodId(god_id)].icon_url or None
    except (KeyError, ValueError):
        return None


def _god_name(provider, god_id: int) -> Optional[str]:
    try:
        return provider.gods[GodId(god_id)].name
    except (KeyError, ValueError):
        return None


async def smite2_player(provider, entry: str) -> Dict[str, Any]:
    """One roster member's Smite 2 profile, at the same depth as Smite 1's.

    Two requests: the profile — which carries the display handle, the Steam
    avatar *and* every gamemode segment — and the per-god segments. Deliberately
    two and not three: the profile's segments are the same rows
    `segments(kind="gamemode")` would fetch again, and tracker.gg refused this
    address after ~300 requests in a single run, so every request spent here is
    one the nightly crawl does not get.
    """
    from smite2.players import parse_player  # noqa: PLC0415

    platform, handle = parse_player(entry)
    found = await provider.players.overview(platform, handle)
    if not found:
        return {"id": entry, "platform": platform, "handle": handle, "found": False}

    info, modes = found
    modes = [mode for mode in modes if mode.matches]

    total_matches = sum(mode.matches for mode in modes)
    total_wins = sum(mode.wins for mode in modes)

    def total(stat: str) -> int:
        return int(sum(mode.stats.get(stat) or 0 for mode in modes))

    kills, deaths, assists = total("kills"), total("deaths"), total("assists")

    # Best mode on the same terms Smite 1 uses: a real win rate needs a real
    # sample, so a mode with three games cannot win it.
    ranked_modes = [m for m in modes if m.stats.get("skillRating")]
    best_rated = max(
        ranked_modes, key=lambda m: m.stats.get("skillRating") or 0, default=None
    )
    eligible = [mode for mode in modes if mode.matches >= 10]
    best_mode = max(eligible, key=lambda m: m.win_rate, default=None)

    gods: List[Any] = []
    try:
        gods = await provider.players.segments(platform, handle, "god")
    except Exception as error:  # noqa: BLE001
        # A missing god breakdown costs one panel, not the whole player.
        print(f"snapshot: smite2 gods for {handle} failed: {error}", flush=True)

    top_gods = sorted(
        (god for god in gods if god.matches),
        key=lambda god: -god.matches,
    )[:10]

    return {
        "id": entry,
        "platform": platform,
        "handle": handle,
        "found": True,
        # The only place a Smite 2 player has a readable name or a picture.
        "name": info.get("platformUserHandle") or handle,
        "avatar_url": info.get("avatarUrl") or None,
        "matches": total_matches,
        "wins": total_wins,
        "losses": total_matches - total_wins,
        "win_percent": round(total_wins / total_matches, 4) if total_matches else None,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kda": round((kills + assists / 2) / (deaths or 1), 3),
        "damage": total("damage"),
        "gold": total("goldEarned"),
        "minutes": round(total("timePlayed") / 60) if total("timePlayed") else None,
        "skill_rating": round(best_rated.stats["skillRating"]) if best_rated else None,
        "peak_skill_rating": (
            round(best_rated.stats["peakSkillRating"])
            if best_rated and best_rated.stats.get("peakSkillRating")
            else None
        ),
        "best_mode": (
            {
                "name": best_mode.name,
                "win_percent": round(best_mode.win_rate, 4),
                "matches": best_mode.matches,
            }
            if best_mode
            else None
        ),
        "modes": [
            {
                "name": mode.name,
                "matches": mode.matches,
                "wins": mode.wins,
                "losses": mode.losses,
                "win_percent": round(mode.win_rate, 4),
                "kda": round(mode.kda, 3),
                "skill_rating": (
                    round(mode.stats["skillRating"])
                    if mode.stats.get("skillRating")
                    else None
                ),
            }
            for mode in sorted(modes, key=lambda m: -m.matches)[:10]
        ],
        "top_gods": [
            {
                "god": god.name,
                "icon": god.image_url,
                "matches": god.matches,
                "wins": god.wins,
                "losses": god.losses,
                "win_percent": round(god.win_rate, 4),
                "kda": round(god.kda, 3),
            }
            for god in top_gods
        ],
    }


async def build_smite2_players() -> Dict[str, Any]:
    """The roster's Smite 2 stats, or the reason there aren't any.

    THE GUARD BELOW IS THE IMPORTANT PART. This job and the nightly crawl leave
    from the same address and draw on the same reputation, and the crawl already
    refuses to start inside a recorded stand-down for good reason: firing into a
    live ban collects nothing and can extend it. A player refresh that ignored
    the same deadline would do exactly that, and the damage would land on the
    crawl rather than here — the site would lose ten player cards and the corpus
    would lose a night.

    So it reads the stand-down first, and skips rather than asks.
    """
    if not roster.DISCORD_TO_SMITE2:
        return {"skipped": "no Smite 2 roster configured", "players": []}

    state_dir = paths.game_model_dir(Game.SMITE_2)
    cooldown = cooldown_module.Cooldown(
        os.path.join(state_dir, cooldown_module.FILE_NAME)
    )
    standdown = cooldown.read()
    if standdown.active:
        message = (
            f"tracker.gg stand-down, "
            f"{cooldown_module.describe(standdown.remaining)} left"
        )
        print(f"snapshot: skipping Smite 2 players — {message}", flush=True)
        return {
            "skipped": message,
            "reason": standdown.reason,
            "until": standdown.until,
            "players": [],
        }

    from smite2.provider import Smite2Provider  # noqa: PLC0415

    provider = Smite2Provider(silent=True)
    await provider.create()

    players: List[Dict[str, Any]] = []
    for index, entry in enumerate(roster.SMITE2_PLAYERS):
        if index:
            await asyncio.sleep(PLAYER_PACING_SECONDS)
        try:
            players.append(await smite2_player(provider, entry))
        except Exception as error:  # noqa: BLE001
            print(
                f"snapshot: smite2 player {entry} failed: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
            players.append(
                {"id": entry, "found": False, "error": f"{type(error).__name__}: {error}"}
            )

    return {"skipped": None, "players": players}


async def build_players() -> Dict[str, Any]:
    from SmiteProvider import SmiteProvider  # noqa: PLC0415

    provider = SmiteProvider(silent=True)
    # Needed for god names on the worshipper table. Two cached API calls.
    await provider.create()

    players: List[Dict[str, Any]] = []
    for index, username in enumerate(roster.SMITE_USERNAMES):
        # Hi-Rez fails a sustained burst of batched calls rather than queueing
        # it — measured as an HTML error page or a null body on roughly a third
        # of the roster when run flat out. The client retries those now, but not
        # provoking them is cheaper than recovering from them, and a job that
        # runs every six hours has no reason to hurry.
        if index:
            await asyncio.sleep(PLAYER_PACING_SECONDS)
        try:
            players.append(await player_document(provider, username))
        except Exception as error:  # noqa: BLE001
            # One bad lookup out of fourteen must not cost the other thirteen.
            # A partial file beats yesterday's whole one.
            #
            # The type is in the message because several of these arrive with an
            # empty str() — a bare TimeoutError logged as "failed: " and said
            # nothing about what to fix.
            print(
                f"snapshot: player {username} failed: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
            players.append(
                {
                    "name": username,
                    "found": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    try:
        smite2 = await build_smite2_players()
    except Exception as error:  # noqa: BLE001
        print(f"snapshot: smite2 players failed: {error}", flush=True)
        smite2 = {"skipped": f"{type(error).__name__}: {error}", "players": []}

    return {
        "version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "players": players,
        "smite2": smite2,
    }


def write(directory: str, name: str, document: Dict[str, Any]) -> str:
    """Atomically, because serve.py reads this file without coordinating."""
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, name)
    partial = f"{target}.partial"
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    os.replace(partial, target)
    return target


async def run(args) -> int:
    directory = args.out or snapshot_dir()

    if args.stats:
        document = await build_stats()
        target = write(directory, STATS_FILE, document)
        built = [
            name
            for name, game in document["games"].items()
            if isinstance(game, dict) and game.get("built")
        ]
        print(f"Wrote {target} — {', '.join(built) or 'no aggregates built'}", flush=True)
        return 0

    if args.players:
        document = await build_players()
        target = write(directory, PLAYERS_FILE, document)
        found = sum(1 for p in document["players"] if p.get("found"))
        print(f"Wrote {target} — {found}/{len(document['players'])} players", flush=True)
        return 0

    document = await build_status()
    target = write(directory, STATUS_FILE, document)
    print(f"Wrote {target}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--players",
        action="store_true",
        help="refresh the roster's Smite 1 stats instead of the liveness "
        "snapshot. About 120 Hi-Rez requests, so it runs on its own, much "
        "slower schedule and writes its own file.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="break the corpus down by queue, god and role from the aggregate's "
        "per-god table. Reads two small Parquet files and a god catalogue per "
        "game; the aggregate behind it rebuilds daily, so this has its own, "
        "slower schedule.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=f"directory to write into; defaults to ${SNAPSHOT_DIR_ENV}",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
