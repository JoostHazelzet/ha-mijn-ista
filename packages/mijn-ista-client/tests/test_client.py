"""Offline client regressions, without Home Assistant fixtures or live requests."""

from __future__ import annotations

import asyncio
import runpy
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from importlib.metadata import distribution
from itertools import pairwise
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from mijn_ista_client import (
    IstaCookieJar,
    MijnIstaAPI,
    MijnIstaAuthError,
    MijnIstaConnectionError,
    MijnIstaRateLimited,
    MijnIstaShared,
    ResilientMijnIstaAPI,
    Throttle,
    create_session,
    missing_session_cookie_chunks,
    open_client,
    throttle_middleware,
)
from mijn_ista_client import client as client_mod
from mijn_ista_client.const import MIN_LOGIN_GAP_S, MIN_REQUEST_GAP_S
from yarl import URL


def shared(login_gap: float = 0) -> MijnIstaShared:
    return MijnIstaShared(
        login_gate=Throttle("login", login_gap),
        request_throttle=Throttle("data", 0),
    )


def client(state: MijnIstaShared) -> ResilientMijnIstaAPI:
    return ResilientMijnIstaAPI(MagicMock(), "User@Example.com", "pw", shared=state)


def test_import_never_loads_home_assistant():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
class NoExternalClients(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('homeassistant', 'mijn_ista_api'):
            raise AssertionError(f'External dependency must not be imported: {fullname}')
sys.meta_path.insert(0, NoExternalClients())
import mijn_ista_client
assert not any(name.startswith('homeassistant') for name in sys.modules)
assert 'mijn_ista_api' not in sys.modules
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_protocol_is_owned_by_single_distribution():
    assert MijnIstaAPI.__module__ == "mijn_ista_client.api"
    requirements = distribution("mijn-ista-client").requires or []
    assert not any(req.startswith("mijn-ista-api") for req in requirements)


async def test_console_example_fetches_raw_payload(monkeypatch, capsys):
    api = MagicMock(
        authenticate=AsyncMock(),
        get_user_values=AsyncMock(return_value={"cus": []}),
    )

    @asynccontextmanager
    async def fake_open(username, password):
        assert (username, password) == ("example@example.com", "test-password")
        yield api

    monkeypatch.setenv("ISTA_USERNAME", "example@example.com")
    monkeypatch.setenv("ISTA_PASSWORD", "test-password")
    monkeypatch.setattr("mijn_ista_client.open_client", fake_open)
    example = Path(__file__).parents[1] / "examples" / "read_consumption.py"
    namespace = runpy.run_path(str(example))
    await namespace["main"]()
    api.authenticate.assert_awaited_once()
    api.get_user_values.assert_awaited_once()
    assert "'cus': []" in capsys.readouterr().out


async def test_cookie_jar_sends_realistic_chunk_sizes():
    cookies = SimpleCookie()
    cookies[".AspNetCore.Cookies"] = "chunks-3"
    for i, size in enumerate((3987, 3987, 821), start=1):
        cookies[f".AspNetCore.CookiesC{i}"] = "x" * size
    for morsel in cookies.values():
        morsel["path"] = "/"
    jar = IstaCookieJar()
    jar.update_cookies(cookies, URL("https://mijn.ista.nl/Account/Login"))
    sent = jar.filter_cookies(URL("https://mijn.ista.nl/Home"))
    assert set(sent) == set(cookies)
    assert missing_session_cookie_chunks(jar) == []
    stock = aiohttp.CookieJar()
    stock.update_cookies(cookies, URL("https://mijn.ista.nl/Account/Login"))
    if getattr(stock, "_limits_enabled", False):
        assert missing_session_cookie_chunks(stock)


async def test_no_session_cookie():
    assert missing_session_cookie_chunks(IstaCookieJar()) == []


async def test_default_limits():
    state = MijnIstaShared()
    assert MIN_LOGIN_GAP_S == state.login_gate.min_gap == 120
    assert MIN_REQUEST_GAP_S == state.request_throttle.min_gap == 5


async def test_throttle_serializes_concurrent_starts():
    throttle = Throttle("test", 0.02)
    starts = []

    async def acquire():
        await throttle.acquire()
        starts.append(time.monotonic())

    await asyncio.gather(*(acquire() for _ in range(3)))
    assert all(b - a >= 0.02 for a, b in pairwise(starts))


async def test_throttle_rejection_does_not_consume_attempt():
    throttle = Throttle("test", 120)
    await throttle.acquire(max_wait=0)
    with pytest.raises(MijnIstaRateLimited) as err:
        await throttle.acquire(max_wait=30)
    assert 119 < err.value.retry_after <= 120
    assert isinstance(err.value, MijnIstaConnectionError)


@pytest.mark.parametrize(
    ("url", "count"),
    [
        ("https://mijn.ista.nl/api/Values/UserValues", 1),
        ("https://mijn.ista.nl/api/Consumption/MonthValues", 1),
        ("https://mijn.ista.nl/Account/Login", 0),
        ("https://example.com/api/test", 0),
    ],
)
async def test_middleware_paths(url, count):
    throttle = MagicMock(acquire=AsyncMock())
    handler = AsyncMock()
    middleware = throttle_middleware(throttle)
    assert await middleware(MagicMock(url=URL(url)), handler) is handler.return_value
    assert throttle.acquire.await_count == count


async def test_session_factory_wires_cookie_jar_and_middleware():
    state = shared()
    async with create_session(state) as session:
        assert isinstance(session.cookie_jar, IstaCookieJar)
        request = MagicMock(url=URL("https://mijn.ista.nl/api/Values/UserValues"))
        with patch.object(state.request_throttle, "acquire", AsyncMock()) as acquire:
            await session._middlewares[0](request, AsyncMock())
        acquire.assert_awaited_once()
    assert session.closed


@pytest.mark.parametrize("fail", [False, True])
async def test_context_owns_session_without_implicit_login(fail):
    with patch.object(MijnIstaAPI, "authenticate", AsyncMock()) as login:
        try:
            async with open_client("u", "pw", lang="en-GB") as api:
                assert api.matches("u", "pw", "en-GB")
                assert not api.session.closed
                if fail:
                    raise RuntimeError("example failure")
        except RuntimeError as exc:
            assert fail and str(exc) == "example failure"
        login.assert_not_awaited()
    assert api.session.closed


async def test_context_closes_on_cancellation():
    async def run():
        async with open_client("u", "pw") as api:
            sessions.append(api.session)
            entered.set()
            await asyncio.Event().wait()

    sessions = []
    entered = asyncio.Event()
    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sessions[0].closed


async def test_clients_share_limits_but_not_cookie_jars():
    state = shared(120)
    async with (
        open_client("u", "pw", shared=state) as first,
        open_client("v", "pw", shared=state) as second,
    ):
        assert first.session.cookie_jar is not second.session.cookie_jar
        with patch.object(MijnIstaAPI, "authenticate", AsyncMock()) as login:
            await first.authenticate()
            second.login_max_wait = 0
            with pytest.raises(MijnIstaRateLimited):
                await second.authenticate()
            assert login.await_count == 1


async def test_success_clears_cookie_jar_and_rejection_streak():
    state = shared()
    state.invalid_streak["user@example.com"] = 1
    api = client(state)
    with patch.object(MijnIstaAPI, "authenticate", AsyncMock()) as base:
        await api.authenticate()
    base.assert_awaited_once()
    api.session.cookie_jar.clear.assert_called_once()
    assert not state.invalid_streak


@pytest.mark.parametrize(
    "failure",
    [
        MijnIstaAuthError("Session was not accepted after login"),
        MijnIstaConnectionError("HTTP 403"),
    ],
)
async def test_transient_login_failure_retries(failure):
    api = client(shared())
    with patch.object(
        MijnIstaAPI, "authenticate", AsyncMock(side_effect=[failure, None])
    ) as base:
        await api.authenticate()
    assert base.await_count == api.session.cookie_jar.clear.call_count == 2


async def test_invalid_credentials_require_two_confirmations():
    state = shared()
    api = client(state)
    with (
        patch.object(
            MijnIstaAPI,
            "authenticate",
            AsyncMock(side_effect=MijnIstaAuthError("Invalid credentials")),
        ) as base,
        pytest.raises(MijnIstaAuthError),
    ):
        await api.authenticate()
    assert base.await_count == 2
    assert state.invalid_streak["user@example.com"] == 2


async def test_invalid_streak_survives_client_changes():
    state = shared()
    first, second = client(state), client(state)
    first.login_attempts = second.login_attempts = 1
    with patch.object(
        MijnIstaAPI,
        "authenticate",
        AsyncMock(side_effect=MijnIstaAuthError("Invalid credentials")),
    ):
        with pytest.raises(MijnIstaConnectionError):
            await first.authenticate()
        with pytest.raises(MijnIstaAuthError):
            await second.authenticate()


async def test_other_error_resets_invalid_streak():
    state = shared()
    api = client(state)
    failures = [
        MijnIstaAuthError("Invalid credentials"),
        MijnIstaConnectionError("down"),
        MijnIstaAuthError("Invalid credentials"),
    ]
    with (
        patch.object(MijnIstaAPI, "authenticate", AsyncMock(side_effect=failures)),
        pytest.raises(MijnIstaConnectionError) as err,
    ):
        await api.authenticate()
    assert not isinstance(err.value, MijnIstaAuthError)
    assert "3 attempt(s)" in str(err.value)
    assert state.invalid_streak["user@example.com"] == 1


@pytest.fixture
def fast_poll():
    with (
        patch.object(client_mod, "MONTH_POLL_INTERVAL_S", 0.001),
        patch.object(client_mod, "MONTH_BUDGET_QUICK_S", 0.01),
        patch.object(client_mod, "MONTH_BUDGET_FULL_S", 0.01),
    ):
        yield


async def test_full_history_polls_until_complete(fast_poll):
    api = client(shared())
    api._poll_shard = AsyncMock(
        side_effect=[None, {"sh": 2, "hs": 1}, {"sh": 2, "hs": 2, "mc": [1, 2]}]
    )
    assert await api.get_month_values("cuid") == {"sh": 2, "hs": 2, "mc": [1, 2]}
    assert api._poll_shard.await_count == 3
    api._poll_shard.assert_awaited_with(
        "/api/Consumption/MonthValues", {"Cuid": "cuid"}
    )


async def test_quick_returns_partial_month_data(fast_poll):
    api = client(shared())
    api._poll_shard = AsyncMock(return_value={"sh": 4, "hs": 1, "mc": [1]})
    assert await api.get_month_values("cuid", quick=True) == {
        "sh": 4,
        "hs": 1,
        "mc": [1],
    }
    api._poll_shard.assert_awaited_once()


async def test_budget_returns_partial_data_with_warning(fast_poll, caplog):
    api = client(shared())
    api._poll_shard = AsyncMock(return_value={"sh": 4, "hs": 1, "mc": [1]})
    assert (await api.get_month_values("cuid"))["hs"] == 1
    assert "time budget exceeded" in caplog.text


async def test_empty_shards_remain_tolerated(fast_poll):
    api = client(shared())
    api._poll_shard = AsyncMock(return_value=None)
    assert await api.get_month_values("cuid") == {}
