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

# How many items a branch may differ from the build being recommended by.
#
# Without this the branches were drawn from anywhere in the top twelve, and on a
# real aggregate that is not a fork — it is three unrelated builds sharing a
# picture. Measured across the Smite 2 roster, twenty-three of eighty-eight gods
# got a diagram containing *none* of the six items listed beside it, and the
# supports were the worst of it: Bacchus's "ahead" row was a full damage warrior
# build, because the most offensive thing in a support's top twelve is whatever
# off-meta damage build happened to rank.
#
# A fork is a decision about two or three items, not about which build to play.
# Two is what makes the shared opening at least four items long, which is the
# part of the picture that is actually a plan.
MAX_DIVERGENCE: int = 2


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


def carries_tenacity(items: Sequence[Any]) -> bool:
    """Whether a build brings crowd-control reduction.

    Read off the stat line rather than off passive text, which means it sees
    Smite 2's Tenacity items and Smite 1's, and misses a passive that grants the
    same thing. Understating is the right direction: this only ever nudges the
    order of builds the ranking already likes.
    """
    for item in items:
        for prop in getattr(item, "item_properties", None) or []:
            name = getattr(prop.attribute, "name", str(prop.attribute)).upper()
            if "TENACITY" in name and (prop.flat_value or prop.percent_value):
                return True
    return False


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

    # Tenacity against a lobby that actually brings crowd control. Half the
    # roster is tagged with a lockdown spec, so this is scaled by the share
    # rather than tripped by one enemy: a team where four of five chain
    # crowd control is a different problem from a team with one stun.
    cc_share = getattr(context, "crowd_control_share", 0.0)
    if cc_share >= 0.6 and carries_tenacity(items):
        score += 0.5

    # Less defence behind a front line. `team_context` has counted allied tanks
    # since it was written and nothing on this path had ever read them, which
    # made `allies:` a documented option that changed nothing — measured at
    # zero builds of eighty-eight moved by a four-tank ally team.
    #
    # Small, and capped at two allies, for the reason the stat model caps its
    # own version: a second tank is worth less than the first, and a solo laner
    # behind a support is still a solo laner.
    allied_tanks = min(int(getattr(context, "allied_tanks", 0) or 0), 2)
    if allied_tanks:
        offence = offensive_share(items)
        if offence is not None:
            score += ALLY_TILT * allied_tanks * (offence - 0.5)

    return score


def _ids(items: Sequence[Any]) -> frozenset:
    return frozenset(item.id for item in items)


def is_kin(neutral: Sequence[Any], other: Sequence[Any]) -> bool:
    """Whether `other` is a variation of `neutral` rather than a rival to it.

    Sized off the build rather than fixed at four, so the three-item builds a
    test constructs behave like the six-item builds a corpus holds.
    """
    return len(_ids(neutral) & _ids(other)) >= max(len(neutral) - MAX_DIVERGENCE, 1)


