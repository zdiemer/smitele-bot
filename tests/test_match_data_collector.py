"""What the Smite 1 collector does when Hi-Rez answers badly.

Every failure here is quiet by nature: a queue-window that errors still leaves
the other eleven queues to print a plausible count, and a batch of ten match
details that never arrives leaves no gap in anything the run prints. The two
that were not quiet were worse — a spent retry budget arrives as a builtin
ConnectionError or a TimeoutError, neither of which the collector used to
catch, so one slow response ended the whole day's collection.
"""

from __future__ import annotations

import asyncio
import datetime
import os

import pytest

pytest.importorskip("pandas", reason="pandas not installed")
mdc = pytest.importorskip(
    "match_data_collector", reason="collector deps not installed"
)

from HirezAPI import QueueId  # noqa: E402


class Provider:
    """Serves canned answers per (queue, hour, minute) and counts calls."""

    MAX_RETRIES = 3

    def __init__(self, ids_answer=None, details_answer=None):
        self.ids_answer = ids_answer or (lambda q, h, m: [])
        self.details_answer = details_answer or (lambda chunk: [])
        self.id_calls = []
        self.detail_calls = []

    async def get_match_ids_by_queue(self, queue, _date, hour, minute):
        self.id_calls.append((queue, hour, minute))
        answer = self.ids_answer(queue, hour, minute)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def get_match_details_batch(self, chunk):
        self.detail_calls.append(tuple(chunk))
        answer = self.details_answer(tuple(chunk))
        if isinstance(answer, Exception):
            raise answer
        return answer


def done(match_id):
    """A completed match, the only kind the collector keeps."""
    return {"Match": match_id, "Active_Flag": "n"}


def collector(provider, queues=None, date="2026-08-06"):
    return mdc.MatchDataCollector(
        provider,
        datetime.datetime.strptime(date, "%Y-%m-%d"),
        queues=queues or [QueueId.CONQUEST],
    )


def fetch_ids(provider, **kwargs):
    inst = collector(provider, **kwargs)
    return asyncio.run(inst._MatchDataCollector__fetch_match_ids())


def fetch_details(provider, **kwargs):
    inst = collector(provider, **kwargs)
    return asyncio.run(inst._MatchDataCollector__fetch_match_details())


