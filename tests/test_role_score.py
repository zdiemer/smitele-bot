"""The role vector's arithmetic, where a silent error would mis-rank builds.

The combat sim is validated against the corpus; these pin the surrounding
math — Pareto dominance, effective HP, penetration efficiency, and the layer
extraction — which the corpus cannot see is wrong on its own.
"""

from __future__ import annotations

import pytest

role_score = pytest.importorskip("role_score")
RoleVector = role_score.RoleVector
pareto_layers = role_score.pareto_layers


class TestDominance:
    def test_strictly_better_on_all_axes_dominates(self):
        assert RoleVector((2, 2), ("a", "b")).dominates(RoleVector((1, 1), ("a", "b")))

    def test_better_on_one_equal_on_rest_dominates(self):
        assert RoleVector((2, 1), ("a", "b")).dominates(RoleVector((1, 1), ("a", "b")))

    def test_equal_does_not_dominate(self):
        assert not RoleVector((1, 1), ("a", "b")).dominates(RoleVector((1, 1), ("a", "b")))

    def test_a_tradeoff_is_incomparable(self):
        a = RoleVector((2, 1), ("a", "b"))
        b = RoleVector((1, 2), ("a", "b"))
        assert not a.dominates(b)
        assert not b.dominates(a)


class TestParetoLayers:
    def test_the_front_is_layer_zero(self):
        # (2,1) and (1,2) are a non-dominated front; (1,1) is dominated by both.
        vs = [RoleVector((2, 1), ("a", "b")), RoleVector((1, 2), ("a", "b")),
              RoleVector((1, 1), ("a", "b"))]
        layers = pareto_layers(vs)
        assert layers[0] == 0 and layers[1] == 0
        assert layers[2] == 1

    def test_a_single_axis_is_a_total_order(self):
        vs = [RoleVector((3,), ("x",)), RoleVector((1,), ("x",)), RoleVector((2,), ("x",))]
        assert pareto_layers(vs) == [0, 2, 1]


class TestEffectiveHealth:
    def _stats(self, health, phys, mag):
        from stat_calculator import _Stats
        from item import ItemAttribute
        s = _Stats()
        s.set_stat(ItemAttribute.HEALTH, health)
        s.set_stat(ItemAttribute.PHYSICAL_PROTECTION, phys)
        s.set_stat(ItemAttribute.MAGICAL_PROTECTION, mag)
        return s

    def test_protection_scales_health_by_the_curve(self):
        # 2000 health, 100 physical prot -> 2000 * 200/100 = 4000 physical EHP.
        s = self._stats(2000, 100, 0)
        assert role_score.effective_health(s, magical=False) == pytest.approx(4000.0)

    def test_mean_averages_both_types(self):
        s = self._stats(2000, 100, 0)
        # physical 4000, magical 2000 -> mean 3000.
        assert role_score.mean_effective_health(s) == pytest.approx(3000.0)


class TestPenetrationEfficiency:
    def _attacker(self, flat, percent):
        from stat_calculator import _Stats, _Penetration
        from item import ItemAttribute
        s = _Stats()
        s.set_stat(ItemAttribute.PHYSICAL_PENETRATION, _Penetration(flat, percent))
        return s

    def _defender(self, prot):
        from stat_calculator import _Stats
        from item import ItemAttribute
        s = _Stats()
        s.set_stat(ItemAttribute.PHYSICAL_PROTECTION, prot)
        return s

    def test_all_penetration_lands_when_target_is_tanky(self):
        # 30 flat vs 100 prot: nominal 30, all of it used -> 1.0.
        eff = role_score.penetration_efficiency(
            self._attacker(30, 0.0), self._defender(100), physical=True
        )
        assert eff == pytest.approx(1.0)

    def test_penetration_past_the_floor_is_wasted(self):
        # 100 flat vs 40 prot: nominal 100, only 40 used -> 0.4.
        eff = role_score.penetration_efficiency(
            self._attacker(100, 0.0), self._defender(40), physical=True
        )
        assert eff == pytest.approx(0.4)

    def test_no_penetration_is_perfectly_efficient(self):
        eff = role_score.penetration_efficiency(
            self._attacker(0, 0.0), self._defender(50), physical=True
        )
        assert eff == pytest.approx(1.0)
