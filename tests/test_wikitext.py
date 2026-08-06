"""The wikitext parser feeds every Smite 2 god and item, and its failures are
silent — a mis-parsed cooldown is a plausible number, not an exception. These
pin the cases that actually went wrong while building it.
"""

from __future__ import annotations

import pytest

from smite2 import wikitext


class TestTemplateScanning:
    def test_pipes_inside_nested_templates_are_not_separators(self):
        """`{{Int|30}}` inside a parameter is one value, not two."""
        found = wikitext.parse_templates("{{Item infobox|stat1={{Int|30}}|tier=3}}",
                                         "Item infobox")
        assert found[0].params == {"stat1": "{{Int|30}}", "tier": "3"}

    def test_pipes_inside_links_are_not_separators(self):
        found = wikitext.parse_templates(
            "{{God infoboxS2|role1=[[File:Mid.png|link=Mid|25px]] [[Mid]]|name=Ra}}",
            "God infoboxS2",
        )
        assert found[0].get("name") == "Ra"
        assert "25px" in found[0].get("role1")

    def test_rank_separator_survives_scanning(self):
        """`{{!}}` must reach rank_values intact — it is a template call in
        source, so a naive scanner could swallow it."""
        found = wikitext.parse_templates("{{Ability|damage=60 {{!}} 85 {{!}} 110}}",
                                         "Ability")
        assert "{{!}}" in found[0].get("damage")

    def test_anonymous_parameters_are_numbered(self):
        found = wikitext.parse_templates("{{Ia|Anubis_Ability_2.ogg}}", "Ia")
        assert found[0].params == {"1": "Anubis_Ability_2.ogg"}

    def test_an_equals_inside_a_nested_template_does_not_name_the_parameter(self):
        """`{{Foo|{{Bar|a=1}}}}` has one anonymous parameter, not one called
        `{{Bar|a`."""
        found = wikitext.parse_templates("{{Foo|{{Bar|a=1}}}}", "Foo")
        assert found[0].params == {"1": "{{Bar|a=1}}"}

    def test_templates_come_back_in_source_order(self):
        """The scanner finishes a nested template before its parent, so without
        sorting `parse_all` returns the innermost first — which reads an item's
        recipe as its grandchildren."""
        text = "{{Recipe|item=Thoth|i1={{Recipe|item=Oracle|i1={{Recipe|item=Gem}}}}}}"
        names = [t.get("item") for t in wikitext.parse_templates(text, "Recipe")]
        assert names == ["Thoth", "Oracle", "Gem"]

    def test_top_level_filters_to_the_outermost(self):
        text = "{{Recipe|item=Thoth|i1={{Recipe|item=Oracle}}}}"
        found = wikitext.parse_templates(text, "Recipe", top_level=True)
        assert [t.get("item") for t in found] == ["Thoth"]

    def test_names_are_matched_loosely(self):
        assert wikitext.parse_templates("{{god_infoboxS2|name=Ra}}", "God infoboxS2")

    def test_an_unterminated_template_does_not_hang(self):
        assert wikitext.parse_templates("{{Ability|name=Broken", "Ability") == []

    def test_comments_are_stripped(self):
        found = wikitext.parse_templates(
            "{{Ability<!-- todo -->|name=Mummify}}", "Ability"
        )
        assert found[0].get("name") == "Mummify"


class TestRankValues:
    def test_the_rank_separator_is_accepted_in_both_spellings(self):
        """strip_markup rewrites {{!}} to a pipe, so by the time a value reaches
        rank_values it may be either. Splitting on only the source spelling
        silently truncated every array to its first entry — Anubis's Plague of
        Locusts reported a flat 10s cooldown instead of 10/9.5/9/8.5/8."""
        assert wikitext.rank_values("10 {{!}} 9.5 {{!}} 9") == [10.0, 9.5, 9.0]
        assert wikitext.rank_values("10 | 9.5 | 9") == [10.0, 9.5, 9.0]

    def test_a_flat_value_is_a_one_element_array(self):
        assert wikitext.rank_values("12 seconds") == [12.0]

    def test_non_numeric_ranks_drop_out(self):
        """A zero standing in for "no cost" would read as a real cost of zero."""
        assert wikitext.rank_values("None") == []

    def test_decimals_and_negatives(self):
        assert wikitext.rank_values("-5 {{!}} 2.5") == [-5.0, 2.5]