def branches(
    candidates: Sequence[Dict],
    resolve: Callable[[Sequence[int]], Optional[List[Any]]],
) -> Optional[Dict[str, List[Any]]]:
    """Pick the recommended, aggressive and defensive builds out of a ranking.

    `resolve` turns recorded item ids into catalogue items, and returns None for
    a build referencing something that has since been removed — which is
    common enough across a patch that dropping those quietly is the only way
    the tree survives an item rotation.

    Everything here hangs off the neutral build, which is the one `/build`
    actually recommends. The branches are drawn from its kin, and the neutral
    build is returned alongside them so the drawing can include it — the two
    halves of the same fix, since the picture used to be able to contain none of
    the six items the embed listed next to it.
    """
    resolved: List[List[Any]] = []
    for candidate in candidates[:BRANCH_DEPTH]:
        items = resolve(candidate.get("items") or [])
        if items:
            resolved.append(items)
    if not resolved:
        return None

    neutral = resolved[0]
    scored = [
        (share, index, items)
        for index, items in enumerate(resolved)
        if is_kin(neutral, items) and (share := offensive_share(items)) is not None
    ]
    if len(scored) < 2:
        return None

    # The branches are measured against *each other*, not against the neutral
    # build. Requiring each to be more extreme than neutral looked reasonable
    # and was wrong in a way only the real aggregate showed: the highest-ranked
    # build for a carry or a mid is routinely a full damage build, sitting at a
    # share of 1.0, and nothing can be more offensive than that. Four of the six
    # most-played Smite 1 gods could never fork, which is why the tree almost
    # never appeared.
    #
    # Ties break toward the ranking, so the aggressive branch is the *best*
    # build among the most aggressive rather than merely the most extreme one.
    ahead = min(scored, key=lambda entry: (-entry[0], entry[1]))
    behind = min(scored, key=lambda entry: (entry[0], entry[1]))

    # Still no fork when the candidates genuinely agree: a god with one settled
    # build should be shown one build, not a tree whose halves are the same.
    if ahead[0] - behind[0] < MIN_SPLIT:
        return None
    return {"neutral": neutral, "ahead": ahead[2], "behind": behind[2]}


# How much ranking score a perfectly-aimed build may spend to climb.
#
# This used to be denominated in *rank positions* — one place per unit of fit,
# so a perfect fit climbed past two builds and no further. That reads as the
# conservative choice and is really an arbitrary one, because positions are not
# evenly spaced in anything that matters. On a pool of four thousand candidates
# whose top scores differ by ten-thousandths, two places is nothing; on a pool
# of twelve it can be a genuinely worse build. Measured on the live Smite 2
# aggregate, the position scheme moved *zero* builds of eighty-eight between an
# all-physical and an all-magical enemy team, and zero for a four-tank ally
# team. A knob that cannot move anything is not conservative, it is off.
#
# The budget below is in the same units as `ANTI_HEAL_TOLERANCE`: shrunk win
# rate, the thing the ranking maximises. Each term contributes at most ±0.5, so
# a perfectly-aimed build can climb past anything within about two points of it,
# which is the range the anti-heal sweep found indistinguishable from zero on
# held-out days. The ranking still wins any argument bigger than that.
LOBBY_BUDGET: float = 0.04

# How much the offensive/defensive tilt moves for each allied front line, before
# the budget scales it. Two allies reach the same ±0.5 as the protection term:
# which protections to buy is a fact about the enemy and how much to buy is a
# preference, but both are worth the same to a build that gets them wrong.
ALLY_TILT: float = 0.25

# How much ranking score anti-heal is allowed to cost.
#
# Everything else here is a tie-break, and anti-heal cannot be one. Measured
# across the Smite 2 roster: 87 of 88 gods have an anti-heal build somewhere in
# their pool, but the highest-ranked one sits at a median position of 18, and a
# reorder worth two or three places reached six gods. Five enemy healers moved
# two builds out of eighty-eight — the largest thing a lobby can tell you,
# arriving as nothing.
#
# So this is a promotion with a budget rather than a nudge. The budget is in
# units of the shrunk win rate the ranking maximises, and it was swept rather
# than picked: `build_eval --strategies anti_heal:0.01,anti_heal:0.02,…` prices
# each setting against held-out days, at two cutoffs.
#
#   tolerance   gods reached   Aug cutoff   Jun cutoff
#     0.01          6 / 87       -0.01pp      +0.15pp
#     0.02         23 / 87       -0.29pp      +0.34pp
#     0.03         42 / 87       -0.55pp      +0.09pp
#     0.05         74 / 87       -0.94pp      +0.25pp
#
# Read that as an upper bound on the cost, not the cost. The harness has no
# lobby, so it applies the constraint to every matchup; the bot applies it only
# against a healer, and only there does the 25% healing reduction it buys have
# anything to reduce. What the two cutoffs agree on is that the cost is small
# and does not clearly grow until well past this value — they disagree even on
# its sign — so 0.03 buys the constraint for about half the roster at a price
# the holdout cannot reliably distinguish from zero.
ANTI_HEAL_TOLERANCE: float = 0.03


