"""Smite 2's stat model — the formulas, the caps, and the two kinds of stat
that only exist in an item's prose.

These are the numbers a build embed prints, so being wrong here is wrong
quietly: a mitigation curve off by a factor still produces a plausible-looking
number. Each formula is pinned to a value stated on wiki.smite2.com rather than
to whatever the code currently returns.
"""

from __future__ import annotations

import zlib

import pytest

import smite2_stats as stats
from god import God, GodStat, GodStats
from god_types import GodType
from item import Item, ItemAttribute, ItemProperty, ItemType


def _stable_id(name: str) -> int:
    """A deterministic id for a fake item or god.

    Not `hash()`: Python randomises string hashing per process, so ids
    built from it differ between runs. Scoring breaks ties on id, and a
    fake catalogue hits ties often — every item past the point a target
    saturates scores identically — so hash-derived ids made these tests
    pass or fail depending on the seed the interpreter happened to start
    with."""
    return zlib.crc32(name.encode()) % 10_000_000



def make_item(name="Test", properties=None, passive=None, tier=3, cost=2500):
    item = Item()
    item.name = name
    item.id = _stable_id(name)
    item.tier = tier
    item.price = cost
    item.total_cost = cost
    item.active = True
    item.is_starter = False
    item.type = ItemType.ITEM
    item.item_properties = properties or []
    item.passive = passive
    item.icon_url = ""
    item.restricted_roles = []
    item.glyph = False
    return item


def make_god(name="Test", god_type=GodType.MAGICAL, scaling=None, curves=None):
    god = God()
    god.name = name
    god.id = 1
    god.type = god_type
    god.role = None
    god.scaling = scaling
    god.role_scaling = {}
    god.positions = []
    god.specs = []
    god.stats = GodStats()
    god.stats.values = {}
    for attribute, value in (curves or {}).items():
        god.stats.values[attribute] = GodStat(value, 0.0, curve=[value] * 20)
    return god


class TestMitigation:
    """The wiki's "1 Protection withstands 1% of the damage" is shorthand that
    is only true near zero; the curve is 100/(100+protection)."""

    @pytest.mark.parametrize(
        "protection,expected_reduction",
        [(0, 0.0), (50, 1 / 3), (100, 0.5), (150, 0.6)],
    )
    def test_published_points_on_the_curve(self, protection, expected_reduction):
        taken = stats.damage_taken_multiplier(protection)
        assert taken == pytest.approx(1 - expected_reduction, abs=1e-9)

    def test_negative_protection_never_amplifies_damage(self):
        assert stats.damage_taken_multiplier(-500) == 1.0

    def test_effective_health_inverts_mitigation(self):
        # 100 protection halves incoming damage, so it doubles the health pool.
        assert stats.effective_health(2000, 100) == pytest.approx(4000)


class TestCooldownRate:
    """Cooldown Rate is points, not a percentage, and reads as one."""

    @pytest.mark.parametrize(
        "rate,reduction", [(10, 0.0909), (50, 0.3333), (120, 0.5454)]
    )
    def test_rate_is_not_a_percentage(self, rate, reduction):
        assert stats.cooldown_reduction(rate) == pytest.approx(reduction, abs=1e-3)


class TestPenetration:
    def test_percent_applies_before_flat(self):
        # Not commutative: the other order leaves 56.
        assert stats.penetrated(100, flat=30, percent=0.20) == pytest.approx(50)

    def test_never_goes_below_zero(self):
        assert stats.penetrated(10, flat=50, percent=0.0) == 0.0


class TestCaps:
    def test_penetration_is_capped_both_ways(self):
        build = stats.Smite2Stats()
        build.add_flat(ItemAttribute.PENETRATION, 90)
        build.add_percent(ItemAttribute.PENETRATION, 0.75)
        build.apply_caps()
        assert build.get(ItemAttribute.PENETRATION) == 50.0
        assert build.get_percent(ItemAttribute.PENETRATION) == 0.40

    def test_overcapping_is_remembered_not_just_clamped(self):
        build = stats.Smite2Stats()
        build.add_flat(ItemAttribute.PLATED, 60)
        build.apply_caps()
        assert build.get(ItemAttribute.PLATED) == 35.0
        assert build.overcapped[ItemAttribute.PLATED] == 60

    def test_attack_speed_is_left_alone(self):
        """Smite 1 caps attack speed at 2.5; Smite 2 publishes no cap, and
        inventing one is indistinguishable from a real one once it is code."""
        assert ItemAttribute.ATTACK_SPEED not in stats.FLAT_CAPS
        assert ItemAttribute.ATTACK_SPEED not in stats.PERCENT_CAPS


