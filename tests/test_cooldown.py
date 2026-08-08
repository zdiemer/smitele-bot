"""The stand-down that outlives the run that earned it.

A backfill was refused with `Retry-After: 3600` — an hour, which is a ban rather
than a pause. Stopping was right and already worked; forgetting the deadline was
not, because the nightly would then fire into a live ban at 02:40, collect
nothing, and spend reputation doing it.

So these cover two things: that the number survives the process, and that a run
consults it before making a single request.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "HirezAPI"),
)

cooldown_module = pytest.importorskip("smite2.cooldown")
egress = pytest.importorskip("smite2.egress")

PROXIED = "http://gate.example.net:8080"


@pytest.fixture(autouse=True)
def no_ambient_proxy(monkeypatch):
    monkeypatch.delenv(egress.ENV_VAR, raising=False)
    monkeypatch.setattr(egress, "proxy_url", lambda: None)


def store(tmp_path, name=None):
    return cooldown_module.Cooldown(
        str(tmp_path / cooldown_module.FILE_NAME), egress=name or egress.DIRECT
    )


class TestPersistence:
    def test_a_standdown_survives_a_new_reader(self, tmp_path):
        """The whole point: the deadline outlives the process that learned it."""
        store(tmp_path).arm(3600, "429 asking for 3600s")

        fresh = store(tmp_path).read()
        assert fresh.active
        assert 3500 < fresh.remaining <= 3600
        assert "3600s" in fresh.reason

    def test_nothing_recorded_is_not_a_standdown(self, tmp_path):
        assert not store(tmp_path).read().active

    def test_an_elapsed_standdown_is_over(self, tmp_path):
        target = store(tmp_path)
        target.arm(1, "brief")
        raw = json.loads((tmp_path / cooldown_module.FILE_NAME).read_text())
        raw["egress"][egress.DIRECT]["until"] = time.time() - 5
        (tmp_path / cooldown_module.FILE_NAME).write_text(json.dumps(raw))

        assert not target.read().active

    def test_arming_never_shortens_one_in_force(self, tmp_path):
        """A later, milder refusal must not release an earlier, harsher one."""
        target = store(tmp_path)
        target.arm(3600, "the long one")
        target.arm(60, "the short one")
        assert target.read().remaining > 3000

    def test_an_absurd_duration_is_capped(self, tmp_path):
        """A bad header should not stand the crawl down for a week."""
        armed = store(tmp_path).arm(60 * 60 * 24 * 30, "nonsense")
        assert armed.remaining <= cooldown_module.MAX_COOLDOWN_SECONDS

    def test_clear_releases_it(self, tmp_path):
        target = store(tmp_path)
        target.arm(3600, "429")
        target.clear()
        assert not target.read().active

    def test_a_corrupt_file_is_not_a_standdown(self, tmp_path):
        (tmp_path / cooldown_module.FILE_NAME).write_text("{ not json")
        assert not store(tmp_path).read().active


class TestPerEgress:
    def test_a_ban_on_one_address_does_not_stop_another(self, tmp_path):
        """The reason a proxy is a remedy at all.

        A ban is issued to an address; moving to a different one is precisely
        how you stop being subject to it, so the record has to be keyed the same
        way the clearance is.
        """
        store(tmp_path, egress.DIRECT).arm(3600, "429")

        assert store(tmp_path, egress.DIRECT).read().active
        assert not store(tmp_path, PROXIED).read().active

    def test_arming_one_egress_leaves_the_other_intact(self, tmp_path):
        store(tmp_path, egress.DIRECT).arm(3600, "direct ban")
        store(tmp_path, PROXIED).arm(600, "proxy ban")

        assert store(tmp_path, egress.DIRECT).read().reason == "direct ban"
        assert store(tmp_path, PROXIED).read().reason == "proxy ban"


class TestArmedByTheClient:
    """That a refusal actually reaches the store, from the code path that saw it."""

    @pytest.fixture
    def client_bits(self):
        pytest.importorskip("curl_cffi")
        pytest.importorskip("ijson")
        return pytest.importorskip("smite2.tracker_client")

    def test_an_hour_long_retry_after_arms_a_standdown(self, tmp_path, client_bits):
        target = store(tmp_path)
        client = client_bits.TrackerClient(None, silent=True, cooldown=target)

        with pytest.raises(client_bits.TrackerBlocked, match="that is a block"):
            client._TrackerClient__cool_down("/matches", "3600")

        recorded = target.read()
        assert recorded.active
        assert 3500 < recorded.remaining <= 3600

    def test_a_short_retry_after_arms_nothing(self, tmp_path, client_bits):
        """A pause is not a ban; the next run must not be held off for it."""
        target = store(tmp_path)
        client = client_bits.TrackerClient(None, silent=True, cooldown=target)

        assert client._TrackerClient__cool_down("/matches", "30") == (
            client_bits.COOLDOWN
        )
        assert not target.read().active

    def test_running_out_of_patience_arms_a_standdown(self, tmp_path, client_bits):
        target = store(tmp_path)
        client = client_bits.TrackerClient(None, silent=True, cooldown=target)
        for _ in range(client_bits.MAX_RATE_LIMITS):
            client._TrackerClient__cool_down("/matches", "30")

        with pytest.raises(client_bits.TrackerBlocked, match="did not help"):
            client._TrackerClient__cool_down("/matches", "30")

        assert target.read().active

    def test_a_client_without_a_store_still_stops(self, tmp_path, client_bits):
        """The bot builds one without a cooldown; it must not crash on a ban."""
        client = client_bits.TrackerClient(None, silent=True)
        with pytest.raises(client_bits.TrackerBlocked):
            client._TrackerClient__cool_down("/matches", "3600")


class TestTheStanddownIsBounded:
    """`Retry-After` describes this request, not tomorrow.

    A header asking for a day used to arm a day, and the collector refuses to
    start inside a live stand-down — so one bad number cost several nights to
    avoid a refusal that a single request would have disproved.
    """

    @pytest.fixture
    def client_bits(self):
        pytest.importorskip("curl_cffi")
        pytest.importorskip("ijson")
        return pytest.importorskip("smite2.tracker_client")

    def test_a_day_long_retry_after_arms_four_hours(self, tmp_path, client_bits):
        target = store(tmp_path)
        client = client_bits.TrackerClient(None, silent=True, cooldown=target)

        with pytest.raises(client_bits.TrackerBlocked):
            client._TrackerClient__cool_down("/matches", "86400")

        recorded = target.read()
        assert recorded.active
        assert recorded.remaining <= client_bits.MAX_STANDDOWN_SECONDS
        assert recorded.remaining > client_bits.MAX_STANDDOWN_SECONDS - 60

    def test_what_was_asked_for_survives_in_the_reason(self, tmp_path, client_bits):
        """Or the discrepancy between asked and armed is invisible."""
        target = store(tmp_path)
        client = client_bits.TrackerClient(None, silent=True, cooldown=target)

        with pytest.raises(client_bits.TrackerBlocked):
            client._TrackerClient__cool_down("/matches", "86400")

        assert "24.0h" in target.read().reason

    def test_an_hour_is_under_the_bound_and_arms_unchanged(
        self, tmp_path, client_bits
    ):
        target = store(tmp_path)
        client = client_bits.TrackerClient(None, silent=True, cooldown=target)

        with pytest.raises(client_bits.TrackerBlocked):
            client._TrackerClient__cool_down("/matches", "3600")

        assert 3500 < target.read().remaining <= 3600
