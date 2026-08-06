"""Turning a tracker.gg player row into a corpus record.

The column names below are Hi-Rez's, ugly ones included, and that is deliberate:
`build_aggregate`, `build_features`, `build_ranker` and `src/ml` all read these
exact names. Emitting them verbatim is what lets the entire downstream pipeline
run over Smite 2 data without knowing it exists.

The one place this is genuinely dangerous is slotting. It is tempting to read
`items` positionally — 1 starter, 2 relic, 3-8 core — and the sample says that
is mostly true and occasionally not: across 26,444 player rows there were 2,079
talents sitting at positions 3-8, 1,530 unknown entries scattered through them,
and 156 relics at position 1. Slotting by position therefore writes an Aspect
into a build slot for about 8% of players, silently, because the result still
looks like a build. Everything here selects on `equipmentType` and uses position
only to order what is left.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

from smite2.ids import item_id
from smite2.queues import Smite2QueueId, from_mode_string

# The six core build slots.
ITEM_SLOTS = 6

# What can occupy one.
CORE_EQUIPMENT = ("item-passive", "item-active")

# tracker.gg's role keys onto the aggregate's ROLE_CATEGORIES.
ROLES: Dict[str, str] = {
    "solo": "Solo",
    "jungle": "Jungle",
    "mid": "Mid",
    "middle": "Mid",
    "support": "Support",
    "carry": "Carry",
    "adc": "ADC",
}

# Ranked Conquest is the only mode with a rating, so its column is the only one
# that can be filled. The other two exist because the aggregate reads them.
RATING_COLUMNS = ("Rank_Stat_Conquest", "Rank_Stat_Duel", "Rank_Stat_Joust")
TIER_COLUMNS = ("Conquest_Tier", "Duel_Tier", "Joust_Tier")


def _stat(stats: Dict[str, Any], name: str, default=0):
    entry = stats.get(name)
    if isinstance(entry, dict):
        value = entry.get("value")
        return default if value is None else value
    return default


def _slots(entries: List[Dict[str, Any]]) -> Tuple[List[int], int, int, int, int]:
    """Six core item ids, plus the starter, relic and Aspect.

    Selected by `equipmentType` and then ordered by position — never sliced by
    position, for the reasons in the module docstring. An `unknown` entry (a hex
    id tracker.gg could not name) becomes 0 and so fails `IsFullBuild`, which is
    correct: a build with an unnameable item in it is not a build we can rank.
    """
    core: List[Tuple[int, str]] = []
    starter = relic = aspect = 0
    unknown = 0

    for entry in entries or []:
        kind = entry.get("equipmentType")
        slug = str(entry.get("id") or "")
        try:
            position = int(entry.get("position", 0))
        except (TypeError, ValueError):
            position = 0

        if kind == "unknown" or not slug:
            unknown += 1
            continue

        if kind in CORE_EQUIPMENT:
            core.append((position, slug))
        elif kind == "starter":
            starter = item_id(slug)
        elif kind == "relic":
            relic = item_id(slug)
        elif kind == "talent":
            # tracker.gg's "talent" is the wiki's Aspect: a selection-time
            # choice that changes how the god plays, at most one per player.
            aspect = item_id(slug)

    core.sort(key=lambda pair: pair[0])
    item_ids = [item_id(slug) for _position, slug in core[:ITEM_SLOTS]]
    item_ids += [0] * (ITEM_SLOTS - len(item_ids))
    return item_ids, starter, relic, aspect, unknown


def player_rows(
    match: Dict[str, Any], god_ids: Dict[str, int]
) -> Iterator[Dict[str, Any]]:
    """One corpus record per player in a match.

    `god_ids` maps whatever tracker.gg calls a god — a slug, or occasionally the
    engine's own `Gods.CuChulainn` — onto the synthetic id the wiki side uses.
    A god we cannot name yields GodId 0, which `prepare` drops.
    """
    attributes = match.get("attributes") or {}
    metadata = match.get("metadata") or {}

    match_id = str(attributes.get("id") or "")
    if not match_id:
        return

    queue = from_mode_string(
        attributes.get("gamemode"), bool(metadata.get("isRanked"))
    )
    winner = metadata.get("winningTeamId")
    timestamp = str(metadata.get("timestamp") or "")

    for segment in match.get("segments") or []:
        if segment.get("type") != "overview":
            continue
        attrs = segment.get("attributes") or {}
        meta = segment.get("metadata") or {}
        stats = segment.get("stats") or {}

        item_ids, starter, relic, aspect, unknown = _slots(meta.get("items"))
        team = meta.get("teamId")
        role = str(
            (meta.get("playedRole") or meta.get("assignedRole") or {}).get("key") or ""
        ).lower()

        rating = _stat(stats, "skillRating", 0) or 0

        record = {
            "Match": match_id,
            "GodId": god_ids.get(str(meta.get("god") or ""), 0),
            "Role": ROLES.get(role, "Unknown"),
            # A literal "Winner"/"Loser", because `prepare` compares against
            # that string rather than reading a boolean.
            "Win_Status": "Winner" if team == winner else "Loser",
            "match_queue_id": queue.value,
            "TaskForce": 1 if team == "order" else 2,
            "Winning_TaskForce": 1 if winner == "order" else 2,
            "Kills_Player": int(_stat(stats, "kills")),
            "Deaths": int(_stat(stats, "deaths")),
            "Assists": int(_stat(stats, "assists")),
            "Damage_Player": int(_stat(stats, "damage")),
            # Absent from this source. Written because features.SKILL_FEATURES
            # reads them; a constant column has zero variance, which
            # `features.encode` already guards against.
            "Account_Level": 0,
            "Mastery_Level": 0,
            # Smite-2-only, invisible to every existing reader because they all
            # project columns explicitly.
            "StarterId": starter,
            "Aspect": aspect,
            "BuildId": str(meta.get("buildId") or ""),
            "Platform": str(attrs.get("platformSlug") or ""),
            "PlayerId": str(attrs.get("platformUserIdentifier") or ""),
            "PartyId": str(meta.get("partyId") or ""),
            "MatchStartUtc": timestamp,
            "AssignedRole": str((meta.get("assignedRole") or {}).get("key") or ""),
            "PlayedRole": role,
            # Popped by the collector for its unnameable-item rate, never
            # written to the corpus. A rising rate means the wiki join has
            # drifted, which is otherwise invisible: an unnameable item just
            # makes a build fail IsFullBuild and vanish from the aggregate.
            "UnknownItems": unknown,
            "ItemSlots": len(meta.get("items") or []),
        }

        for slot in range(ITEM_SLOTS):
            record[f"ItemId{slot + 1}"] = item_ids[slot]

        # One relic, where Smite 1 has two. ActiveId2 is written as a constant 0
        # rather than omitted: Vocabulary.encode maps 0 to index 0 and the model
        # masks that out, so src/ml runs unchanged.
        record["ActiveId1"] = relic
        record["ActiveId2"] = 0

        ranked = Smite2QueueId.is_ranked(queue)
        for column in RATING_COLUMNS:
            record[column] = 0.0
        for column in TIER_COLUMNS:
            record[column] = 0.0
        if ranked and rating:
            # Only Conquest has a rating, and HighMmr keys off this column.
            record["Rank_Stat_Conquest"] = float(rating)

        yield record


def match_date(match: Dict[str, Any]) -> Optional[str]:
    """The calendar day a match was played, as `YYYY-MM-DD`.

    Rows are filed by this rather than by the day they were collected, which is
    what lets a night's crawl backfill the three days a page spans instead of
    discarding two of them.
    """
    timestamp = ((match.get("metadata") or {}).get("timestamp") or "")[:10]
    return timestamp if len(timestamp) == 10 else None
