"""The order a build is bought in, and where it forks on how the game is going.

An optimized build used to arrive as an unordered set rendered cheapest-first.
Cheapest-first is a proxy for build order and not a good one: it says to buy a
cheap item you do not need before an expensive one that carries the god, and it
has no opinion at all about the game you are actually in.

Two things here. `order` puts a finished build into the sequence you would
really buy it in — by value per gold at each step, so what you buy next is
whatever gives the most for what it costs *given what you already own*, which
is how the decision is actually made. Cost stops being a ceiling on the finished
build and becomes part of every step.

`fork` is the other half. A build is not one plan: ahead you press, behind you
survive, and the items differ. Rather than invent a second model for that, the
fork asks the same optimizer the same question at two different balances — the
tank:damage ratio that already exists — and keeps the items all three answers
agree on as the shared opening. Where they stop agreeing is exactly where the
game state starts to matter, which is a better place to put a branch than any
fixed slot number.

Both games use this. It needs only a way to score a set of items and a way to
price one, and each game supplies its own.
"""

from __future__ import annotations

from typing import Callable, List, NamedTuple, Sequence

from item import Item

Scorer = Callable[[Sequence[Item]], float]
Pricer = Callable[[Item], int]
# Whether an item is the one you open the game on. A starter is bought first
# whatever else the build wants, so it is pinned rather than ranked: value per
# gold would happily bury it in the fourth slot, which reads as though you were
# meant to buy it there.
Opener = Callable[[Item], bool]


class Step(NamedTuple):
    """One purchase, and what the build has cost by the time it is made."""

    item: Item
    spent: int


class BuildPath(NamedTuple):
    """A build as a plan rather than a list.

    `shared` is what you buy regardless. `ahead` and `behind` are the two
    continuations, and are empty when the branches agreed all the way down —
    which happens, and is worth saying rather than inventing a disagreement.
    """

    shared: List[Step]
    ahead: List[Step]
    behind: List[Step]

    @property
    def forks(self) -> bool:
        return bool(self.ahead or self.behind)

    @property
    def default(self) -> List[Item]:
        """One build, in order — the shared opening then the ahead branch.

        Ahead rather than behind because it is the build the optimizer picks
        when nothing is going wrong, and because a plan has to render as *some*
        six items in the embed's image strip.
        """
        return [step.item for step in self.shared + self.ahead]


def order(
    items: Sequence[Item], score: Scorer, price: Pricer, opens: Opener = None
) -> List[Step]:
    """A finished build, in the sequence it would be bought.

    Greedy on value per gold: at each step the item that adds the most score
    for what it costs, given everything already bought. That ordering is not the
    same as cheapest-first — a cheap item whose stat the build already has
    plenty of adds nothing and correctly waits — and it is not the same as
    most-expensive-last either, since an expensive item can be the best purchase
    available if it is what the build is for.

    `opens` pins the starter to the front regardless of what it scores, because
    that is the one purchase whose position is not a judgement call.
    """
    return order_from(items, [], 0, score, price, opens)


def fork(
    neutral: Sequence[Item],
    ahead: Sequence[Item],
    behind: Sequence[Item],
    score: Scorer,
    price: Pricer,
    opens: Opener = None,
) -> BuildPath:
    """Split three answers into what they agree on and where they diverge.

    The shared opening is the items all three builds contain — not the first N
    of any one of them. An item every balance wants is one the game state does
    not change your mind about, and those are exactly the ones to buy before you
    know how the game is going.

    Each branch is then its own remainder, ordered and priced as a continuation
    of the shared opening rather than from zero, so the gold figures read as
    what you will have spent by then.
    """
    shared_items = [
        item
        for item in neutral
        if any(other.id == item.id for other in ahead)
        and any(other.id == item.id for other in behind)
    ]
    shared = order(shared_items, score, price, opens)
    spent = shared[-1].spent if shared else 0
    bought = [step.item for step in shared]

    def continuation(build: Sequence[Item]) -> List[Step]:
        rest = [
            item for item in build if not any(item.id == held.id for held in bought)
        ]
        return order_from(rest, bought, spent, score, price, opens) if rest else []

    return BuildPath(shared, continuation(ahead), continuation(behind))


def order_from(
    items: Sequence[Item],
    already: Sequence[Item],
    spent: int,
    score: Scorer,
    price: Pricer,
    opens: Opener = None,
) -> List[Step]:
    """`order`, continuing a build already part-bought."""
    remaining = list(items)
    if opens is not None:
        # Whatever opens the build goes first, in the order it was given.
        remaining.sort(key=lambda item: not opens(item))
        pinned = [item for item in remaining if opens(item)]
        remaining = [item for item in remaining if not opens(item)]
    else:
        pinned = []
    bought = list(already)
    steps: List[Step] = []
    running = spent

    for item in pinned:
        bought.append(item)
        running += price(item)
        steps.append(Step(item, running))

    while remaining:
        current = score(bought)

        def gain_per_gold(item: Item) -> tuple:
            cost = max(price(item), 1)
            return ((score(bought + [item]) - current) / cost, -cost, item.name)

        best = max(remaining, key=gain_per_gold)
        remaining.remove(best)
        bought.append(best)
        running += price(best)
        steps.append(Step(best, running))

    return steps


def describe(path: BuildPath, currency: str = "gold") -> str:
    """The plan, in words, for a build embed.

    Written as an order rather than a list because that is the whole point: the
    same six items bought in a different sequence is a different build for the
    twenty minutes it takes to finish them.
    """
    if not path.shared and not path.forks:
        return ""

    lines: List[str] = []
    if path.shared:
        opening = " → ".join(
            f"**{step.item.name}** ({step.spent:,})" for step in path.shared
        )
        lines.append(f"**Build order**: {opening}")

    if not path.forks:
        return "\n".join(lines) + f"\n_Total {path.shared[-1].spent:,} {currency}._"

    if lines:
        lines.append("")
        lines.append("_Then:_")
    else:
        # Every balance wanted a different first item, so there is no opening to
        # share. Saying "then" here would be describing a step that does not
        # exist.
        lines.append("_Forks immediately:_")
    for label, steps in (("Ahead", path.ahead), ("Behind", path.behind)):
        if not steps:
            continue
        branch = " → ".join(
            f"**{step.item.name}** ({step.spent:,})" for step in steps
        )
        lines.append(f"**{label}**: {branch}")
    return "\n".join(lines)
