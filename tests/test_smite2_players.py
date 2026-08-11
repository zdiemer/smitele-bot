"""Per-player Smite 2 lookups and the embeds they render.

Shapes here come from real tracker.gg responses; the fixtures are trimmed
versions of them rather than invented.
"""

from __future__ import annotations

import pytest

from smite2.players import (
    MatchSummary,
    Segment,
    best_and_worst,
    _segment,
    _summarise,
)

discord = pytest.importorskip("discord", reason="py-cord not installed")

import smite2_player_embeds as embeds  # noqa: E402


def raw_segment(key: str, played: int, won: int, ranked=False, rating=None):
    stats = {
        "matchesPlayed": {"value": played, "displayValue": str(played)},
        "matchesWon": {"value": won, "displayValue": str(won)},
        "matchesLost": {"value": played - won, "displayValue": str(played - won)},
        "matchesWinPct": {
            "value": 100.0 * won / played if played else 0.0,
            "displayValue": f"{100.0 * won / played:.1f}%" if played else "0%",
        },
        "kdaRatio": {"value": 3.5, "displayValue": "3.50"},
        "killsPerMatch": {"value": 7.0, "displayValue": "7.0"},
        "deathsPerMatch": {"value": 4.0, "displayValue": "4.0"},
        "assistsPerMatch": {"value": 9.0, "displayValue": "9.0"},
        "timePlayed": {"value": 3600, "displayValue": "1h"},
    }
    if rating is not None:
        stats["skillRating"] = {"value": rating, "displayValue": str(rating)}
        stats["peakSkillRating"] = {"value": rating + 50, "displayValue": ""}
    return {
        "attributes": {"gamemode": key, "god": key, "role": key},
        "metadata": {"name": key.title(), "imageUrl": None, "isRanked": ranked},
        "stats": stats,
    }


class TestSegments:
    def test_only_numeric_stats_are_kept(self):
        """Every stat arrives as an object with a value and a display string;
        the display string is not a number and must not be treated as one."""
        segment = _segment(raw_segment("arena", 10, 5), "gamemode")
        assert segment.stats["matchesPlayed"] == 10
        assert segment.display["matchesWinPct"] == "50.0%"

    def test_derived_properties(self):
        segment = _segment(raw_segment("arena", 10, 7), "gamemode")
        assert (segment.matches, segment.wins, segment.losses) == (10, 7, 3)
        assert segment.win_rate == pytest.approx(0.7)


class TestBestAndWorst:
    def test_a_minimum_sample_is_required(self):
        """One win is a 100% win rate. The Smite 1 command uses the same floor
        for the same reason."""
        segments = [
            _segment(raw_segment("lucky", 1, 1), "god"),
            _segment(raw_segment("real", 40, 30), "god"),
        ]
        best, _ = best_and_worst(segments, minimum=10)
        assert best.name == "Real"

    def test_nothing_qualifies(self):
        segments = [_segment(raw_segment("a", 2, 1), "god")]
        assert best_and_worst(segments, minimum=10) == (None, None)

    def test_ties_break_on_sample_size(self):
        segments = [
            _segment(raw_segment("small", 10, 5), "god"),
            _segment(raw_segment("big", 100, 50), "god"),
        ]
        best, _ = best_and_worst(segments, minimum=10)
        assert best.name == "Big"


class TestMatchSummary:
    MATCH = {
        "attributes": {"id": "abc", "gamemode": "assault"},
        "metadata": {
            "timestamp": "2026-08-06T13:39:00+00:00",
            "gamemodeName": "Assault",
            "isRanked": False,
            "winningTeamId": "order",
            "duration": 1200,
        },
        "segments": [
            {"type": "team", "attributes": {}, "metadata": {}, "stats": {}},
            {
                "type": "overview",
                "attributes": {"platformSlug": "steam", "platformUserIdentifier": "42"},
                "metadata": {
                    "teamId": "order",
                    "god": "hou-yi",
                    "godName": "Hou Yi",
                    "godImageUrl": "http://x/hou-yi.jpg",
                    "playedRole": {"key": "carry"},
                },
                "stats": {
                    "kills": {"value": 19},
                    "deaths": {"value": 3},
                    "assists": {"value": 14},
                    "skillRating": {"value": 655},
                    "skillRatingDelta": {"value": 12},
                },
            },
            {
                "type": "overview",
                "attributes": {"platformSlug": "steam", "platformUserIdentifier": "99"},
                "metadata": {"teamId": "chaos", "god": "ra", "godName": "Ra"},
                "stats": {"kills": {"value": 1}},
            },
        ],
    }

    def test_the_summary_is_from_the_queried_players_view(self):
        """Ten players share a match; the one asked about is the one reported."""
        summary = _summarise(self.MATCH, "steam", "42")
        assert summary.god_name == "Hou Yi"
        assert (summary.kills, summary.deaths, summary.assists) == (19, 3, 14)

    def test_a_win_is_the_players_team_matching_the_winner(self):
        assert _summarise(self.MATCH, "steam", "42").won is True
        assert _summarise(self.MATCH, "steam", "99").won is False

    def test_skill_rating_is_carried_when_present(self):
        summary = _summarise(self.MATCH, "steam", "42")
        assert summary.skill_rating == 655
        assert summary.skill_rating_delta == 12

    def test_a_player_not_in_the_match_is_none(self):
        assert _summarise(self.MATCH, "steam", "nobody") is None

    def test_handles_are_matched_case_insensitively(self):
        match = dict(self.MATCH)
        assert _summarise(match, "xbl", "42") is not None


class TestEmbeds:
    def test_queue_stats_shows_rating_only_for_ranked(self):
        modes = [
            _segment(raw_segment("assault", 100, 80), "gamemode"),
            _segment(raw_segment("conquest-ranked", 2, 2, True, 655), "gamemode"),
        ]
        embed = embeds.queue_stats("steam", "42", modes)
        text = "\n".join(f.value for f in embed.fields)
        assert "Skill Rating" in text
        assert text.count("Skill Rating") == 1

    def test_rank_reports_no_ranked_play_rather_than_zero(self):
        modes = [_segment(raw_segment("assault", 100, 80), "gamemode")]
        embed = embeds.rank("steam", "42", modes)
        assert "No ranked matches" in embed.description

    def test_worshippers_for_one_god(self):
        gods = [_segment(raw_segment("thor", 307, 219), "god")]
        embed = embeds.worshippers("steam", "42", gods, "Thor")
        assert "Thor" in embed.title

    def test_worshippers_for_an_unplayed_god_says_so(self):
        gods = [_segment(raw_segment("thor", 307, 219), "god")]
        embed = embeds.worshippers("steam", "42", gods, "Ra")
        assert "no recorded matches" in embed.description

    def test_match_history_marks_wins_and_losses(self):
        matches = [_summarise(TestMatchSummary.MATCH, "steam", "42")]
        embed = embeds.match_history("steam", "42", matches)
        assert "Win" in embed.fields[0].value


class TestPlayerParsing:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("steam:76561198000000000", ("steam", "76561198000000000")),
            ("xbl:Gamer Tag", ("xbl", "Gamer Tag")),
            ("psn:Handle", ("psn", "Handle")),
            ("epic:Name", ("epic", "Name")),
            # No platform means the default, not a parse failure.
            ("PlainHandle", ("steam", "PlainHandle")),
            # A colon that is not a platform belongs to the handle.
            ("weird:name", ("steam", "weird:name")),
        ],
    )
    def test_player_identity_is_platform_plus_handle(self, value, expected):
        from smite2.players import parse_player

        assert parse_player(value) == expected