class TestIdFetchSurvivesFailures:
    """A failed queue-window costs that window, never the run."""

    @pytest.mark.parametrize(
        "error",
        [
            ConnectionError("Failed to connect"),
            asyncio.TimeoutError(),
            TimeoutError(),
        ],
        ids=["builtin-ConnectionError", "asyncio-TimeoutError", "builtin-TimeoutError"],
    )
    def test_a_spent_retry_budget_does_not_end_the_day(self, error):
        """The regression that killed a real run.

        `_make_request` raises the *builtin* ConnectionError once its retries
        are spent, and a timeout arrives as TimeoutError. The collector caught
        aiohttp's ClientConnectionError, which is neither of them, so both went
        straight past and out of the job.
        """
        # Fails only in the very first window; every later one answers.
        def answer(_queue, hour, minute):
            if (hour, minute) == (0, 0):
                return error
            return [done(f"{hour}-{minute}")]

        found = fetch_ids(Provider(ids_answer=answer))

        assert len(found) == 143, "142 later windows plus nothing for the failed one"

    def test_an_error_object_instead_of_a_list_is_skipped(self):
        """Hi-Rez answers a bad request with a lone dict carrying a ret_msg.

        Iterating that walks its keys, and subscripting a key raised TypeError
        out of the whole job.
        """
        def answer(_queue, hour, _minute):
            if hour == 0:
                return {"ret_msg": "Invalid session id."}
            return [done(f"{hour}")]

        found = fetch_ids(Provider(ids_answer=answer))

        assert len(found) == 23, "one match per surviving hour, deduplicated"

    def test_every_window_is_still_asked_for(self):
        provider = Provider()
        fetch_ids(provider)

        assert len(provider.id_calls) == 144
        assert provider.id_calls[0][1:] == (0, 0)
        assert provider.id_calls[-1][1:] == (23, 50)

    def test_failures_are_counted_and_reported(self, capsys):
        """Without this the run prints a normal-looking count from the queues
        that did answer, and a day missing a whole queue reads as healthy."""
        provider = Provider(ids_answer=lambda *_: ConnectionError("nope"))
        fetch_ids(provider)

        out = capsys.readouterr().out
        assert "WARNING: 144 of 144 queue-window requests failed" in out
        assert "this day is incomplete" in out

    def test_a_clean_run_warns_about_nothing(self, capsys):
        fetch_ids(Provider(ids_answer=lambda _q, h, m: [done(f"{h}-{m}")]))

        assert "WARNING" not in capsys.readouterr().out

    def test_a_day_past_detail_retention_says_so(self, capsys):
        """Active_Flag is "y" for every match older than about 31 days, and
        those IDs yield zero rows from getmatchdetailsbatch. A backfill aimed
        past the edge otherwise prints a total of zero and looks identical to a
        day nobody played."""
        provider = Provider(
            ids_answer=lambda _q, h, m: [
                {"Match": f"{h}-{m}-{i}", "Active_Flag": "y"} for i in range(3)
            ]
        )
        found = fetch_ids(provider)

        out = capsys.readouterr().out
        assert found == []
        assert "432 of 432 match IDs are past Hi-Rez's detail retention" in out

    def test_a_collectable_day_says_nothing_about_retention(self, capsys):
        fetch_ids(Provider(ids_answer=lambda _q, h, m: [done(f"{h}-{m}")]))

        assert "detail retention" not in capsys.readouterr().out

    def test_an_error_inside_a_list_is_not_called_expired(self, capsys):
        """The miscount that made a recoverable gap look permanent.

        Hi-Rez also returns its errors as a one-element *list* around a ret_msg
        dict, which clears the isinstance check that catches the bare-dict form.
        Every entry then failed the `== "n"` test and landed in the retention
        counter, so 2026-08-02 reported "1,728 of 1,728 match IDs past
        retention" — one per queue-window, not per match — for a day that was
        fetchable all along.
        """
        provider = Provider(
            ids_answer=lambda *_: [{"ret_msg": "Something went wrong."}]
        )
        found = fetch_ids(provider)

        out = capsys.readouterr().out
        assert found == []
        assert "detail retention" not in out, "a bad answer is not an old day"
        assert "WARNING: 144 of 144 queue-window requests failed" in out
        assert "this day is incomplete" in out

    def test_a_window_that_half_answers_keeps_what_it_gave(self):
        """A payload carrying real matches is used even if junk rides along."""
        provider = Provider(
            ids_answer=lambda _q, h, m: [
                {"ret_msg": "partial"},
                done(f"{h}-{m}"),
            ]
        )

        assert len(fetch_ids(provider)) == 144

    def test_an_empty_window_is_not_a_failure(self, capsys):
        """Nobody played in those ten minutes. That is an answer, not an error,
        and counting it would mark every quiet night incomplete."""
        fetch_ids(Provider(ids_answer=lambda *_: []))

        assert "WARNING" not in capsys.readouterr().out

    def test_retention_still_counts_matches_not_windows(self, capsys):
        """The counter has to stay per-match, which is what made the bogus
        1,728 recognisable as a per-request tally in the first place."""
        provider = Provider(
            ids_answer=lambda _q, h, m: [
                {"Match": f"{h}-{m}-{i}", "Active_Flag": "y"} for i in range(3)
            ]
        )
        fetch_ids(provider)

        assert "432 of 432" in capsys.readouterr().out, "144 windows x 3"

    def test_matches_still_in_progress_are_left_out(self):
        provider = Provider(
            ids_answer=lambda _q, h, m: [
                done(f"done-{h}-{m}"),
                {"Match": f"live-{h}-{m}", "Active_Flag": "y"},
            ]
        )
        found = fetch_ids(provider)

        assert len(found) == 144
        assert not any(str(m).startswith("live") for m in found)


class TestDetailFetchAccountsForWhatItLoses:
    def test_a_failing_chunk_is_retried_after_the_run(self, capsys):
        """The chunks that fail do so in a burst. Asking again once the burst
        has passed is what actually recovers them — so the failure has to
        outlast the three in-line attempts to be worth a second pass at all."""
        attempts = {}

        def burst(chunk):
            attempts[chunk] = attempts.get(chunk, 0) + 1
            # Exhausts the three in-line retries, then answers the sweep.
            if attempts[chunk] <= Provider.MAX_RETRIES:
                return ConnectionError("burst")
            return [{"Match": m} for m in chunk]

        provider = Provider(
            ids_answer=lambda _q, h, m: [done(f"{h}-{m}")], details_answer=burst
        )
        details = fetch_details(provider)

        out = capsys.readouterr().out
        assert len(details) == 144, "everything dropped in the first pass came back"
        assert "Retrying 144 match IDs" in out
        assert "Recovered 144 of 144" in out
        assert "WARNING" not in out

    def test_what_cannot_be_recovered_is_reported(self, capsys):
        provider = Provider(
            ids_answer=lambda _q, h, m: [done(f"{h}-{m}")],
            details_answer=lambda _chunk: ConnectionError("still down"),
        )
        details = fetch_details(provider)

        out = capsys.readouterr().out
        assert details == []
        assert "WARNING: 144 match IDs (100.0%) could not be fetched" in out

    def test_a_clean_detail_pass_says_nothing_about_retries(self, capsys):
        provider = Provider(
            ids_answer=lambda _q, h, m: [done(f"{h}-{m}")],
            details_answer=lambda chunk: [{"Match": m} for m in chunk],
        )
        fetch_details(provider)

        out = capsys.readouterr().out
        assert "Retrying" not in out
        assert "WARNING" not in out


