"""The run report that outlives the Job that produced it.

`collect.py` prints a good end-of-run summary and then the pod is collected and
takes it with it, which makes "did last night work?" a question with a two-day
shelf life and a `kubectl` dependency. These cover the parts of writing it down
that are easy to get quietly wrong:

  - a dry run promises to write nothing, and that has to include this;
  - a night that *refused to start* must still leave a record, because
    otherwise it is indistinguishable from a night that has not happened;
  - a failed write must not take the run down with it, since by the time this
    is called the run is over either way.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "HirezAPI"),
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "smite2_collector"
    ),
)

last_run = pytest.importorskip("smite2.last_run")


def test_round_trips(tmp_path):
    last_run.write(str(tmp_path), {"exit_reason": "ok", "requests": 1500})
    loaded = last_run.read(str(tmp_path))

    assert loaded["exit_reason"] == "ok"
    assert loaded["requests"] == 1500
    assert loaded["version"] == last_run.SCHEMA_VERSION
    # Stamped for the caller, so every record can be aged without each writer
    # remembering to do it.
    assert loaded["finished"] > 0


def test_missing_file_reads_as_none(tmp_path):
    assert last_run.read(str(tmp_path)) is None


def test_garbage_reads_as_none(tmp_path):
    # A half-written file predating the atomic rename, or a truncated one from
    # a share that went away. Nothing reads this back to act on, so the only
    # correct response is to say there is no record.
    with open(last_run.path_for(str(tmp_path)), "w", encoding="utf-8") as handle:
        handle.write("{not json")

    assert last_run.read(str(tmp_path)) is None


def test_non_dict_reads_as_none(tmp_path):
    with open(last_run.path_for(str(tmp_path)), "w", encoding="utf-8") as handle:
        json.dump([1, 2, 3], handle)

    assert last_run.read(str(tmp_path)) is None


def test_write_replaces_rather_than_merges(tmp_path):
    last_run.write(str(tmp_path), {"exit_reason": "ok", "requests": 1500})
    last_run.write(str(tmp_path), {"exit_reason": "standdown"})

    loaded = last_run.read(str(tmp_path))
    assert loaded["exit_reason"] == "standdown"
    # Last night's request count surviving into a night that never ran would be
    # a report that reads as a successful crawl.
    assert "requests" not in loaded


def test_write_leaves_no_partial_behind(tmp_path):
    last_run.write(str(tmp_path), {"exit_reason": "ok"})

    assert os.listdir(tmp_path) == [last_run.FILE_NAME]


def test_unwritable_directory_does_not_raise(tmp_path):
    # The share is on SMB and does go away. A report is not worth an exception
    # on the way out of a run that has already finished its real work.
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("", encoding="utf-8")

    last_run.write(str(blocked / "state"), {"exit_reason": "ok"})


def test_reasons_match_the_collector_exit_codes():
    # These strings are the dashboard's whole vocabulary for why a night did
    # nothing; drift between them and collect.py is silent.
    assert set(last_run.REASONS) == {"ok", "blocked", "standdown", "no_gods"}


def test_frontier_counts_agree_with_summary(tmp_path):
    frontier_module = pytest.importorskip("frontier")

    frontier = frontier_module.Frontier(str(tmp_path))
    frontier.add("steam", "alice", "2026-08-01")
    frontier.add("steam", "bob", "2026-08-01")

    counts = frontier.counts()
    summary = frontier.summary()

    assert counts["total"] == 2
    assert counts["unvisited"] == 2
    # The prose and the fields are the same numbers, so a dashboard and a Job
    # log can never disagree about the roster.
    assert f"{counts['total']:,} players known" in summary
    assert f"{counts['unvisited']:,} never queried" in summary


class TestTheCollectorWritesIt:
    """The crawl itself, driven end to end against a fake tracker.

    Shares the shape of `test_collector_abort`'s harness deliberately: the same
    fake provider and client, so what these assert about the record is asserted
    about the same run that file asserts about the rows.
    """

    @staticmethod
    def _collect():
        pytest.importorskip("pandas")
        pytest.importorskip("curl_cffi")
        pytest.importorskip("ijson")
        return pytest.importorskip("collect")

    @pytest.fixture
    def crawling(self, tmp_path, monkeypatch):
        import types

        collect = self._collect()

        corpus = tmp_path / "output"
        state = tmp_path / "state"
        corpus.mkdir()
        state.mkdir()

        class God:
            def __init__(self, identifier, name):
                self.id = identifier
                self.name = name

        class Provider:
            def __init__(self, *args, **kwargs):
                self.gods = {1: God(1, "Anubis")}

            async def create(self):
                return None

            def god_by_name(self, name):
                return self.gods[1] if name == "Anubis" else None

        class Client:
            def __init__(self, *args, **kwargs):
                self.requests = 7
                self.bytes = 2048
                self.rate_limited = 2
                self.interval = 2.25

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get_json(self, path, params=None):
                return {"data": {"items": []}}

            async def iter_matches(self, platform, handle, page=0):
                self.requests += 1
                return
                yield  # pragma: no cover  (an empty async generator)

        async def no_address(url=None, timeout=10.0):
            return None

        async def seed_one(client, frontier, today, silent):
            frontier.add("steam", "76561198000000001", today)
            return 1

        monkeypatch.setattr(collect, "Smite2Provider", Provider)
        monkeypatch.setattr(collect, "TrackerClient", Client)
        monkeypatch.setattr(collect.egress_module, "observed_ip", no_address)
        monkeypatch.setattr(
            collect.paths, "game_match_data_dir", lambda game: str(corpus)
        )
        monkeypatch.setattr(collect.paths, "game_model_dir", lambda game: str(state))
        monkeypatch.setattr(collect, "seed", seed_one)

        def args(**overrides):
            base = dict(
                budget=100,
                hours=1.0,
                interval=1.5,
                jitter=0.0,
                coverage_target=0.0,
                pages=1,
                since_days=0,
                horizon=None,
                revisit=True,
                flush_every=50_000,
                dry_run=False,
                quiet=True,
                reset_clearance=False,
                reset_cooldown=False,
            )
            base.update(overrides)
            return types.SimpleNamespace(**base)

        return collect, str(state), args

    def test_a_clean_run_records_its_numbers(self, crawling):
        collect, state, args = crawling

        assert asyncio.run(collect.crawl(args())) == 0

        record = last_run.read(state)
        assert record["exit_reason"] == "ok"
        assert record["requests"] > 0
        assert record["rate_limited"] == 2
        assert record["final_interval"] == 2.25
        assert record["budget"] == 100
        assert record["frontier"]["total"] >= 1
        assert record["elapsed_seconds"] >= 0

    def test_a_dry_run_writes_nothing(self, crawling):
        collect, state, args = crawling

        asyncio.run(collect.crawl(args(dry_run=True)))

        # --dry-run promises to write nothing. A report is still a write, and
        # one that would overwrite the last real night's record with a run that
        # deliberately collected nothing.
        assert last_run.read(state) is None

    def test_a_standdown_is_recorded_rather_than_silent(self, crawling):
        collect, state, args = crawling

        cooldown_module = pytest.importorskip("smite2.cooldown")
        cooldown = cooldown_module.Cooldown(
            os.path.join(state, cooldown_module.FILE_NAME)
        )
        cooldown.arm(3600, "429 on /matches asking for 3600s")

        assert asyncio.run(collect.crawl(args())) == 3

        record = last_run.read(state)
        assert record is not None, "a night that refused to start left no trace"
        assert record["exit_reason"] == "standdown"
        # The reason verbatim: it is the only thing that distinguishes a ban
        # being served from a crawl that is merely idle.
        assert "3600s" in record["standdown"]["reason"]
        assert record["standdown"]["remaining_seconds"] > 0
        assert "requests" not in record, "it never made one"


def test_coverage_snapshot_mirrors_the_report():
    coverage_module = pytest.importorskip("coverage")

    tracker = coverage_module.CoverageTracker()
    for index in range(12):
        tracker.observe("2026-08-05", f"match-{index}", f"player-{index}")

    rows = tracker.snapshot()

    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-08-05"
    assert row["seen"] == 12
    assert row["half_a"] + row["half_b"] >= row["seen"]
    # Every day the printed table shows is a row here, in the same order.
    assert row["date"] in tracker.report()
