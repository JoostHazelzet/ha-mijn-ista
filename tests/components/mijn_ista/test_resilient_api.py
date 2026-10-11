"""Tests for the resilient mijn.ista.nl client (api.py)."""

from __future__ import annotations

import time
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from mijn_ista_api import MijnIstaAPI, MijnIstaAuthError, MijnIstaConnectionError
from yarl import URL

from custom_components.mijn_ista import api as api_mod
from custom_components.mijn_ista.api import (
    IstaCookieJar,
    MijnIstaRateLimited,
    MijnIstaShared,
    ResilientMijnIstaAPI,
    Throttle,
    missing_session_cookie_chunks,
    stash_handoff,
    take_handoff,
    throttle_middleware,
)

_ISTA = URL("https://mijn.ista.nl/Account/Login")
_HOME = URL("https://mijn.ista.nl/Home")
_CHUNK_SIZES = (3987, 3987, 821)  # realistic .AspNetCore.Cookies chunk sizes


def _store_session_cookie(jar: aiohttp.CookieJar) -> None:
    cookies = SimpleCookie()
    cookies[".AspNetCore.Cookies"] = f"chunks-{len(_CHUNK_SIZES)}"
    for i, size in enumerate(_CHUNK_SIZES, start=1):
        cookies[f".AspNetCore.CookiesC{i}"] = "x" * size
    for morsel in cookies.values():
        morsel["path"] = "/"
    jar.update_cookies(cookies, _ISTA)


def _shared(login_gap: float = 0.0) -> MijnIstaShared:
    return MijnIstaShared(
        login_gate=Throttle("gate", login_gap),
        request_throttle=Throttle("requests", 0.0),
    )


def _api(shared: MijnIstaShared, session: MagicMock | None = None) -> ResilientMijnIstaAPI:
    if session is None:
        session = MagicMock()
    return ResilientMijnIstaAPI(session, "User@Example.com", "pw", shared=shared)


# ── cookie jar ──────────────────────────────────────────────────────────────


class TestCookieJar:
    async def test_ista_jar_sends_all_chunks(self):
        jar = IstaCookieJar()
        _store_session_cookie(jar)
        sent = jar.filter_cookies(_HOME)
        assert {".AspNetCore.Cookies", ".AspNetCore.CookiesC1"} <= set(sent)
        assert all(f".AspNetCore.CookiesC{i}" in sent for i in (1, 2, 3))
        assert missing_session_cookie_chunks(jar) == []

    async def test_stock_jar_drops_a_chunk_when_header_is_capped(self):
        jar = aiohttp.CookieJar()
        if not getattr(jar, "_limits_enabled", False):
            pytest.skip("this aiohttp version does not cap the Cookie header")
        _store_session_cookie(jar)
        assert missing_session_cookie_chunks(jar) != []

    async def test_missing_chunks_without_session_cookie(self):
        assert missing_session_cookie_chunks(IstaCookieJar()) == []


# ── throttles ───────────────────────────────────────────────────────────────


class TestThrottle:
    async def test_first_acquire_is_immediate(self):
        throttle = Throttle("t", 10)
        assert throttle.remaining() == 0
        await throttle.acquire(max_wait=0)
        assert 9 < throttle.remaining() <= 10

    async def test_waits_for_the_gap(self):
        throttle = Throttle("t", 0.05)
        await throttle.acquire()
        start = time.monotonic()
        await throttle.acquire()
        assert time.monotonic() - start >= 0.04

    async def test_max_wait_exceeded_raises(self):
        throttle = Throttle("t", 100)
        await throttle.acquire()
        with pytest.raises(MijnIstaRateLimited) as err:
            await throttle.acquire(max_wait=30)
        assert 99 < err.value.retry_after <= 100
        assert isinstance(err.value, MijnIstaConnectionError)

    async def test_middleware_throttles_only_api_paths(self):
        throttle = MagicMock()
        throttle.acquire = AsyncMock()
        middleware = throttle_middleware(throttle)
        handler = AsyncMock(return_value="response")

        for url, throttled in (
            ("https://mijn.ista.nl/api/Values/UserValues", True),
            ("https://mijn.ista.nl/Account/Login", False),
            ("https://example.com/api/x", False),
        ):
            throttle.acquire.reset_mock()
            request = MagicMock(url=URL(url))
            assert await middleware(request, handler) == "response"
            assert throttle.acquire.called is throttled


# ── authenticate ────────────────────────────────────────────────────────────


