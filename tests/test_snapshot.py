"""What the public site is allowed to know, and how it fails.

The whole design of the snapshot is a safety property: smite.diemer.codes is
public, and both things it reports on are metered — 75,000 Hi-Rez requests and
500 sessions a day, and twelve Cloudflare clearance solves. Nothing reachable
from a URL may spend either. That is enforced by the web tier never calling a
third party, which in turn rests on this job being the only thing that does.

So these cover the two ways that quietly stops being true:

  - reading clearance state through `ClearanceManager` instead of
    `ClearanceStore`. The names are one word apart and the manager *mints* — a
    monitor that spends the budget it reports on is worse than none.
  - a section that raises instead of degrading. The corpus is on SMB and does
    go away; a share wobble must cost one card, not the page.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "HirezAPI"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "match_data_collector"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "web"))

pytest.importorskip("pandas")
snapshot = pytest.importorskip("snapshot")

cooldown_module = pytest.importorskip("smite2.cooldown")
clearance_module = pytest.importorskip("smite2.clearance")
egress = pytest.importorskip("smite2.egress")
last_run = pytest.importorskip("smite2.last_run")


@pytest.fixture(autouse=True)
def no_ambient_proxy(monkeypatch):
    monkeypatch.delenv(egress.ENV_VAR, raising=False)
    monkeypatch.setattr(egress, "proxy_url", lambda: None)


class TestSectionsDegrade:
    def test_a_raising_section_becomes_an_error_field(self):
        def boom():
            raise OSError("the share went away")

        result = snapshot.section("corpus", boom)

        assert result["error"].startswith("OSError")
        assert "share went away" in result["error"]

    def test_a_working_section_is_returned_untouched(self):
        assert snapshot.section("x", lambda: {"files": 3}) == {"files": 3}

    def test_an_empty_corpus_is_reported_not_raised(self, tmp_path):
        assert snapshot.corpus_section(str(tmp_path)) == {
            "files": 0,
            "newest": None,
            "newest_at": None,
        }

    def test_a_missing_directory_is_reported_not_raised(self, tmp_path):
        result = snapshot.corpus_section(str(tmp_path / "never-created"))

        assert result["files"] == 0

    def test_a_missing_aggregate_is_reported_not_raised(self, tmp_path):
        result = snapshot.aggregate_section(str(tmp_path))

        assert result == {"built": None, "newest": None, "files": 0, "rows": 0}

    def test_uncounted_rows_are_null_not_zero(self, tmp_path):
        """A manifest that predates row counting must not read as an empty corpus.

        Smite 1's real manifest carries UNKNOWN (-1) on all 3,306 entries, which
        summed to a confident `rows: 0` — the same thing an aggregate built over
        nothing would report, and the one number on that card anyone would act on.
        """
        manifest_module = pytest.importorskip("manifest")
        import datetime

        entries = [
            manifest_module.Entry(
                path=f"/corpus/match_details_2026-08-0{n}.parquet",
                name=f"match_details_2026-08-0{n}.parquet",
                size=100,
                mtime=1,
                rows=manifest_module.UNKNOWN,
            )
            for n in range(1, 4)
        ]
        manifest_module.write(
            str(tmp_path),
            manifest_module.Manifest(
                entries=entries,
                newest=datetime.date(2026, 8, 3),
                built=datetime.date(2026, 8, 4),
            ),
        )

        result = snapshot.aggregate_section(str(tmp_path))

        assert result["rows"] is None, "uncounted rows reported as a real count"
        assert result["unknown_rows"] == 3
        assert result["files"] == 3

    def test_missing_crawl_state_is_reported_not_raised(self, tmp_path):
        result = snapshot.crawl_section(str(tmp_path))

        assert result["frontier"] is None
        assert result["matches_collected"] == 0


class TestCorpus:
    def test_newest_is_the_latest_day_not_the_latest_write(self, tmp_path):
        # Files are named by the day they cover, and a re-write of an old day
        # must not make it look like the corpus is current.
        for day in ("2026-08-01", "2026-08-03", "2026-08-02"):
            (tmp_path / f"match_details_{day}.parquet").write_bytes(b"x")

        result = snapshot.corpus_section(str(tmp_path))

        assert result["files"] == 3
        assert result["newest"] == "match_details_2026-08-03.parquet"
        assert result["newest_at"] > 0

    def test_both_directories_are_counted(self, tmp_path):
        live = tmp_path / "output"
        archive = tmp_path / "archive"
        live.mkdir()
        archive.mkdir()
        (live / "match_details_2026-08-03.parquet").write_bytes(b"x")
        (archive / "match_details_2026-01-01.parquet").write_bytes(b"x")

        result = snapshot.corpus_section(str(live), str(archive))

        assert result["files"] == 2
        assert result["newest"] == "match_details_2026-08-03.parquet"


class TestTracker:
    """The two block signals, kept apart exactly as they are on disk."""

    def test_a_quiet_egress_reports_no_block(self, tmp_path):
        result = snapshot.tracker_section(str(tmp_path))

        assert result["standdown"]["active"] is False
        assert result["clearance"]["blocked"] is False
        assert result["clearance"]["cookie"] is None
        assert result["clearance"]["mints_limit"] == clearance_module.MAX_MINTS_PER_DAY

    def test_an_armed_standdown_surfaces_with_its_reason(self, tmp_path):
        cooldown = cooldown_module.Cooldown(
            str(tmp_path / cooldown_module.FILE_NAME), egress=egress.DIRECT
        )
        cooldown.arm(3600, "429 on /matches asking for 3600s")

        result = snapshot.tracker_section(str(tmp_path))

        assert result["standdown"]["active"] is True
        assert result["standdown"]["remaining_seconds"] > 3000
        # Verbatim. The reason is the only thing that tells someone which lever
        # to reach for, and paraphrasing it here would lose the Retry-After.
        assert result["standdown"]["reason"] == "429 on /matches asking for 3600s"

    def test_an_expired_standdown_is_not_active(self, tmp_path):
        path = str(tmp_path / cooldown_module.FILE_NAME)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": cooldown_module.SCHEMA_VERSION,
                    "egress": {
                        egress.DIRECT: {
                            "until": time.time() - 60,
                            "reason": "over",
                            "armed_at": time.time() - 3660,
                        }
                    },
                },
                handle,
            )

        result = snapshot.tracker_section(str(tmp_path))

        assert result["standdown"]["active"] is False
        assert result["standdown"]["remaining_seconds"] == 0

    def test_a_tripped_clearance_breaker_surfaces(self, tmp_path):
        from smite2.provider import CLEARANCE_FILE

        store = clearance_module.ClearanceStore(
            str(tmp_path / CLEARANCE_FILE), egress=egress.DIRECT
        )
        state = store.load()
        state.blocked_until = time.time() + 4 * 3600
        state.mints = [time.time() - 60] * 12
        store.save(state)

        result = snapshot.tracker_section(str(tmp_path))

        assert result["clearance"]["blocked"] is True
        assert result["clearance"]["mints_today"] == 12
        # A WAF ban and a solver backoff are different failures with different
        # fixes; merging them would send someone to the wrong lever.
        assert result["standdown"]["active"] is False

    def test_reading_the_clearance_state_never_mints(self, tmp_path, monkeypatch):
        """The safety property this whole file exists for.

        `ClearanceManager.get()` solves a challenge when it has no usable
        cookie — one of twelve a day. If a fifteen-minute monitor ever reaches
        for the manager instead of the store, it exhausts the budget by lunch
        and the nightly crawl has nothing left to mint with.
        """

        def forbidden(*args, **kwargs):
            raise AssertionError("the snapshot tried to mint a clearance cookie")

        monkeypatch.setattr(clearance_module.ClearanceManager, "get", forbidden)
        monkeypatch.setattr(clearance_module.ClearanceManager, "__init__", forbidden)

        result = snapshot.tracker_section(str(tmp_path))

        assert result["clearance"]["mints_today"] == 0

    def test_it_reads_its_own_egress_bucket(self, tmp_path, monkeypatch):
        # A ban is issued to an address. Reporting another egress's stand-down
        # would say the crawl is blocked when it is not, and vice versa.
        proxied = "http://gate.example.net:8080"
        cooldown_module.Cooldown(
            str(tmp_path / cooldown_module.FILE_NAME),
            egress=egress.identity(proxied),
        ).arm(3600, "banned on the proxy")

        assert snapshot.tracker_section(str(tmp_path))["standdown"]["active"] is False

        monkeypatch.setattr(egress, "proxy_url", lambda: proxied)
        result = snapshot.tracker_section(str(tmp_path))

        assert result["standdown"]["active"] is True
        assert result["egress"] == egress.identity(proxied)


class TestLastRunFlowsThrough:
    def test_a_recorded_standdown_reaches_the_snapshot(self, tmp_path):
        last_run.write(
            str(tmp_path), {"exit_reason": "standdown", "elapsed_seconds": 0.1}
        )

        assert last_run.read(str(tmp_path))["exit_reason"] == "standdown"


class TestCompetitiveQueues:
    """`getqueuestats` labels rows "<category>: <mode>", not by enum name."""

    @pytest.mark.parametrize(
        "name",
        ["Normal: Conquest", "Ranked: Conquest", "Normal: Slash", "Duel"],
    )
    def test_real_queues_count(self, name):
        assert snapshot._competitive(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Custom: Joust",
            "Training: Easy Bots (Solo)",
            "custom: arena",
            "Bot: Whatever",
        ],
    )
    def test_bot_and_custom_queues_do_not(self, name):
        # Unfiltered, "Training: Easy Bots (Solo)" at 100% wins every roster
        # member's best queue, which is true and tells a reader nothing.
        assert snapshot._competitive(name) is False


class TestWriting:
    def test_write_is_atomic_and_leaves_no_partial(self, tmp_path):
        target = snapshot.write(str(tmp_path), snapshot.STATUS_FILE, {"version": 1})

        assert os.path.basename(target) == snapshot.STATUS_FILE
        assert os.listdir(tmp_path) == [snapshot.STATUS_FILE]
        with open(target, "r", encoding="utf-8") as handle:
            assert json.load(handle) == {"version": 1}

    def test_the_two_documents_are_separate_files(self):
        # The cheap fifteen-minute job must not be able to blank the expensive
        # six-hourly one by running first.
        assert snapshot.STATUS_FILE != snapshot.PLAYERS_FILE

    def test_write_creates_the_directory(self, tmp_path):
        target = snapshot.write(
            str(tmp_path / "web"), snapshot.PLAYERS_FILE, {"players": []}
        )

        assert os.path.exists(target)


class TestRosterPrivacy:
    def test_the_public_view_carries_no_discord_ids(self):
        import roster

        # The site publishes game handles. A Discord id is an account
        # identifier for a different service and has no business on a page
        # anyone can load.
        for name in roster.SMITE_USERNAMES:
            assert isinstance(name, str)
            assert not name.isdigit()
        assert len(roster.SMITE_USERNAMES) == len(roster.DISCORD_TO_SMITE)

    def test_the_public_view_is_stably_ordered(self):
        import roster

        assert list(roster.SMITE_USERNAMES) == sorted(
            roster.DISCORD_TO_SMITE.values(), key=str.lower
        )
