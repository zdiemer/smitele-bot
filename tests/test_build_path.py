"""Build order and the fork on game state.

An optimized build used to arrive unordered and rendered cheapest-first, which
says to buy a cheap item you do not need before the one the god is built around.
These pin the two properties that fixes: order follows value per gold, and the
branch point is wherever the balances stop agreeing rather than a fixed slot.
"""

from __future__ import annotations

import zlib

import pytest

import build_path
from item import Item, ItemType


def _stable_id(name: str) -> int:
    """A deterministic id for a fake item or god.

    Not `hash()`: Python randomises string hashing per process, so ids
    built from it differ between runs. Scoring breaks ties on id, and a
    fake catalogue hits ties often — every item past the point a target
    saturates scores identically — so hash-derived ids made these tests
    pass or fail depending on the seed the interpreter happened to start
    with."""
    return zlib.crc32(name.encode()) % 10_000_000



def make_item(name, cost, value):
    item = Item()
    item.name = name
    item.id = _stable_id(name)
    item.tier = 3
    item.price = item.total_cost = cost
    item.active = True
    item.is_starter = False
    item.type = ItemType.ITEM
    item.item_properties = []
    item.passive = None
    item.icon_url = ""
    item.restricted_roles = []
    item.glyph = False
    item.test_value = value
    return item


def score(items):
    """Additive and saturating-free, so the ordering is the only thing tested."""
    return sum(item.test_value for item in items)


def price(item):
    return item.total_cost


class TestOrder:
    def test_orders_by_value_per_gold_not_by_price(self):
        """The whole point: cheapest-first is a proxy and a bad one."""
        cheap_and_useless = make_item("Cheap", cost=1000, value=1)
        dear_and_good = make_item("Dear", cost=2000, value=10)
        steps = build_path.order([cheap_and_useless, dear_and_good], score, price)
        assert [step.item.name for step in steps] == ["Dear", "Cheap"]

    def test_gold_accumulates_across_the_order(self):
        items = [make_item("A", 1000, 5), make_item("B", 2000, 5)]
        steps = build_path.order(items, score, price)
        assert [step.spent for step in steps] == [1000, 3000]

    def test_every_item_is_bought_exactly_once(self):
        items = [make_item(f"I{i}", 1000 + i * 100, 10 - i) for i in range(6)]
        steps = build_path.order(items, score, price)
        assert len(steps) == 6
        assert len({step.item.id for step in steps}) == 6

    def test_an_empty_build_orders_to_nothing(self):
        assert build_path.order([], score, price) == []


class TestFork:
    def test_shared_is_what_every_branch_agrees_on(self):
        common = make_item("Common", 1000, 5)
        aggressive = make_item("Aggressive", 1000, 5)
        defensive = make_item("Defensive", 1000, 5)
        path = build_path.fork(
            [common, aggressive],
            [common, aggressive],
            [common, defensive],
            score,
            price,
        )
        assert [step.item.name for step in path.shared] == ["Common"]
        assert [step.item.name for step in path.ahead] == ["Aggressive"]
        assert [step.item.name for step in path.behind] == ["Defensive"]

    def test_branch_gold_continues_from_the_shared_opening(self):
        common = make_item("Common", 1000, 5)
        later = make_item("Later", 2000, 5)
        path = build_path.fork([common, later], [common, later], [common], score, price)
        assert path.shared[-1].spent == 1000
        assert path.ahead[0].spent == 3000

    def test_agreement_all_the_way_down_does_not_invent_a_fork(self):
        items = [make_item("A", 1000, 5), make_item("B", 1000, 5)]
        path = build_path.fork(items, items, items, score, price)
        assert not path.forks
        assert len(path.shared) == 2

    def test_the_default_build_is_the_shared_opening_then_ahead(self):
        common = make_item("Common", 1000, 5)
        aggressive = make_item("Aggressive", 1000, 5)
        defensive = make_item("Defensive", 1000, 5)
        path = build_path.fork(
            [common, aggressive],
            [common, aggressive],
            [common, defensive],
            score,
            price,
        )
        assert [item.name for item in path.default] == ["Common", "Aggressive"]


class TestDescribe:
    def test_names_the_order_and_the_running_gold(self):
        items = [make_item("A", 1000, 9), make_item("B", 2000, 1)]
        path = build_path.fork(items, items, items, score, price)
        text = build_path.describe(path)
        assert "Build order" in text
        assert "**A** (1,000)" in text
        assert "**B** (3,000)" in text

    def test_says_where_it_forks(self):
        common = make_item("Common", 1000, 5)
        path = build_path.fork(
            [common, make_item("Push", 1000, 5)],
            [common, make_item("Push", 1000, 5)],
            [common, make_item("Survive", 1000, 5)],
            score,
            price,
        )
        text = build_path.describe(path)
        assert "Ahead" in text and "Behind" in text

    def test_a_fork_from_the_first_item_does_not_say_then(self):
        """There is no shared step for a "then" to follow."""
        path = build_path.fork(
            [make_item("A", 1000, 5)],
            [make_item("B", 1000, 5)],
            [make_item("C", 1000, 5)],
            score,
            price,
        )
        text = build_path.describe(path)
        assert "Then it depends" not in text
        assert "forks from the first item" in text

    def test_nothing_at_all_describes_as_nothing(self):
        assert build_path.describe(build_path.fork([], [], [], score, price)) == ""
