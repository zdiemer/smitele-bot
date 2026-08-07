"""Two ways Hi-Rez fails a burst, both of which used to be fatal.

Found by running the web snapshot's `--players` mode, which asks for fourteen
players back to back at about five batched calls each. Roughly a third of them
failed, differently each run:

  * `ContentTypeError` — an HTML error page instead of JSON. It was raised out
    of `__make_request_base`, past a retry loop that never saw it, so a
    transient page cost the whole call.
  * `TypeError: 'NoneType' object is not subscriptable` — a null body. The
    retry loop already had `if res is None: raise ConnectionError` at the
    bottom, so None was clearly expected; `__is_expired` just subscripted it
    first and crashed before that could happen.

Neither is specific to the snapshot. The bot hits the same client, one command
at a time, which is why this stayed hidden.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "HirezAPI"))

import aiohttp  # noqa: E402

hirez = pytest.importorskip("HirezAPI")


@pytest.fixture
def client(monkeypatch):
    """A `Smite` that never opens a socket and never sleeps between retries."""
    api = hirez.Smite("auth-key", "dev-id", silent=True)
    monkeypatch.setattr(api, "_Base__session_id", "session", raising=False)
    monkeypatch.setattr(api, "_Base__should_keep_alive", False, raising=False)
    monkeypatch.setattr(api, "_Base__save_session", False, raising=False)
    monkeypatch.setattr(hirez.Smite, "RETRY_DELAY_SECONDS", 0)
    return api


def respond_with(monkeypatch, api, answers):
    """Queue up per-attempt outcomes; an exception class is raised, else returned."""
    remaining = list(answers)
    calls = {"count": 0}

    async def fake(*args, **kwargs):
        calls["count"] += 1
        answer = remaining.pop(0) if remaining else remaining
        if isinstance(answer, type) and issubclass(answer, Exception):
            raise answer(None, None)
        return answer

    monkeypatch.setattr(api, "_Base__make_request_base", fake, raising=False)
    return calls


class TestNullBody:
    def test_a_null_body_is_retried_not_a_crash(self, client, monkeypatch):
        calls = respond_with(
            monkeypatch, client, [None, [{"ret_msg": None, "Wins": 3}]]
        )

        result = client._make_request("getqueuestats")
        import asyncio

        assert asyncio.run(result) == [{"ret_msg": None, "Wins": 3}]
        assert calls["count"] == 2

    def test_a_persistent_null_body_is_a_connection_error(self, client, monkeypatch):
        import asyncio

        respond_with(monkeypatch, client, [None, None, None])

        # Not a TypeError from deep inside the client. The caller can act on
        # this one, and the snapshot's per-player guard records it as such.
        with pytest.raises(ConnectionError):
            asyncio.run(client._make_request("getqueuestats"))

    def test_is_expired_tolerates_a_null_body(self, client):
        assert client._Base__is_expired(None) is False


class TestHtmlErrorPage:
    def test_a_content_type_error_is_retried(self, client, monkeypatch):
        import asyncio

        calls = respond_with(
            monkeypatch, client, [aiohttp.ContentTypeError, {"ret_msg": None}]
        )

        assert asyncio.run(client._make_request("getdataused")) == {"ret_msg": None}
        assert calls["count"] == 2

    def test_a_persistent_content_type_error_still_raises(self, client, monkeypatch):
        import asyncio

        # Giving up is right — this is what a genuinely broken route looks
        # like, and swallowing it would report success with no data.
        respond_with(
            monkeypatch,
            client,
            [aiohttp.ContentTypeError] * hirez.Smite.MAX_RETRIES,
        )

        with pytest.raises(aiohttp.ContentTypeError):
            asyncio.run(client._make_request("getdataused"))


class TestExpiredSessionStillWorks:
    def test_a_list_carrying_an_invalid_session_is_detected(self, client):
        assert client._Base__is_expired([{"ret_msg": "Invalid session id."}]) is True

    def test_a_list_with_a_null_row_does_not_crash(self, client):
        assert client._Base__is_expired([None, {"ret_msg": None}]) is False

    def test_a_dict_without_ret_msg_does_not_crash(self, client):
        # `.get` rather than `[]`: a response shaped unexpectedly should not
        # take down the session check.
        assert client._Base__is_expired({"session_id": "abc"}) is False