class TestStatLines:
    def test_a_scaling_cooldown(self):
        parsed = wikitext.parse_stat_line(
            '*<span style="color:#fcc26a;">Cooldown</span>: '
            "10 {{!}} 9.5 {{!}} 9 {{!}} 8.5 {{!}} 8 seconds"
        )
        assert parsed == ("Cooldown", [10.0, 9.5, 9.0, 8.5, 8.0], "seconds")

    def test_the_unit_does_not_swallow_the_rest_of_the_array(self):
        _, _, unit = wikitext.parse_stat_line("*Cost: 30 {{!}} 40 {{!}} 50 mana")
        assert unit == "mana"

    def test_a_percentage(self):
        assert wikitext.parse_stat_line("*Self Slow: 35 {{!}} 30%") == (
            "Self Slow", [35.0, 30.0], "%",
        )

    def test_prose_without_a_value_is_skipped(self):
        assert wikitext.parse_stat_line("*This ability cannot be interrupted") is None

    def test_an_empty_line_is_skipped(self):
        assert wikitext.parse_stat_line("   ") is None

    def test_a_block_keys_by_label(self):
        block = wikitext.parse_stat_block(
            "*Damage: 60 {{!}} 90\n*Cooldown: 12 seconds\n*Some prose here"
        )
        assert block["Damage"] == ([60.0, 90.0], "")
        assert block["Cooldown"] == ([12.0], "seconds")
        assert "Some prose here" not in block


class TestSections:
    PAGE = (
        "{{God infoboxS2|name=Hecate}}\n"
        "==Lore==\nShe is old.\n"
        "==Abilities==\n{{Ability|slot=Passive|name=Ghost Walk}}\n"
        "==God Aspect==\n{{Achievement|name=Aspect of Ruin}}\n"
        "{{Ability|slot=2nd Ability|name=Spell Eater}}\n"
        "==Skins==\nnone\n"
    )

    def test_sections_split_on_level_two_headings(self):
        found = wikitext.sections(self.PAGE)
        assert sorted(found) == ["Abilities", "God Aspect", "Lore", "Skins"]

    def test_scoping_excludes_the_aspect_section(self):
        """70 of 88 god articles repeat every ability in the Aspect section, so
        parsing the whole page doubles the kit."""
        whole = wikitext.parse_templates(self.PAGE, "Ability")
        scoped = wikitext.parse_templates(
            wikitext.sections(self.PAGE)["Abilities"], "Ability"
        )
        assert len(whole) == 2
        assert [t.get("name") for t in scoped] == ["Ghost Walk"]

    def test_comment_stripping_does_not_shift_section_boundaries(self):
        """Matching on the stripped text and slicing the original moves every
        boundary by the length of the comments."""
        page = "<!-- a long comment -->\n==Lore==\nbody text\n==Skins==\nx\n"
        assert wikitext.sections(page)["Lore"].strip() == "body text"


class TestStripMarkup:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("[[Mid|the mid lane]]", "the mid lane"),
            ("[[Anubis]]", "Anubis"),
            ("'''bold''' and ''italic''", "bold and italic"),
            ('<span style="color:#fff">red</span>', "red"),
            ("a<br>b", "a\nb"),
            ("60 {{!}} 85", "60 | 85"),
            ("&nbsp;spaced&nbsp;", "spaced"),
        ],
    )
    def test_markup_is_reduced_to_text(self, raw, expected):
        assert wikitext.strip_markup(raw) == expected
