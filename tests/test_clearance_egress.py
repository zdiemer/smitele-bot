"""Clearance kept per outbound address, and the naming that makes it possible.

A Cloudflare cookie is bound to the address that solved the challenge. Before
this, one file held one cookie for every process sharing it — fine while
everything left from the same place, and a 403 storm the moment anything did
not: each refusal discards the cookie and mints a replacement, and twelve of
those in a day arms a four-hour breaker.

So the tests here are mostly about isolation between buckets, plus the one-way
migration that decides where an existing cookie lands.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "HirezAPI"),
)

clearance = pytest.importorskip("smite2.clearance")
egress = pytest.importorskip("smite2.egress")

import asyncio  # noqa: E402
import time  # noqa: E402

PROXIED = "http://bot:hunter2@gate.example.net:8080"
STRIPPED = "http://gate.example.net:8080"


@pytest.fixture(autouse=True)
def no_ambient_proxy(monkeypatch):
    """The host's own environment must not decide what a test measures."""
    monkeypatch.delenv(egress.ENV_VAR, raising=False)
    monkeypatch.setattr(egress, "proxy_url", lambda: None)


def store_at(tmp_path, egress_name):
    return clearance.ClearanceStore(str(tmp_path / "clearance.json"), egress=egress_name)


def cookie(value="abc", **overrides):
    fields = dict(
        value=value,
        user_agent="Mozilla/5.0 Firefox/130.0",
        issued_at=time.time(),
    )
    fields.update(overrides)
    return clearance.Clearance(**fields)


class TestIdentity:
    def test_the_identity_strips_credentials(self):
        named = egress.identity(PROXIED)
        assert named == STRIPPED
        assert "hunter2" not in named
        assert "bot" not in named

    def test_no_proxy_configured_is_direct(self):
        assert egress.identity(None) == egress.DIRECT
        assert egress.identity("") == egress.DIRECT

    def test_the_proxy_dict_puts_credentials_in_their_own_fields(self):
        """Playwright rejects user:pass inline in `server`."""
        assert egress.proxy_dict(PROXIED) == {
            "server": STRIPPED,
            "username": "bot",
            "password": "hunter2",
        }

    def test_a_proxy_without_credentials_has_no_credential_fields(self):
        assert egress.proxy_dict(STRIPPED) == {"server": STRIPPED}

    def test_no_proxy_has_no_dict(self):
        assert egress.proxy_dict(None) is None

    def test_an_unparseable_proxy_never_masquerades_as_direct(self):
        """A typo must not silently share the unproxied bucket's cookie."""
        assert egress.identity("not a url") != egress.DIRECT


class TestMigration:
    def test_a_v1_file_migrates_into_the_direct_bucket(self, tmp_path):
        """v1 predates any proxy, so its cookie was solved from the host."""
        path = tmp_path / "clearance.json"
        path.write_text(
            json.dumps(
                {
                    "clearance": {
                        "value": "legacy",
                        "user_agent": "Mozilla/5.0 Firefox/130.0",
                        "issued_at": time.time(),
                        "last_ok": 0.0,
                    },
                    "mints": [time.time()],
                    "blocked_until": 0.0,
                }
            )
        )

        direct = store_at(tmp_path, egress.DIRECT).load()
        assert direct.clearance is not None
        assert direct.clearance.value == "legacy"
        assert len(direct.mints) == 1

    def test_a_proxied_process_does_not_see_the_legacy_cookie(self, tmp_path):
        """The point of the migration: withhold it from a different address."""
        path = tmp_path / "clearance.json"
        path.write_text(
            json.dumps(
                {
                    "clearance": {
                        "value": "legacy",
                        "user_agent": "Mozilla/5.0 Firefox/130.0",
                        "issued_at": time.time(),
                    },
                    "mints": [],
                    "blocked_until": 0.0,
                }
            )
        )

        assert store_at(tmp_path, STRIPPED).load().clearance is None

    def test_an_unrecognised_payload_degrades_to_no_cookie(self, tmp_path):
        """One extra solve, not an exception out of every lookup."""
        path = tmp_path / "clearance.json"
        path.write_text(json.dumps({"clearance": {"nonsense": True}, "mints": []}))
        assert store_at(tmp_path, egress.DIRECT).load().clearance is None

    def test_an_unreadable_file_is_an_empty_state(self, tmp_path):
        (tmp_path / "clearance.json").write_text("{ not json")
        state = store_at(tmp_path, egress.DIRECT).load()
        assert state.clearance is None and state.mints == []


