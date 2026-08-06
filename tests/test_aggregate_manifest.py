"""The aggregate's record of what it already counted.

Every test here is a shape that used to be counted wrongly, or a property the
incremental path depends on. The two that matter most are
`test_a_rewritten_file_is_detected` (a Smite 2 backfill mutates days that
already have files) and `test_an_archived_file_is_not_recounted` (a Smite 1 day
changes path when it rotates), because those are the two real bugs.
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "HirezAPI"),
)

pd = pytest.importorskip("pandas")
manifest = pytest.importorskip("manifest")

TODAY = datetime.date(2026, 8, 6)


def write_day(directory, date: str, rows: int) -> str:
    """A corpus file with a knowable number of rows."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"match_details_{date}.parquet")
    pd.DataFrame({"MatchId": range(rows), "GodId": [1] * rows}).to_parquet(
        path, compression="zstd", index=False
    )
    return path


def append_to_day(path: str, rows: int) -> None:
    """What `store.merge_day` does: read, concatenate, rewrite in place."""
    existing = pd.read_parquet(path)
    extra = pd.DataFrame(
        {"MatchId": range(10_000, 10_000 + rows), "GodId": [2] * rows}
    )
    pd.concat([existing, extra], ignore_index=True).to_parquet(
        path, compression="zstd", index=False
    )
    # Rewrites inside the same second are caught by size, but the collector's
    # real flushes land seconds apart; make the test deterministic either way.
    stamp = os.stat(path)
    os.utime(path, (stamp.st_atime + 60, stamp.st_mtime + 60))


def manifest_of(paths, newest=TODAY, built=TODAY) -> manifest.Manifest:
    return manifest.Manifest(
        entries=[manifest.entry_for(path) for path in paths],
        newest=newest,
        built=built,
    )


class TestMutation:
    def test_a_rewritten_file_is_detected(self, tmp_path):
        """The Smite 2 backfill bug: a day that grew was skipped as counted."""
        output = str(tmp_path / "output")
        day = write_day(output, "2026-08-05", 100)
        recorded = manifest_of([day])

        append_to_day(day, 500)

        plan = manifest.classify([day], recorded)
        assert [entry.path for entry in plan.changed] == [day]
        assert plan.pending == []
        assert plan.must_rebuild

    def test_an_untouched_corpus_stays_incremental(self, tmp_path):
        output = str(tmp_path / "output")
        days = [write_day(output, date, 50) for date in ("2026-08-04", "2026-08-05")]
        recorded = manifest_of(days)

        plan = manifest.classify(days, recorded)
        assert not plan.must_rebuild
        assert plan.pending == []
        assert {entry.path for entry in plan.carried} == set(days)

    def test_a_new_day_is_the_only_pending_file(self, tmp_path):
        output = str(tmp_path / "output")
        old = write_day(output, "2026-08-04", 50)
        recorded = manifest_of([old])
        new = write_day(output, "2026-08-05", 50)

        plan = manifest.classify([old, new], recorded)
        assert plan.pending == [new]
        assert not plan.must_rebuild


class TestIdentity:
    def test_an_archived_file_is_not_recounted(self, tmp_path):
        """The Smite 1 bug: rotating to the archive changed the path only.

        `corpus_paths` scans both directories and returns where it found the
        file, so before basenames were the identity this looked like a brand new
        day and every rotation double-counted itself.
        """
        output = str(tmp_path / "output")
        archive = str(tmp_path / "archive")
        day = write_day(output, "2026-07-01", 50)
        recorded = manifest_of([day])

        os.makedirs(archive, exist_ok=True)
        rotated = os.path.join(archive, os.path.basename(day))
        os.rename(day, rotated)

        plan = manifest.classify([rotated], recorded)
        assert plan.pending == []
        assert plan.changed == []
        assert [entry.path for entry in plan.carried] == [rotated]

    def test_a_counted_file_that_vanished_forces_a_rebuild(self, tmp_path):
        output = str(tmp_path / "output")
        day = write_day(output, "2026-08-05", 50)
        recorded = manifest_of([day])

        os.remove(day)

        plan = manifest.classify([], recorded)
        assert [entry.path for entry in plan.missing] == [day]
        assert plan.must_rebuild

    def test_duplicate_basenames_fall_back_to_paths(self, tmp_path):
        """A basename in two places must not match the wrong entry."""
        one = write_day(str(tmp_path / "a"), "2026-08-05", 50)
        two = write_day(str(tmp_path / "b"), "2026-08-05", 90)
        recorded = manifest_of([one, two])

        plan = manifest.classify([one, two], recorded)
        assert not plan.must_rebuild
        assert {entry.path for entry in plan.carried} == {one, two}


class TestWindowing:
    def test_days_truncation_does_not_narrow_what_is_recorded(self, tmp_path):
        """`--days` hides files; it must not forget they are in the totals.

        Dropping them would let the same day be folded in a second time the next
        time the window widened.
        """
        output = str(tmp_path / "output")
        days = [
            write_day(output, date, 50)
            for date in ("2026-08-03", "2026-08-04", "2026-08-05")
        ]
        recorded = manifest_of(days)

        plan = manifest.classify(days[-1:], recorded)
        assert not plan.must_rebuild
        assert {entry.path for entry in plan.carried} == set(days)