class TestAdaptive:
    """29 catalogue items carry their damage stat only in the passive text."""

    ADAPTIVE = "Adaptive Stat: +30 Strength or +45 Intelligence (based on highest item stat)."

    def test_reads_the_pair(self):
        assert stats.adaptive_stat(make_item(passive=self.ADAPTIVE)) == (30.0, 45.0)

    def test_tolerates_the_executioners_typo(self):
        """The Executioner has said "hightest" since it shipped."""
        item = make_item(
            passive="Adaptive Stat: +30 Strength or +55 Intelligence "
            "(based on hightest item stat)."
        )
        assert stats.adaptive_stat(item) == (30.0, 55.0)

    def test_an_item_without_one_reads_as_none(self):
        assert stats.adaptive_stat(make_item(passive="Every 10s: -1s Cooldowns.")) is None

    def test_resolves_to_whichever_stat_the_build_already_has_more_of(self):
        strength_item = make_item(
            "Str", [ItemProperty(ItemAttribute.STRENGTH, flat_value=60)]
        )
        adaptive = make_item("Adaptive", passive=self.ADAPTIVE)
        total = stats.item_stats([strength_item, adaptive], make_god())
        assert total.get(ItemAttribute.STRENGTH) == 90
        assert total.get(ItemAttribute.INTELLIGENCE) == 0

    def test_falls_back_to_the_gods_scaling_when_nothing_to_compare(self):
        adaptive = make_item("Adaptive", passive=self.ADAPTIVE)
        total = stats.item_stats([adaptive], make_god(scaling="int"))
        assert total.get(ItemAttribute.INTELLIGENCE) == 45

    def test_hybrid_gods_default_to_strength(self):
        """The rule the wiki states for hybrid scaling."""
        adaptive = make_item("Adaptive", passive=self.ADAPTIVE)
        total = stats.item_stats([adaptive], make_god(scaling="hybrid"))
        assert total.get(ItemAttribute.STRENGTH) == 30

    def test_scaling_beats_damage_type(self):
        """A physical-damage god that scales with Intelligence — Neith and
        Danzaburou are both real examples — must not be handed Strength."""
        adaptive = make_item("Adaptive", passive=self.ADAPTIVE)
        god = make_god(god_type=GodType.PHYSICAL, scaling="int")
        assert stats.item_stats([adaptive], god).get(ItemAttribute.INTELLIGENCE) == 45


class TestPassiveStats:
    def test_reads_a_flat_grant(self):
        item = make_item(passive="While Berserk: +15 Physical Protection.")
        granted = stats.passive_stats(item)
        assert granted.get(ItemAttribute.PHYSICAL_PROTECTION) == 15

    def test_reads_a_percentage_grant(self):
        item = make_item(passive="First Ability Cast: Gains +20% Penetration.")
        assert stats.passive_stats(item).get_percent(ItemAttribute.PENETRATION) == 0.20

    def test_scales_off_what_the_build_already_has(self):
        """Rod of Tahuti, the most-picked item in the corpus, carries most of
        its value this way."""
        base = stats.Smite2Stats()
        base.add_flat(ItemAttribute.INTELLIGENCE, 400)
        item = make_item(
            passive="+Intelligence equal to 25% of your Intelligence from items."
        )
        assert stats.passive_stats(item, base).get(ItemAttribute.INTELLIGENCE) == 100

    def test_a_scaling_passive_is_skipped_rather_than_guessed(self):
        item = make_item(
            passive="+Intelligence equal to 25% of your Intelligence from items."
        )
        assert not stats.passive_stats(item).flat

    def test_the_adaptive_sentence_is_not_counted_twice(self):
        item = make_item(
            passive="Adaptive Stat: +30 Strength or +45 Intelligence "
            "(based on highest item stat)."
        )
        granted = stats.passive_stats(item)
        assert granted.get(ItemAttribute.STRENGTH) == 0
        assert granted.get(ItemAttribute.INTELLIGENCE) == 0


class TestCooldownRefunds:
    """The largest unread category in the catalogue: seven of the twenty-five
    most-played items whose passive said nothing were cooldown refunds."""

    def test_a_timed_refund_converts_to_cooldown_rate(self):
        """Chronos' Pendant: -1s every 10s is 10% off, which is 11.1 Rate.

        The wiki's own note on the item says its 25 Rate plus this passive comes
        to "36 Cooldown Rate", which is the arithmetic this has to reproduce.
        """
        item = make_item(passive="Every 10s: -1s Ability Cooldowns.")
        rate = stats.passive_stats(item).get(ItemAttribute.COOLDOWN_RATE)
        assert rate == pytest.approx(11.1, abs=0.2)
        assert 25 + rate == pytest.approx(36, abs=0.2)

    def test_a_percentage_off_the_ultimate_counts_for_less_than_all(self):
        whole = make_item(passive="-30% Cooldown for your Abilities.")
        ultimate = make_item(passive="-30% Cooldown for your Ultimate Ability.")
        assert stats.passive_stats(ultimate).get(
            ItemAttribute.COOLDOWN_RATE
        ) < stats.passive_stats(whole).get(ItemAttribute.COOLDOWN_RATE)

    def test_an_event_refund_takes_the_largest_not_the_sum(self):
        """A kill or an assist — the bullets are alternatives, not a total."""
        item = make_item(
            passive="God Kill: -3s Non Ultimate Cooldowns -10s Ultimate Cooldowns "
            "God Assist: -1.5s Non Ultimate Cooldowns -5s Ultimate Cooldowns"
        )
        rate = stats.passive_stats(item).get(ItemAttribute.COOLDOWN_RATE)
        biggest = stats._cooldown_rate_for(10.0 / stats.EVENT_COOLDOWN_WINDOW)
        assert rate == pytest.approx(biggest)

    def test_an_item_with_no_refund_gets_none(self):
        item = make_item(passive="+35% Critical Strike Damage.")
        assert stats.passive_stats(item).get(ItemAttribute.COOLDOWN_RATE) == 0