class TestRangeSelection:
    """Backfilling 177 days needs a range, and needs to survive restarts."""

    def test_no_range_is_a_single_day(self, monkeypatch):
        monkeypatch.setattr(mdc.sys, "argv", ["collect", "2026-03-01"])
        monkeypatch.delenv("SMITELE_COLLECT_UNTIL", raising=False)

        assert mdc._dates_to_collect() == [datetime.datetime(2026, 3, 1)]

    def test_a_range_is_inclusive_and_runs_oldest_first(self, monkeypatch):
        """Oldest first because the old edge of the retention window expires a
        day every day; the newest missing day will still be there next week."""
        monkeypatch.setattr(mdc.sys, "argv", ["collect", "2026-03-01", "2026-03-04"])

        dates = mdc._dates_to_collect()

        assert dates == [datetime.datetime(2026, 3, d) for d in (1, 2, 3, 4)]

    def test_an_end_before_the_start_is_just_the_start(self, monkeypatch):
        monkeypatch.setattr(mdc.sys, "argv", ["collect", "2026-03-04", "2026-03-01"])

        assert mdc._dates_to_collect() == [datetime.datetime(2026, 3, 4)]

    def test_the_range_comes_from_the_environment_too(self, monkeypatch):
        monkeypatch.setattr(mdc.sys, "argv", ["collect"])
        monkeypatch.setenv("SMITELE_COLLECT_DATE", "2026-03-01")
        monkeypatch.setenv("SMITELE_COLLECT_UNTIL", "2026-03-03")

        assert len(mdc._dates_to_collect()) == 3


class TestAlreadyCollected:
    """A backfill is interrupted by definition — it outlives several pods."""

    def test_a_day_on_disk_is_recognised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mdc.paths, "MATCH_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(mdc.paths, "MATCH_ARCHIVE_DIR", str(tmp_path / "archive"))
        os.makedirs(tmp_path / "archive")
        (tmp_path / "match_details_2026-03-01.parquet").write_text("")

        assert collector(Provider(), date="2026-03-01").existing_corpus_file()
        assert collector(Provider(), date="2026-03-02").existing_corpus_file() is None

    def test_a_day_already_rotated_into_the_archive_counts(self, tmp_path, monkeypatch):
        """The run that backfills a day older than the archive cutoff rotates
        it out itself, so the next run finds `output` empty and would collect
        the whole day a second time."""
        archive = tmp_path / "archive"
        os.makedirs(archive)
        monkeypatch.setattr(mdc.paths, "MATCH_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(mdc.paths, "MATCH_ARCHIVE_DIR", str(archive))
        (archive / "match_details_2026-03-01.parquet").write_text("")

        assert collector(Provider(), date="2026-03-01").existing_corpus_file()

    def test_the_converted_part_files_count_as_collected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mdc.paths, "MATCH_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(mdc.paths, "MATCH_ARCHIVE_DIR", str(tmp_path / "archive"))
        os.makedirs(tmp_path / "archive")
        (tmp_path / "match_details_2026-03-01.part001.parquet").write_text("")

        assert collector(Provider(), date="2026-03-01").existing_corpus_file()

    def test_a_skipped_day_fetches_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(mdc.paths, "MATCH_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(mdc.paths, "MATCH_ARCHIVE_DIR", str(tmp_path / "archive"))
        os.makedirs(tmp_path / "archive")
        (tmp_path / "match_details_2026-03-01.parquet").write_text("")
        provider = Provider()

        asyncio.run(
            collector(provider, date="2026-03-01").run_job(skip_if_collected=True)
        )

        assert provider.id_calls == [], "a skipped day costs no requests"
        assert "already collected" in capsys.readouterr().out
