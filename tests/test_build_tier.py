"""A rating without a tier is Smite 2's normal state, not a broken row.

`/build god_name:ymir` for Smite 2 answered "Finding you a build with those
settings." and then never said anything else. It was not hanging — the command
raised `ValueError: 0 is not a valid TierId` and died, which Discord shows as
silence.

The cause is a guard that tests one column and then indexes an enum with a
different one. `avg_rating` and `avg_tier` are both divided by `rated_wins`, so
a source that supplies a rating and no tier makes the first truthy and the
second zero — and `TierId` starts at BRONZE_V = 1, so zero is not the lowest
tier but the absence of one. tracker.gg is exactly that source: measured across
the Smite 2 aggregate, `sum_tier` is 0.0 over all 700 rated rows while
`sum_rating` totals 2,594,583.

So this was every Smite 2 god with ranked data, not one god.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "HirezAPI"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "SmiteBot"))

pytest.importorskip("pandas")
god_builder = pytest.importorskip("god_builder")

from HirezAPI import TierId  # noqa: E402


class TestTierOrNone:
    def test_zero_is_no_tier_not_the_lowest_one(self):
        # The whole bug in one assertion: TierId(0) raises, and the caller used
        # to let it.
        with pytest.raises(ValueError):
            TierId(0)

        assert god_builder._tier_or_none(0) is None
        assert god_builder._tier_or_none(0.0) is None

    def test_a_real_average_rounds_down_to_its_tier(self):
        assert god_builder._tier_or_none(1) is TierId.BRONZE_V
        # Averages are fractional; 5.9 is still BRONZE_I, not SILVER.
        assert god_builder._tier_or_none(5.9) is TierId(5)

    @pytest.mark.parametrize("value", [None, "", "n/a", float("nan"), -1, 999])
    def test_anything_unusable_is_none_rather_than_an_exception(self, value):
        # This runs inside a Discord command with no error path of its own, so
        # raising here is indistinguishable from the bot ignoring the user.
        assert god_builder._tier_or_none(value) is None

    def test_the_highest_real_tier_still_resolves(self):
        highest = max(t.value for t in TierId)

        assert god_builder._tier_or_none(highest) is TierId(highest)
