"""Parsing Smite 2 ability damage out of the wiki rank tables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pytest

kit = pytest.importorskip("smite2_ability_kit")


@dataclass
class _Rank:
    name: str
    rank_values: str


@dataclass
class _Ability:
    name: str
    rank_properties: List[_Rank]
    cooldown_by_rank: List[float]
    is_passive: bool = False


@dataclass
class _God:
    abilities: List[_Ability]


def ability(name, damage, scaling, cd, extra=None):
    props = []
    if damage is not None:
        props.append(_Rank("Damage", damage))
    if scaling is not None:
        props.append(_Rank("Damage Scaling", scaling))
    props += extra or []
    return _Ability(name, props, [cd] if cd is not None else [])


class TestParsingOneAbility:
    def test_reads_base_scaling_and_stat(self):
        a = kit.parse_ability(ability("Crush", "100/155/210/265/320", "90 % Intelligence", 10.0), False)
        assert a.base_damage == 320
        assert a.scaling == pytest.approx(0.90)
        assert a.scaling_stat == "intelligence"
        assert a.cooldown == 10.0

    def test_strength_scaling(self):
        a = kit.parse_ability(ability("Rift", "110", "30 % Strength", 15.0), False)
        assert a.scaling_stat == "strength"
        assert a.scaling == pytest.approx(0.30)

    def test_an_exotic_scaling_contributes_base_only(self):
        # Protections/health scaling is real but unusable here -> zero scaling.
        a = kit.parse_ability(ability("Odd", "200", "50 % Physical and Magical Protection", 12.0), False)
        assert a.base_damage == 200
        assert a.scaling == 0.0

    def test_no_damage_line_is_not_an_ability(self):
        assert kit.parse_ability(ability("Buff", None, "50 % Strength", 12.0), False) is None

    def test_no_cooldown_is_dropped(self):
        assert kit.parse_ability(ability("Stance", "100", "50 % Strength", None), False) is None

    def test_per_tick_multiplies_by_a_stated_count(self):
        a = kit.parse_ability(
            _Ability("Beam", [
                _Rank("Damage Per Tick", "20/30/40/50/60"),
                _Rank("Damage Scaling", "10 % Intelligence"),
                _Rank("Ticks", "5"),
            ], [12.0]),
            False,
        )
        assert a.hits == 5
        assert a.total_base == pytest.approx(60 * 5)

    def test_the_largest_damage_line_wins(self):
        a = kit.parse_ability(
            _Ability("Two", [
                _Rank("Initial Damage", "100"),
                _Rank("Detonation Damage", "250"),
                _Rank("Damage Scaling", "50 % Strength"),
            ], [10.0]),
            False,
        )
        assert a.base_damage == 250


class TestParsingAKit:
    def test_passives_and_undamaging_abilities_are_skipped(self):
        god = _God([
            _Ability("Passive", [_Rank("Damage", "100"), _Rank("Damage Scaling", "50 % Strength")], [1.0], is_passive=True),
            ability("Nuke", "200", "60 % Intelligence", 10.0),
            ability("Dash", None, None, 12.0),
        ])
        parsed = kit.parse_kit(god)
        assert [a.name for a in parsed.damaging] == ["Nuke"]