class TestIsolation:
    def test_a_proxied_process_does_not_see_the_direct_cookie(self, tmp_path):
        direct = store_at(tmp_path, egress.DIRECT)
        direct.save(clearance._State(clearance=cookie("direct-cookie")))

        assert store_at(tmp_path, STRIPPED).load().clearance is None
        assert direct.load().clearance.value == "direct-cookie"

    def test_saving_one_egress_leaves_the_other_intact(self, tmp_path):
        """Read-modify-write: the bot must not clobber the collector."""
        direct = store_at(tmp_path, egress.DIRECT)
        proxied = store_at(tmp_path, STRIPPED)

        direct.save(clearance._State(clearance=cookie("direct-cookie")))
        proxied.save(clearance._State(clearance=cookie("proxied-cookie")))

        assert direct.load().clearance.value == "direct-cookie"
        assert proxied.load().clearance.value == "proxied-cookie"

    def test_the_mint_budget_is_per_egress(self, tmp_path):
        """Twelve solves from one address must not block a different one."""
        now = time.time()
        spent = clearance._State(mints=[now] * clearance.MAX_MINTS_PER_DAY)
        store_at(tmp_path, egress.DIRECT).save(spent)

        assert len(store_at(tmp_path, egress.DIRECT).load().mints) == (
            clearance.MAX_MINTS_PER_DAY
        )
        assert store_at(tmp_path, STRIPPED).load().mints == []

    def test_the_breaker_is_per_egress(self, tmp_path):
        armed = clearance._State(blocked_until=time.time() + 3600)
        store_at(tmp_path, egress.DIRECT).save(armed)

        assert store_at(tmp_path, egress.DIRECT).load().blocked_until > time.time()
        assert store_at(tmp_path, STRIPPED).load().blocked_until == 0.0

    def test_a_stale_mint_ages_out(self, tmp_path):
        old = clearance._State(mints=[time.time() - clearance.DAY_SECONDS - 60])
        store = store_at(tmp_path, egress.DIRECT)
        store.save(old)
        assert store.load().mints == []


class TestManager:
    def test_invalidate_only_touches_its_own_bucket(self, tmp_path, monkeypatch):
        direct = store_at(tmp_path, egress.DIRECT)
        proxied = store_at(tmp_path, STRIPPED)
        shared = cookie("same-value")
        direct.save(clearance._State(clearance=shared))
        proxied.save(clearance._State(clearance=shared))

        clearance.ClearanceManager(direct, silent=True).invalidate(shared)

        assert direct.load().clearance is None
        assert proxied.load().clearance is not None, "the other bucket was cleared"

    def test_the_cookie_records_the_egress_it_was_minted_for(
        self, tmp_path, monkeypatch
    ):
        """What makes a mismatch diagnosable rather than a mystery."""
        minted = cookie("fresh", egress=STRIPPED, observed_ip="203.0.113.7")

        async def fake_mint(headless=True, timeout=120, proxy_url=None):
            assert proxy_url == PROXIED, "the mint must go through the proxy"
            return minted

        monkeypatch.setattr(clearance, "mint", fake_mint)
        store = store_at(tmp_path, STRIPPED)
        manager = clearance.ClearanceManager(store, silent=True, proxy_url=PROXIED)

        got = asyncio.run(manager.get())
        assert got.value == "fresh"
        assert store.load().clearance.observed_ip == "203.0.113.7"
        assert store.load().clearance.egress == STRIPPED

    def test_a_cached_cookie_is_reused_without_minting(self, tmp_path, monkeypatch):
        async def explode(**kwargs):
            raise AssertionError("should not have minted")

        monkeypatch.setattr(clearance, "mint", explode)
        store = store_at(tmp_path, egress.DIRECT)
        store.save(clearance._State(clearance=cookie("cached")))

        manager = clearance.ClearanceManager(store, silent=True)
        assert asyncio.run(manager.get()).value == "cached"
