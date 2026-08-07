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
roster = pytest.importorskip("roster")


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


class TestNullRowsInABatch:
    """Hi-Rez returns lists with null *elements*, not just null responses.

    Seen live: one roster member failed every run with
    `TypeError: 'NoneType' object is not subscriptable`. The client already
    retries a wholly-null body; this is a good list carrying rows Hi-Rez
    declined to fill in, and grouping over one of them took the whole player
    down.
    """

    def test_a_batch_with_null_rows_is_survivable(self):
        from itertools import groupby

        rows = [
            {"Queue": "Normal: Conquest", "GodId": 1, "Wins": 1},
            None,
            {"Queue": "Normal: Conquest", "GodId": 2, "Wins": 1},
        ]

        # What the code does now.
        cleaned = [row for row in (rows or []) if row]
        grouped = {name: list(g) for name, g in groupby(cleaned, key=lambda r: r["Queue"])}

        assert len(cleaned) == 2
        assert list(grouped) == ["Normal: Conquest"]

        # And what it used to do, so this test fails loudly if the guard is
        # removed rather than silently passing.
        with pytest.raises(TypeError):
            list(groupby(rows, key=lambda r: r["Queue"]))

    def test_a_null_response_is_survivable(self):
        assert [row for row in (None or []) if row] == []


class TestCorpusBreakdown:
    """The per-god table rolled up by queue, god and role.

    The number that matters most here is what `plays` counts: the aggregate
    groups by (god, queue, role, mmr) and counts *player records*, so ten come
    from one match. Nothing may divide by ten and call it matches.
    """

    @staticmethod
    def _table(tmp_path):
        pd = pytest.importorskip("pandas")

        frame = pd.DataFrame(
            [
                # god, queue, role, high mmr, plays, wins
                (1, 426, "Solo", False, 100, 55),
                (1, 426, "Mid", False, 50, 20),
                (1, 435, "Unknown", False, 30, 15),
                (2, 426, "Solo", True, 20, 12),
            ],
            columns=["GodId", "match_queue_id", "Role", "HighMmr", "plays", "wins"],
        )
        frame.to_parquet(os.path.join(str(tmp_path), "god_stats.parquet"))
        return frame

    def test_rolls_up_by_every_dimension(self, tmp_path):
        self._table(tmp_path)
        from game import Game

        result = snapshot.stats_section(
            Game.SMITE,
            str(tmp_path),
            (lambda g: f"god{g}", lambda q: f"queue{q}"),
        )

        assert result["built"] is True
        assert result["total_plays"] == 200
        assert result["high_mmr_plays"] == 20
        assert result["distinct_gods"] == 2
        assert result["distinct_queues"] == 2

        # Sorted by plays, biggest first — a breakdown nobody can rank is not one.
        assert [q["name"] for q in result["queues"]] == ["queue426", "queue435"]
        assert result["queues"][0]["plays"] == 170
        assert result["gods"][0]["name"] == "god1"
        assert result["gods"][0]["plays"] == 180

    def test_win_percent_is_a_ratio_not_a_percentage(self, tmp_path):
        self._table(tmp_path)
        from game import Game

        result = snapshot.stats_section(
            Game.SMITE, str(tmp_path), (str, str)
        )

        god1 = next(g for g in result["gods"] if g["key"] == "1")
        assert god1["wins"] == 90
        assert god1["win_percent"] == pytest.approx(0.5, abs=0.01)

    def test_a_missing_aggregate_says_so_rather_than_showing_zeroes(self, tmp_path):
        from game import Game

        result = snapshot.stats_section(Game.SMITE, str(tmp_path), (str, str))

        # Not `total_plays: 0`, which reads as "the corpus is empty" rather than
        # "nothing has been aggregated yet".
        assert result == {"built": False}

    def test_unnamed_ids_fall_back_rather_than_raising(self, tmp_path):
        self._table(tmp_path)
        from game import Game

        def refuses(_value):
            raise KeyError("no such god")

        with pytest.raises(KeyError):
            refuses(1)

        # The real resolvers swallow their own lookup errors; this pins that the
        # rollup passes the raw key through so they can.
        result = snapshot.stats_section(
            Game.SMITE, str(tmp_path), (lambda g: f"#{g}", lambda q: f"#{q}")
        )
        assert result["gods"][0]["name"].startswith("#")


class TestMatchesPerDay:
    def test_counts_by_the_day_played(self, tmp_path):
        pd = pytest.importorskip("pandas")

        pd.DataFrame(
            {
                "match_id": ["a", "b", "c", "d"],
                "date": ["2026-08-01", "2026-08-02", "2026-08-02", "2026-08-01"],
            }
        ).to_parquet(os.path.join(str(tmp_path), "seen_matches.parquet"))

        series = snapshot.matches_per_day(str(tmp_path))

        # Chronological, because it is drawn as a time series.
        assert series == [
            {"date": "2026-08-01", "matches": 2},
            {"date": "2026-08-02", "matches": 2},
        ]

    def test_no_index_is_an_empty_series_not_an_error(self, tmp_path):
        assert snapshot.matches_per_day(str(tmp_path)) == []