class TestAuthenticate:
    async def test_success_first_attempt(self):
        shared = _shared()
        api = _api(shared)
        with patch.object(MijnIstaAPI, "authenticate", AsyncMock()) as base:
            await api.authenticate()
        assert base.await_count == 1
        api.session.cookie_jar.clear.assert_called_once()

    async def test_transient_failure_is_retried(self):
        shared = _shared()
        api = _api(shared)
        side_effect = [MijnIstaAuthError("Session was not accepted after login"), None]
        with patch.object(MijnIstaAPI, "authenticate", AsyncMock(side_effect=side_effect)):
            await api.authenticate()
        assert api.session.cookie_jar.clear.call_count == 2

    async def test_invalid_credentials_need_two_confirmations(self):
        shared = _shared()
        api = _api(shared)
        base = AsyncMock(side_effect=MijnIstaAuthError("Invalid credentials"))
        with patch.object(MijnIstaAPI, "authenticate", base), pytest.raises(
            MijnIstaAuthError
        ):
            await api.authenticate()
        assert base.await_count == 2
        assert shared.invalid_streak["user@example.com"] == 2

    async def test_invalid_streak_survives_across_calls(self):
        shared = _shared()
        api = _api(shared)
        api.login_attempts = 1
        base = AsyncMock(side_effect=MijnIstaAuthError("Invalid credentials"))
        with patch.object(MijnIstaAPI, "authenticate", base):
            with pytest.raises(MijnIstaConnectionError):
                await api.authenticate()
            with pytest.raises(MijnIstaAuthError):
                await api.authenticate()

    async def test_other_error_resets_invalid_streak(self):
        shared = _shared()
        api = _api(shared)
        side_effect = [
            MijnIstaAuthError("Invalid credentials"),
            MijnIstaConnectionError("boom"),
            MijnIstaAuthError("Invalid credentials"),
        ]
        with (
            patch.object(MijnIstaAPI, "authenticate", AsyncMock(side_effect=side_effect)),
            pytest.raises(MijnIstaConnectionError) as err,
        ):
            await api.authenticate()
        assert not isinstance(err.value, MijnIstaAuthError)
        assert "3 attempt(s)" in str(err.value)
        assert shared.invalid_streak["user@example.com"] == 1

    async def test_success_resets_invalid_streak(self):
        shared = _shared()
        shared.invalid_streak["user@example.com"] = 1
        api = _api(shared)
        with patch.object(MijnIstaAPI, "authenticate", AsyncMock()):
            await api.authenticate()
        assert "user@example.com" not in shared.invalid_streak

    async def test_all_attempts_fail_raises_connection_error(self):
        shared = _shared()
        api = _api(shared)
        base = AsyncMock(side_effect=MijnIstaConnectionError("down"))
        with patch.object(MijnIstaAPI, "authenticate", base), pytest.raises(
            MijnIstaConnectionError
        ):
            await api.authenticate()
        assert base.await_count == 3

    async def test_closed_gate_without_waiting_raises_rate_limited(self):
        shared = _shared(login_gap=120)
        await shared.login_gate.acquire()
        api = _api(shared)
        api.login_max_wait = 0
        base = AsyncMock()
        with patch.object(MijnIstaAPI, "authenticate", base), pytest.raises(
            MijnIstaRateLimited
        ):
            await api.authenticate()
        base.assert_not_awaited()

    async def test_matches(self):
        api = _api(_shared())
        assert api.matches("User@Example.com", "pw", "nl-NL")
        assert not api.matches("User@Example.com", "other", "nl-NL")
        assert not api.matches("User@Example.com", "pw", "en-GB")


# ── month polling ───────────────────────────────────────────────────────────


@pytest.fixture
def fast_month_polling():
    with (
        patch.object(api_mod, "MONTH_POLL_INTERVAL_S", 0.001),
        patch.object(api_mod, "MONTH_BUDGET_QUICK_S", 0.05),
        patch.object(api_mod, "MONTH_BUDGET_FULL_S", 0.05),
    ):
        yield


class TestMonthValues:
    async def test_polls_until_complete(self, fast_month_polling):
        api = _api(_shared())
        api._poll_shard = AsyncMock(
            side_effect=[None, {"sh": 2, "hs": 1, "mc": [1]}, {"sh": 2, "hs": 2, "mc": [1, 2]}]
        )
        data = await api.get_month_values("cuid")
        assert data["mc"] == [1, 2]
        assert api._poll_shard.await_count == 3

    async def test_quick_returns_first_partial_data(self, fast_month_polling):
        api = _api(_shared())
        api._poll_shard = AsyncMock(return_value={"sh": 4, "hs": 1, "mc": [1]})
        data = await api.get_month_values("cuid", quick=True)
        assert data["hs"] == 1
        assert api._poll_shard.await_count == 1

    async def test_budget_exceeded_returns_partial_data(self, fast_month_polling, caplog):
        api = _api(_shared())
        api._poll_shard = AsyncMock(return_value={"sh": 4, "hs": 1, "mc": [1]})
        data = await api.get_month_values("cuid")
        assert data["hs"] == 1
        assert api._poll_shard.await_count >= 1
        assert "time budget exceeded" in caplog.text

    async def test_no_data_returns_empty_dict(self, fast_month_polling):
        api = _api(_shared())
        api._poll_shard = AsyncMock(return_value=None)
        assert await api.get_month_values("cuid") == {}


# ── handoff ─────────────────────────────────────────────────────────────────


class TestHandoff:
    def test_take_returns_stashed_client_once(self):
        shared = _shared()
        api = _api(shared)
        stash_handoff(shared, api, "User@Example.com")
        assert take_handoff(shared, "user@example.com") is api
        assert take_handoff(shared, "user@example.com") is None

    def test_restash_detaches_previous_session(self):
        shared = _shared()
        first, second = _api(shared), _api(shared)
        stash_handoff(shared, first, "u")
        stash_handoff(shared, second, "u")
        first.session.detach.assert_called_once()
        assert take_handoff(shared, "u") is second

    def test_stale_client_is_discarded(self):
        shared = _shared()
        api = _api(shared)
        shared.handoff["u"] = (api, time.monotonic() - 3600)
        assert take_handoff(shared, "u") is None
        api.session.detach.assert_called_once()


def test_rate_limited_message_is_readable():
    assert "42" in str(MijnIstaRateLimited(42.4))