def for_lobby(
    candidates: Sequence[Dict],
    resolve: Callable[[Sequence[int]], Optional[List[Any]]],
    context,
    carries_anti_heal: Optional[Callable[[Sequence[Any]], bool]] = None,
) -> Sequence[Dict]:
    """Reorder a ranking so the lobby can outweigh a small difference in score.

    The ranking's own score is the currency, and `LOBBY_BUDGET` is how much of
    it a perfect fit is allowed to spend. That is the whole difference between
    a knob that works and one that does not: scored in rank positions this moved
    nothing at all on a real aggregate.

    Anti-heal is applied on top and does not go through this, because it is a
    requirement rather than a preference — see `promote_anti_heal`.
    """
    if context is None or not getattr(context, "known", False) or not candidates:
        return candidates

    scored = []
    for position, candidate in enumerate(candidates):
        items = resolve(candidate.get("items") or [])
        fit = (
            matchup_fit(items, context, carries_anti_heal) if items else 0.0
        )
        rank = float(candidate.get("rank") or 0.0)
        # Negated so the sort is ascending on cost, with the ranking's own order
        # breaking ties — two builds the lobby cannot separate must come back in
        # the order they arrived, or the same request answers differently twice.
        scored.append((-(rank + LOBBY_BUDGET * fit), position, candidate))

    scored.sort(key=lambda entry: (entry[0], entry[1]))
    ordered = [candidate for _, _, candidate in scored]

    if getattr(context, "wants_anti_heal", False) and carries_anti_heal is not None:
        ordered = promote_anti_heal(ordered, resolve, carries_anti_heal)
    return ordered


def promote_anti_heal(
    candidates: Sequence[Dict],
    resolve: Callable[[Sequence[int]], Optional[List[Any]]],
    carries_anti_heal: Callable[[Sequence[Any]], bool],
    tolerance: float = ANTI_HEAL_TOLERANCE,
) -> Sequence[Dict]:
    """Move the best anti-heal build to the front, if one is close enough.

    Against a healer, anti-heal is not a preference the way a protection split
    is — both stat models already treat it as a requirement, setting a target at
    the 25% cap rather than a weight. The corpus path had no equivalent, so a
    lobby full of healers produced the same build as an empty one.

    "Close enough" is measured in the ranking's own score, not in positions.
    Positions are the right unit for a tie-break because they are evenly spaced;
    they are the wrong unit here, because the question is not "how many builds
    are above this one" but "how much win rate am I giving up", and on a pool of
    four thousand thin candidates those are not the same question at all.

    Returns the candidates unchanged when the leader already carries anti-heal,
    when none does, or when the closest one costs more than `tolerance`.
    """
    if not candidates:
        return candidates

    def anti_heal(candidate) -> bool:
        items = resolve(candidate.get("items") or [])
        if not items:
            return False
        try:
            return bool(carries_anti_heal(items))
        except Exception:  # noqa: BLE001 — an unreadable passive is not a crash
            return False

    if anti_heal(candidates[0]):
        return candidates

    # The best score in the pool, not the leader's — `for_lobby` has already
    # reordered by fit, so position zero is no longer the highest-ranked build
    # and scanning until the score falls below tolerance would stop early.
    leader = max(float(candidate.get("rank") or 0.0) for candidate in candidates)

    best = None
    for position, candidate in enumerate(candidates):
        rank = float(candidate.get("rank") or 0.0)
        if leader - rank > tolerance or not anti_heal(candidate):
            continue
        if best is None or rank > best[0]:
            best = (rank, position, candidate)
    if best is None:
        return candidates
    return [best[2]] + [
        other for index, other in enumerate(candidates) if index != best[1]
    ]


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
