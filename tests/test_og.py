"""The link preview is drawn, not checked in.

A static card is a photograph of the numbers on the day someone exported it,
and this site's entire subject is that its numbers move — a preview claiming a
healthy crawl six weeks after it stopped being healthy is worse than no
preview. So it is rendered from the live snapshots and cached against their
mtimes.

Which means it runs against whatever the snapshot happens to contain, including
a snapshot that is half missing because a share went away. Every fact on it is
optional for that reason: a card with three true lines beats one with five
where two say "—", and a preview is the one surface where a placeholder is
indistinguishable from a broken site.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "web"))

pytest.importorskip("PIL")
og = pytest.importorskip("og")

from PIL import Image  # noqa: E402
import io  # noqa: E402


FULL_STATUS = {
    "generated_at": time.time(),
    "hirez": {"quota": {"requests_today": 3631, "requests_limit": 75000}},
    "tracker": {"standdown": {"active": False, "remaining_seconds": 0}},
}

FULL_STATS = {
    "generated_at": time.time(),
    "games": {
        "smite": {
            "built": True,
            "distinct_gods": 130,
            "distinct_queues": 9,
            "total_plays": 158121030,
            "matches_per_day": {
                "all": [{"date": "2026-08-05", "matches": 12}],
                "by_queue": {},
                "queues": [],
            },
        }
    },
}


def open_png(payload: bytes) -> Image.Image:
    return Image.open(io.BytesIO(payload))


class TestItRenders:
    def test_the_card_is_the_size_crawlers_expect(self):
        image = open_png(og.render(FULL_STATUS, FULL_STATS))

        # 1200×630 is the size Open Graph consumers crop against; anything else
        # gets letterboxed or refused.
        assert image.size == (og.WIDTH, og.HEIGHT) == (1200, 630)
        assert image.format == "PNG"

    def test_it_is_not_a_blank_rectangle(self):
        image = open_png(og.render(FULL_STATUS, FULL_STATS)).convert("RGB")

        # Cheap proof that something was actually drawn: a blank card would have
        # exactly one colour.
        assert len(image.getcolors(maxcolors=100000) or []) > 20


class TestItSurvivesAThinSnapshot:
    """Every fact is optional, because every section of the snapshot is."""

    @pytest.mark.parametrize(
        "status,stats",
        [
            ({}, {}),
            (None, None),
            ({"hirez": {"error": "boom"}}, {"games": {}}),
            ({"tracker": {"error": "boom"}}, {"games": {"smite": {"built": False}}}),
            (FULL_STATUS, {}),
            ({}, FULL_STATS),
        ],
    )
    def test_a_missing_section_still_renders(self, status, stats):
        image = open_png(og.render(status or {}, stats or {}))

        assert image.size == (1200, 630)

    def test_a_failed_section_contributes_no_lines(self):
        left, right = og._facts({"hirez": {"error": "x"}}, {"games": {}})

        assert left == []
        assert right == []


class TestItSaysWhatIsTrue:
    def test_a_live_standdown_is_named_and_counted_down(self):
        left, right = og._facts(
            {
                "tracker": {
                    "standdown": {"active": True, "remaining_seconds": 1620}
                }
            },
            {},
        )

        text = " ".join(line for line, _ in right)
        assert "tracker.gg blocked" in text
        assert "27m" in text, "the countdown is the useful half"

    def test_a_clear_tracker_says_so_rather_than_going_quiet(self):
        _, right = og._facts(
            {"tracker": {"standdown": {"active": False}}}, {}
        )

        assert any("clear" in line for line, _ in right)

    def test_quota_pressure_changes_the_colour(self):
        _, calm = og._facts(
            {"hirez": {"quota": {"requests_today": 1, "requests_limit": 100}}}, {}
        )
        _, busy = og._facts(
            {"hirez": {"quota": {"requests_today": 90, "requests_limit": 100}}}, {}
        )

        assert calm[0][1] == og.HEALTHY
        assert busy[0][1] == og.LATE

    def test_an_unbuilt_aggregate_contributes_nothing(self):
        # Not "0 matches" — that reads as an empty corpus rather than as an
        # aggregate nobody has run.
        left, _ = og._facts({}, {"games": {"smite": {"built": False}}})

        assert left == []
