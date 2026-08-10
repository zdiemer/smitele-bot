"""Turning a set of candidate builds into the conditional tree.

`/optimize` has the presentation worth keeping — SHARED, then AHEAD and BEHIND,
drawn as one image with running gold totals — and `/build` has the algorithm
worth keeping, since it reads what actually won rather than what should work.
They were welded to each other: `GeneratedBuild.path` was documented as
something only `/optimize` produced, because the fork was built by re-running
the optimizer at two other balances, and the corpus path has no balance to
re-run at.

It does not need one. `build_path.fork` takes any three item lists and works out
the shared opening from what they agree on, so the only question is which three.
The optimizer answers it by re-scoring; the corpus answers it by looking further
down a ranking it already computed:

    neutral   the highest-ranked build, which is what /build showed before
    ahead     the highest-ranked build more offensive than neutral
    behind    the highest-ranked build more defensive than neutral

That is a genuinely better tree than the optimizer's, because all three branches
are builds people actually ran and won with, rather than three points on a
stat-model curve. When the candidates are too alike to split — a god with one
settled build — there is no fork, `build_path.fork` returns a path with no
branches, and `build_path_image.render` returns None, which the embed already
handles by falling back to the plain grid. A god with one real build should be
shown one build.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

import build_path

# How far down the ranking to look for a branch. Far enough to find a real
# disagreement, near enough that AHEAD is still a build worth running: the
# twentieth-best build is not "what to buy when winning", it is just worse.
BRANCH_DEPTH: int = 12

# How much more offensive a build has to be before it counts as the aggressive
# branch. Below this the two are the same build with one item swapped, and a
# tree that forks over that is noise drawn as a diagram.
MIN_SPLIT: float = 0.06


DEFENSIVE_WORDS = ("PROTECTION", "HEALTH", "TENACITY", "PLATED", "DAMPENING")
OFFENSIVE_WORDS = (
    "POWER",
    "STRENGTH",
    "INTELLIGENCE",
    "PENETRATION",
    "CRITICAL",
    "ATTACK_SPEED",
    "LIFESTEAL",
)


def offensive_share(items: Sequence[Any]) -> Optional[float]:
    """What fraction of a build's stat total is spent on killing.

    Read straight off `item.item_properties`, which both games populate with
    the same `(attribute, flat, percent)` shape, so this needs neither stat
    model and cannot disagree with itself across games. That matters more than
    the precision does: the number is only ever compared against another build
    for the same god in the same request, never against a threshold.

    Deliberately coarse for the same reason. This orders three builds that the
    ranking already decided are good; it is not deciding whether they are.
    Matching on attribute *names* rather than enumerating members is what lets
    one function serve two enums that share no members — Smite 1 has
    PHYSICAL_POWER, Smite 2 has STRENGTH, and both end up on the same side.
    """
    offence = defence = 0.0
    for item in items:
        for prop in getattr(item, "item_properties", None) or []:
            name = getattr(prop.attribute, "name", str(prop.attribute)).upper()
            # Percent values are fractions and flats are points, so a build
            # with 40% penetration would otherwise weigh less than one point of
            # health. Scaled to the same order before being added.
            value = float(prop.flat_value or 0.0) + 100.0 * float(
                prop.percent_value or 0.0
            )
            if not value:
                continue
            if any(word in name for word in DEFENSIVE_WORDS):
                defence += value
            elif any(word in name for word in OFFENSIVE_WORDS):
                offence += value

    total = offence + defence
    return offence / total if total > 0 else None


def protection_split(items: Sequence[Any]) -> Optional[float]:
    """The share of a build's protections that are physical.

    None when it bought none, which is a real answer rather than a missing
    one — a full damage build has no protection split to align with anything.
    """
    physical = magical = 0.0
    for item in items:
        for prop in getattr(item, "item_properties", None) or []:
            name = getattr(prop.attribute, "name", str(prop.attribute)).upper()
            value = float(prop.flat_value or 0.0)
            if "PHYSICAL_PROTECTION" in name:
                physical += value
            elif "MAGICAL_PROTECTION" in name:
                magical += value

    total = physical + magical
    return physical / total if total > 0 else None


def matchup_fit(
    items: Sequence[Any],
    context,
    carries_anti_heal: Optional[Callable[[Sequence[Any]], bool]] = None,
) -> float:
    """How well a build suits the lobby, on a scale where zero is indifferent.

    This is the whole trick for using a matchup with `/build`. The corpus knows
    which builds win and knows nothing about who they were played against; the
    lobby knows who you are against and nothing about what wins. Trying to
    combine them by *filtering* the corpus is what the old raw-frame path did,
    and it does not survive contact with reality: the aggregate does not carry
    enemy composition, so filtering means scanning every player row, which
    Smite 2 cannot do at all.

    Scoring composes instead. The ranking decides which builds are good, and
    this decides which of the good ones to show. A build is never invented or
    excluded because of the lobby — it is reordered — so the answer stays
    something that actually won.

    Deliberately small in magnitude. It breaks ties between builds the ranking
    already considers close, and must not promote a mediocre build for having
    the right protections.
    """
    if context is None or not getattr(context, "known", False):
        return 0.0

    score = 0.0

    # Anti-heal against a healer. The largest single thing a lobby can tell
    # you, and the one the stat models both treat as a requirement rather than
    # a preference.
    if getattr(context, "wants_anti_heal", False) and carries_anti_heal is not None:
        try:
            if carries_anti_heal(items):
                score += 1.0
        except Exception:  # noqa: BLE001 — an unreadable passive is not a crash
            pass

    # Protections aimed at the damage actually being dealt. `physical_share` is
    # 0.5 when the lobby is unknown, which makes this term zero rather than a
    # nudge in an arbitrary direction.
    share = protection_split(items)
    wanted = getattr(context, "physical_share", 0.5)
    if share is not None:
        # Perfectly aligned scores 0.5, perfectly wrong scores -0.5.
        score += 0.5 - abs(share - wanted)

    return score


def branches(
    candidates: Sequence[Dict],
    resolve: Callable[[Sequence[int]], Optional[List[Any]]],
) -> Optional[Dict[str, List[Any]]]:
    """Pick the neutral, aggressive and defensive builds out of a ranking.

    `resolve` turns recorded item ids into catalogue items, and returns None for
    a build referencing something that has since been removed — which is
    common enough across a patch that dropping those quietly is the only way
    the tree survives an item rotation.
    """
    resolved: List[List[Any]] = []
    for candidate in candidates[:BRANCH_DEPTH]:
        items = resolve(candidate.get("items") or [])
        if items:
            resolved.append(items)
    if not resolved:
        return None

    neutral = resolved[0]
    middle = offensive_share(neutral)
    if middle is None:
        return None

    ahead = behind = None
    for items in resolved[1:]:
        share = offensive_share(items)
        if share is None:
            continue
        if ahead is None and share - middle >= MIN_SPLIT:
            ahead = items
        elif behind is None and middle - share >= MIN_SPLIT:
            behind = items
        if ahead is not None and behind is not None:
            break

    # One branch is not a fork. Falling back to the neutral build on the missing
    # side would draw a tree whose two halves are identical, which tells the
    # reader there is a decision here when there is not.
    if ahead is None or behind is None:
        return None
    return {"neutral": neutral, "ahead": ahead, "behind": behind}


# How far a matchup may reorder the ranking. One place per unit of fit, so a
# perfectly-aimed build can climb past two the ranking put above it and no
# further. The ranking is measured against held-out win rates; this is not, so
# it gets to break ties rather than overrule them.
MATCHUP_WEIGHT: float = 2.0


def for_lobby(
    candidates: Sequence[Dict],
    resolve: Callable[[Sequence[int]], Optional[List[Any]]],
    context,
    carries_anti_heal: Optional[Callable[[Sequence[Any]], bool]] = None,
) -> Sequence[Dict]:
    """Reorder a ranking so the lobby breaks its near-ties.

    Position in the ranking is the cost, rather than the ranking's own score:
    those scores are win rates a few points apart and a lobby term would either
    vanish against them or swamp them, depending on the god. Ranks are the same
    distance apart for everyone.
    """
    if context is None or not getattr(context, "known", False) or not candidates:
        return candidates

    scored = []
    for position, candidate in enumerate(candidates):
        items = resolve(candidate.get("items") or [])
        fit = (
            matchup_fit(items, context, carries_anti_heal) if items else 0.0
        )
        scored.append((position - MATCHUP_WEIGHT * fit, position, candidate))

    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [candidate for _, _, candidate in scored]


def path_for(
    candidates: Sequence[Dict],
    resolve: Callable[[Sequence[int]], Optional[List[Any]]],
    score: Callable[[Sequence[Any]], float],
    price: Callable[[Any], int],
    opens=None,
) -> Optional["build_path.BuildPath"]:
    """The conditional tree for a corpus-ranked build, or None if it has none."""
    picked = branches(candidates, resolve)
    if picked is None:
        return None
    try:
        return build_path.fork(
            picked["neutral"],
            picked["ahead"],
            picked["behind"],
            score,
            price,
            opens=opens,
        )
    except Exception:  # noqa: BLE001 — a build always beats a drawing of one
        return None
