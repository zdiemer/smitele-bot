"""The Smite 2 role vector's arithmetic, off a hand-built stat bag.

The DPS and vector shape are exercised against the real corpus by hand; these
pin the game-agnostic math — effective HP, penetration efficiency, and the
component-wise median defender — so a silent error cannot mis-rank builds.
"""

from __future__ import annotations

import pytest

s2 = pytest.importorskip("smite2_role_score")
Smite2Stats = pytest.importorskip("smite2_stats").Smite2Stats
ItemAttribute = pytest.importorskip("item").ItemAttribute


def _stats(**flat):
    s = Smite2Stats()
    for name, value in flat.items():
        s.add_flat(getattr(ItemAttribute, name), value)
    return s


class TestDefender:
    def test_effective_health_uses_the_right_protection(self):
        d = s2.Defender(health=2000, physical_protection=100, magical_protection=0)
        assert d.effective_health(magical=False) == pytest.approx(4000.0)
        assert d.effective_health(magical=True) == pytest.approx(2000.0)


class TestMedianDefender:
    def test_component_wise_median(self):
        defs = [
            s2.Defender(1000, 20, 10),
            s2.Defender(2000, 40, 30),
            s2.Defender(3000, 60, 80),
        ]
        m = s2.median_defender(defs)
        assert (m.health, m.physical_protection, m.magical_protection) == (2000, 40, 30)

    def test_even_count_averages_the_middle_two(self):
        defs = [s2.Defender(1000, 20, 10), s2.Defender(3000, 60, 30)]
        m = s2.median_defender(defs)
        assert m.health == pytest.approx(2000.0)

    def test_empty_is_none(self):
        assert s2.median_defender([]) is None


class TestPenetrationEfficiency:
    def test_all_lands_against_a_tanky_target(self):
        stats = _stats(PENETRATION=30)
        d = s2.Defender(2000, 100, 100)
        assert s2.penetration_efficiency(stats, d, magical=False) == pytest.approx(1.0)

    def test_past_the_floor_is_wasted(self):
        stats = _stats(PENETRATION=100)
        d = s2.Defender(2000, 40, 40)
        assert s2.penetration_efficiency(stats, d, magical=False) == pytest.approx(0.4)

    def test_no_penetration_is_perfect(self):
        d = s2.Defender(2000, 50, 50)
        assert s2.penetration_efficiency(_stats(), d, magical=False) == pytest.approx(1.0)


class TestMeanEffectiveHealth:
    def test_averages_both_protection_types(self):
        stats = _stats(HEALTH=2000, PHYSICAL_PROTECTION=100, MAGICAL_PROTECTION=0)
        # physical 4000, magical 2000 -> 3000.
        assert s2.mean_effective_health(stats) == pytest.approx(3000.0)