class TestRoundTrip:
    def test_the_manifest_survives_a_round_trip_and_leaves_no_partial(self, tmp_path):
        output = str(tmp_path / "output")
        out_dir = str(tmp_path / "model")
        days = [write_day(output, date, 50) for date in ("2026-08-04", "2026-08-05")]
        recorded = manifest_of(days, newest=TODAY, built=datetime.date(2026, 8, 1))

        manifest.write(out_dir, recorded)
        loaded = manifest.read(out_dir)

        assert loaded is not None
        assert loaded.newest == TODAY
        assert loaded.built == datetime.date(2026, 8, 1)
        assert {entry.path for entry in loaded.entries} == set(days)
        assert all(entry.rows == 50 for entry in loaded.entries)
        assert not any(name.endswith(".partial") for name in os.listdir(out_dir))

        # And it still classifies correctly after the round trip.
        assert not manifest.classify(days, loaded).must_rebuild

    def test_no_manifest_reads_as_none(self, tmp_path):
        assert manifest.read(str(tmp_path)) is None

    def test_a_rebuild_converges(self, tmp_path):
        """The lifecycle, as `build_aggregate` drives it.

        The failure this guards against is a rebuild that never settles: if what
        gets written back does not match what the next run measures, every run
        detects a change and rebuilds forever. For Smite 2 that is currently
        eight seconds, so it would go unnoticed until the corpus was large
        enough for it to matter.
        """
        output = str(tmp_path / "output")
        out_dir = str(tmp_path / "model")
        days = [write_day(output, date, 50) for date in ("2026-08-04", "2026-08-05")]

        # First run: nothing counted, so everything is pending.
        assert manifest.read(out_dir) is None
        manifest.write(out_dir, manifest_of(days))

        # Second run, corpus untouched: incremental, nothing to do.
        plan = manifest.classify(days, manifest.read(out_dir))
        assert not plan.must_rebuild and plan.pending == []

        # A backfill rewrites one day and adds another.
        append_to_day(days[0], 500)
        days.append(write_day(output, "2026-08-06", 50))

        plan = manifest.classify(days, manifest.read(out_dir))
        assert plan.must_rebuild

        # The rebuild re-reads everything and records it afresh.
        manifest.write(out_dir, manifest_of(days))

        # And the run after that is quiet again — the point of the test.
        plan = manifest.classify(days, manifest.read(out_dir))
        assert not plan.must_rebuild, "a rebuild that does not settle loops"
        assert plan.pending == []
        assert len(plan.carried) == 3

    def test_load_previous_needs_all_six_files(self, tmp_path):
        """The all-or-nothing rule: a partial set must not be folded into.

        `build_aggregate` reads the manifest through this, so the contract that
        it returns a `manifest` key — not a bare path set — is what the
        classification depends on.
        """
        build_aggregate = pytest.importorskip("build_aggregate")

        output = str(tmp_path / "output")
        out_dir = str(tmp_path / "model")
        day = write_day(output, "2026-08-05", 50)

        manifest.write(out_dir, manifest_of([day]))
        assert build_aggregate.load_previous(out_dir) is None, "no tables yet"

        empty = pd.DataFrame({"BuildHash": [], "Plays": []})
        empty.to_parquet(
            os.path.join(out_dir, build_aggregate.BUILD_PLAYS_NAME), index=False
        )
        for name in build_aggregate.OUTPUT_NAMES:
            empty.to_parquet(os.path.join(out_dir, f"{name}.parquet"), index=False)

        previous = build_aggregate.load_previous(out_dir)
        assert previous is not None
        assert previous["manifest"].newest == TODAY
        assert [entry.path for entry in previous["manifest"].entries] == [day]

        os.remove(os.path.join(out_dir, manifest.MANIFEST_NAME))
        assert build_aggregate.load_previous(out_dir) is None, "manifest gone"

    def test_a_manifest_without_fingerprints_forces_one_rebuild(self, tmp_path):
        """The migration: entries written before fingerprints existed.

        Unknown is not unchanged, so the first run after this ships rebuilds
        once and records real values rather than trusting a stale path list.
        """
        output = str(tmp_path / "output")
        out_dir = str(tmp_path / "model")
        day = write_day(output, "2026-08-05", 50)

        os.makedirs(out_dir, exist_ok=True)
        pd.DataFrame(
            {"path": [day], "newest": ["2026-08-05"], "built": ["2026-08-05"]}
        ).to_parquet(
            os.path.join(out_dir, manifest.MANIFEST_NAME),
            compression="zstd",
            index=False,
        )

        loaded = manifest.read(out_dir)
        assert loaded is not None
        assert not loaded.entries[0].known

        plan = manifest.classify([day], loaded)
        assert plan.must_rebuild
        assert [entry.path for entry in plan.changed] == [day]