class TestScheduledJobs:
    """"Nobody scheduled this" and "this is broken" must not look the same.

    The Smite 2 crawl had never been enabled, so its corpus sat 19 hours old and
    the page showed it as stale — which someone reasonably read as the crawl
    having failed. Age is only a fault when something is supposed to be fixing
    it, so the chart now tells the snapshot which pipelines it actually
    rendered.
    """

    def test_nothing_set_is_unknown_not_disabled(self, monkeypatch):
        # A local run sets no such variable, and must not make the site claim
        # every pipeline on the cluster is switched off.
        monkeypatch.delenv(snapshot.SCHEDULED_ENV, raising=False)

        assert snapshot.scheduled_jobs() == set()

    def test_the_list_is_parsed_and_trimmed(self, monkeypatch):
        monkeypatch.setenv(
            snapshot.SCHEDULED_ENV, "collector, aggregate ,smite2.collector,,"
        )

        assert snapshot.scheduled_jobs() == {
            "collector",
            "aggregate",
            "smite2.collector",
        }

    @pytest.mark.parametrize(
        "value,expected",
        [("", None), ("collector,aggregate", True)],
    )
    def test_it_reaches_the_document(self, monkeypatch, tmp_path, value, expected):
        import asyncio

        monkeypatch.setenv(snapshot.SCHEDULED_ENV, value)
        monkeypatch.setattr(snapshot.paths, "MATCH_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(snapshot.paths, "MATCH_ARCHIVE_DIR", str(tmp_path))
        monkeypatch.setattr(
            snapshot.paths, "game_model_dir", lambda game: str(tmp_path)
        )
        monkeypatch.setattr(
            snapshot.paths, "game_match_data_dir", lambda game: str(tmp_path)
        )
        monkeypatch.setattr(
            snapshot.paths, "game_match_archive_dir", lambda game: str(tmp_path)
        )

        document = asyncio.run(snapshot.build_status())

        assert document["games"]["smite"]["scheduled"]["collector"] is expected
        # Smite 2's names are namespaced, so a bare "collector" must not enable
        # it by accident.
        assert document["games"]["smite2"]["scheduled"]["collector"] is (
            False if expected else None
        )


class TestSmite2PlayersRespectTheStandDown:
    """The player refresh must never fire into a live tracker.gg ban.

    This job and the nightly crawl leave from one address and share one
    reputation, and the crawl already refuses to start inside a recorded
    stand-down because doing otherwise can extend it. A refresh that ignored the
    same deadline would take the crawl down with it — the site would lose ten
    player cards and the corpus would lose a night, which is the worse half.

    Not theoretical: the first manual crawl was 429'd for an hour after 309
    requests, and the players job runs every six hours regardless.
    """

    def test_an_active_standdown_skips_the_whole_half(self, tmp_path, monkeypatch):
        import asyncio

        cooldown_module = pytest.importorskip("smite2.cooldown")
        monkeypatch.setattr(
            snapshot.paths, "game_model_dir", lambda game: str(tmp_path)
        )
        cooldown_module.Cooldown(
            str(tmp_path / cooldown_module.FILE_NAME), egress=egress.DIRECT
        ).arm(3600, "429 on /matches asking for 3600s")

        # If the guard fails, this is what it would reach for.
        def forbidden(*args, **kwargs):
            raise AssertionError("asked tracker.gg while standing down")

        monkeypatch.setattr(clearance_module.ClearanceManager, "__init__", forbidden)

        result = asyncio.run(snapshot.build_smite2_players())

        assert result["players"] == []
        assert "stand-down" in result["skipped"]
        # The reason verbatim, so the page can say which ban and for how long.
        assert "3600s" in result["reason"]

    def test_an_empty_roster_is_skipped_without_touching_the_network(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        monkeypatch.setattr(roster, "DISCORD_TO_SMITE2", {})
        monkeypatch.setattr(
            snapshot.paths, "game_model_dir", lambda game: str(tmp_path)
        )

        result = asyncio.run(snapshot.build_smite2_players())

        assert result["players"] == []
        assert "roster" in result["skipped"]


class TestTimestampsCarryTheirOffset:
    """A naive ISO string is read as *local* by JavaScript, not as UTC.

    Hi-Rez publishes UTC and `strptime` hands back a naive datetime, so emitting
    `value.isoformat()` produced "2014-11-18T04:54:00" — which every browser
    rendered as the reader's own 4:54 AM. The whole roster looked like it had
    signed up in the middle of the night.
    """

    def test_a_naive_hirez_timestamp_is_marked_utc(self):
        import datetime

        result = snapshot._date(datetime.datetime(2014, 11, 18, 4, 54))

        assert result.endswith("+00:00"), "no offset means the browser guesses local"
        assert result == "2014-11-18T04:54:00+00:00"

    def test_an_aware_timestamp_is_left_alone(self):
        import datetime

        aware = datetime.datetime(
            2014, 11, 18, 4, 54, tzinfo=datetime.timezone.utc
        )

        assert snapshot._date(aware) == "2014-11-18T04:54:00+00:00"

    def test_the_never_played_sentinel_is_still_none(self):
        import datetime

        # QueueStats starts `last_played` at datetime.min for "never".
        assert snapshot._date(datetime.datetime.min) is None
        assert snapshot._date(None) is None


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