class TestDamageShare:
    def test_bonus_damage_as_a_share_of_a_stat_counts_as_that_stat(self):
        base = stats.Smite2Stats()
        base.add_flat(ItemAttribute.INTELLIGENCE, 400)
        item = make_item(
            passive="Ability Used: Your next Attack deals bonus Magical Damage. "
            "Damage = 80% of your Intelligence"
        )
        granted = stats.passive_stats(item, base).get(ItemAttribute.INTELLIGENCE)
        assert granted == pytest.approx(400 * 0.80 * stats.DAMAGE_SHARE_UPTIME)

    def test_a_proc_is_not_worth_a_permanent_stat(self):
        """Counted in full, Polynomicon read as +320 Intelligence and the best
        item in the game."""
        assert 0 < stats.DAMAGE_SHARE_UPTIME < 1

    def test_a_scaled_grant_is_not_also_counted_as_a_damage_share(self):
        """Rod of Tahuti matches both patterns; counting both made the
        most-picked item in the game read as twice the item it is."""
        base = stats.Smite2Stats()
        base.add_flat(ItemAttribute.INTELLIGENCE, 400)
        item = make_item(
            passive="+Intelligence equal to 25% of your Intelligence from items."
        )
        assert stats.passive_stats(item, base).get(
            ItemAttribute.INTELLIGENCE
        ) == pytest.approx(100)


class TestShred:
    def test_making_a_target_take_more_damage_reads_as_penetration(self):
        item = make_item(
            passive="Ability Hit a God: That God is marked for 8s. "
            "Marked Gods take 5% increased damage from all sources."
        )
        assert stats.passive_stats(item).get_percent(
            ItemAttribute.PENETRATION
        ) == pytest.approx(0.05)


class TestDerivedStats:
    def test_attack_scales_fully_with_strength_and_a_fifth_with_intelligence(self):
        build = stats.Smite2Stats()
        build.add_flat(ItemAttribute.BASIC_ATTACK_POWER, 90)
        build.add_flat(ItemAttribute.STRENGTH, 100)
        build.add_flat(ItemAttribute.INTELLIGENCE, 100)
        assert build.attack_damage == pytest.approx(90 + 100 + 20)

    def test_base_attack_speed_is_a_percentage_bonus(self):
        build = stats.Smite2Stats()
        build.add_flat(ItemAttribute.ATTACK_SPEED, 28)
        build.add_percent(ItemAttribute.ATTACK_SPEED, 0.30)
        assert build.attacks_per_second == pytest.approx(1.58)

    def test_gods_contribute_no_strength_or_intelligence(self):
        """In Smite 2 every point of both comes from the build."""
        assert ItemAttribute.STRENGTH not in stats._GOD_BASE_STATS
        assert ItemAttribute.INTELLIGENCE not in stats._GOD_BASE_STATS


class TestRegenAliasing:
    def test_item_regen_folds_onto_the_gods_name_for_it(self):
        """An item's `mpr` and a god's `ManaPerTime` are one stat under two
        names; unfolded, a build reports its regen twice and neither total
        includes the god."""
        item = make_item(
            properties=[ItemProperty(ItemAttribute.MANA_REGEN, flat_value=5)]
        )
        god = make_god(curves={ItemAttribute.MP5: 4})
        total = stats.build_stats(god, [item])
        assert total.get(ItemAttribute.MP5) == 9
        assert total.get(ItemAttribute.MANA_REGEN) == 0


class TestDescribeBuild:
    def test_reports_effective_health_and_cooldowns(self):
        god = make_god(
            curves={
                ItemAttribute.HEALTH: 2000,
                ItemAttribute.PHYSICAL_PROTECTION: 100,
                ItemAttribute.MAGICAL_PROTECTION: 100,
            }
        )
        item = make_item(
            properties=[ItemProperty(ItemAttribute.COOLDOWN_RATE, flat_value=25)]
        )
        text = stats.describe_build(god, [item])
        assert "Cooldown Rate" in text
        # 2000 health behind 100 protection is 4,000 damage to kill.
        assert "4,000" in text
        # 25 Cooldown Rate is 20% off, not 25%.
        assert "20.0%" in text

    def test_says_when_a_build_wastes_points_on_a_cap(self):
        properties = [ItemProperty(ItemAttribute.PENETRATION, percent_value=0.30)]
        build = [make_item("A", properties), make_item("B", properties)]
        assert "cap" in stats.describe_build(make_god(), build).lower()
