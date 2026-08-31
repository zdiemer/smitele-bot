"""The nightly holdout figure, and the three ways /build declines to quote it.

This is the only claim the bot makes about its own recommender that it cannot
check while making it — a holdout needs a second aggregate and the days behind
it, which a slash command has neither the seconds nor the gigabytes for. So the
number arrives from a job, and everything here is about not repeating a stale or
mismatched one back to a player as if it were current.
"""

from __future__ import annotations

import datetime
import json
import os

import pytest

ranker_lift = pytest.importorskip("ranker_lift")


def utc(days_ago: float = 0.0) -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=days_ago
    )
    return stamp.isoformat(timespec="seconds")


def write(tmp_path, **overrides):
    payload = {
        "game": "smite2",
        "strategy": "shrunk_ranker",
        "baseline": "most_played",
        "cutoff": "2026-08-16",
        "cells": 151,
        "generated": utc(),
        "overall": {
            "lift": 0.0214,
            "beats_baseline": 0.61,
            "decided": 120,
            "support": 18.0,
        },
        "by_lane": {
            "Mid": {"lift": 0.0208, "beats_baseline": 0.64, "decided": 28, "support": 38.0},
            "Solo": {"lift": 0.0044, "beats_baseline": 0.65, "decided": 17, "support": 16.0},
        },
    }
    payload.update(overrides)
    path = os.path.join(tmp_path, ranker_lift.FILE_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return str(tmp_path)


class TestLoading:
    def test_a_normal_file_loads(self, tmp_path):
        assert ranker_lift.RankerLift.load(write(tmp_path)) is not None

    def test_no_file_is_not_an_error(self, tmp_path):
        """The bot starts before any job has run, and must still build."""
        assert ranker_lift.RankerLift.load(str(tmp_path)) is None

    def test_a_malformed_file_is_not_an_error(self, tmp_path):
        path = os.path.join(tmp_path, ranker_lift.FILE_NAME)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        assert ranker_lift.RankerLift.load(str(tmp_path)) is None

    def test_a_file_from_a_different_ranking_is_ignored(self, tmp_path):
        """A sweep writes these too. Quoting one would attribute another
        estimator's lift to the one that actually picked the build."""
        directory = write(tmp_path, strategy="additive_ranker")
        assert ranker_lift.RankerLift.load(directory) is None

    def test_a_file_with_no_overall_figure_is_ignored(self, tmp_path):
        assert ranker_lift.RankerLift.load(write(tmp_path, overall=None)) is None


class TestFreshness:
    def test_a_recent_measurement_is_quoted(self, tmp_path):
        lift = ranker_lift.RankerLift.load(write(tmp_path, generated=utc(1)))
        assert lift.fresh
        assert lift.describe()

    def test_a_stale_measurement_is_not(self, tmp_path):
        """"Beats the meta by 2%" is a claim about a meta, and the meta moves."""
        lift = ranker_lift.RankerLift.load(
            write(tmp_path, generated=utc(ranker_lift.MAX_AGE_DAYS + 1))
        )
        assert not lift.fresh
        assert lift.describe() == ""

    def test_an_unreadable_timestamp_counts_as_stale(self, tmp_path):
        lift = ranker_lift.RankerLift.load(write(tmp_path, generated="whenever"))
        assert lift.describe() == ""

    def test_a_naive_timestamp_is_read_as_utc(self, tmp_path):
        """Written with a zone today; this is what happens if a future writer
        forgets, and the answer must not be a crash."""
        naive = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        lift = ranker_lift.RankerLift.load(
            write(tmp_path, generated=naive.isoformat(timespec="seconds"))
        )
        assert lift.fresh


class TestWhichFigure:
    def test_a_well_decided_lane_uses_its_own(self, tmp_path):
        lift = ranker_lift.RankerLift.load(write(tmp_path))
        assert lift.measured("Mid")["scope"] == "Mid"
        assert lift.measured("Mid")["lift"] == pytest.approx(0.0208)

    def test_a_thin_lane_falls_back_to_overall(self, tmp_path):
        """Seventeen decided cells is not a lane's win rate, it is seventeen
        disagreements. The pooled figure answers the same question with more
        behind it."""
        lift = ranker_lift.RankerLift.load(write(tmp_path))
        assert lift.measured("Solo")["scope"] == "overall"

    def test_a_lane_nobody_measured_falls_back_to_overall(self, tmp_path):
        lift = ranker_lift.RankerLift.load(write(tmp_path))
        assert lift.measured("Jungle")["scope"] == "overall"

    def test_no_lane_asks_for_the_overall_figure(self, tmp_path):
        lift = ranker_lift.RankerLift.load(write(tmp_path))
        assert lift.measured()["scope"] == "overall"

    def test_the_lane_is_matched_however_it_is_capitalised(self, tmp_path):
        lift = ranker_lift.RankerLift.load(write(tmp_path))
        assert lift.measured("mid")["scope"] == "Mid"


class TestDescribing:
    def test_it_names_the_number_and_what_is_behind_it(self, tmp_path):
        text = ranker_lift.RankerLift.load(write(tmp_path)).describe("Mid")
        assert "+2.1%" in text
        assert "28 cells" in text
        assert "in Mid" in text

    def test_the_fallback_says_it_is_across_lanes(self, tmp_path):
        text = ranker_lift.RankerLift.load(write(tmp_path)).describe("Solo")
        assert "across lanes" in text

    def test_a_negative_lift_is_reported_as_one(self, tmp_path):
        """Support was −1.1% before the per-lane half-life. If it goes negative
        again the embed should say so rather than hiding it."""
        directory = write(
            tmp_path,
            overall={
                "lift": -0.0108,
                "beats_baseline": 0.44,
                "decided": 90,
                "support": 12.0,
            },
        )
        assert "-1.1%" in ranker_lift.RankerLift.load(directory).describe()
