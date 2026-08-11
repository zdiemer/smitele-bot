"""The Steam presence fallback: fast, coarse, and never fatal.

It answers "is this player running Smite 2" so /livematch can tell tracker.gg
lagging apart from nobody home. Every reason it can't answer collapses to None,
because they all want the same fallback — answer from tracker.gg alone.
"""

from __future__ import annotations

import pytest

steam = pytest.importorskip("smite2.steam")


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class _Session:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, *a, **k):
        return self._resp


def _patch(monkeypatch, resp, key="k"):
    monkeypatch.setenv("SMITELE_STEAM_API_KEY", key)
    monkeypatch.setattr(steam.aiohttp, "ClientSession", lambda *a, **k: _Session(resp))


@pytest.mark.asyncio
async def test_in_smite2_is_true(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"response": {"players": [
        {"gameid": steam.SMITE2_STEAM_APPID}]}}))
    assert await steam.running_smite2("76561197993375857") is True


@pytest.mark.asyncio
async def test_in_a_different_game_is_false(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"response": {"players": [{"gameid": "440"}]}}))
    assert await steam.running_smite2("76561197993375857") is False


@pytest.mark.asyncio
async def test_not_in_a_game_is_false(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"response": {"players": [{}]}}))
    assert await steam.running_smite2("76561197993375857") is False


@pytest.mark.asyncio
async def test_no_key_is_none(monkeypatch):
    monkeypatch.delenv("SMITELE_STEAM_API_KEY", raising=False)
    assert await steam.running_smite2("76561197993375857") is None


@pytest.mark.asyncio
async def test_a_non_steam_handle_is_none(monkeypatch):
    monkeypatch.setenv("SMITELE_STEAM_API_KEY", "k")
    assert await steam.running_smite2("SomeEpicName") is None


@pytest.mark.asyncio
async def test_a_private_profile_is_none(monkeypatch):
    _patch(monkeypatch, _Resp(200, {"response": {"players": []}}))
    assert await steam.running_smite2("76561197993375857") is None


@pytest.mark.asyncio
async def test_an_http_error_is_none(monkeypatch):
    _patch(monkeypatch, _Resp(500, {}))
    assert await steam.running_smite2("76561197993375857") is None
