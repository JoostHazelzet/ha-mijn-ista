"""Offline tests for the maintained API core, without Home Assistant fixtures.

mijn.ista.nl authenticates through an OpenID Connect (Keycloak) login at
login.ista.com rather than a token issued by the app itself. `authenticate()`
therefore drives a short, scripted sequence of GET/POST calls (challenge
redirect -> identity-provider login form -> credential POST -> session
callback -> CSRF token fetch). These tests fake that sequence with queued
responses rather than mocking a single endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from mijn_ista_api import MijnIstaAPI, MijnIstaAuthError, MijnIstaConnectionError

AUTHORIZE_URL = (
    "https://login.ista.com/realms/portal_nl/protocol/openid-connect/auth?state=abc"
)
FORM_ACTION = "https://login.ista.com/realms/portal_nl/login-actions/authenticate?session_code=xyz"
LOGIN_FORM_HTML = f'<form id="kc-form-login" action="{FORM_ACTION}">...</form>'
INVALID_CREDS_HTML = (
    LOGIN_FORM_HTML + "<span>Ongeldige gebruikersnaam of wachtwoord.</span>"
)
CALLBACK_ACTION = "https://mijn.ista.nl/signin-oidc"
FORM_POST_CALLBACK_HTML = (
    f'<FORM METHOD="POST" ACTION="{CALLBACK_ACTION}">'
    '<INPUT TYPE="HIDDEN" NAME="code" VALUE="auth-code" />'
    '<INPUT TYPE="HIDDEN" NAME="state" VALUE="abc" />'
    "</FORM>"
)
HOME_PAGE_HTML = '<meta name="csrf-token" content="csrf-tok-123" />'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """A response usable as an async context manager, like aiohttp's."""

    def __init__(self, status=200, text="", json_data=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self._text = text
        self._json = json_data if json_data is not None else {}

    async def text(self):
        return self._text

    async def json(self):
        return self._json

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(MagicMock(), (), status=self.status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RaisingContextManager:
    """Raises on __aenter__, like aiohttp does for connection-level failures."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Returns queued responses in call order, separately for GET and POST."""

    def __init__(self, get_responses=None, post_responses=None):
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])
        self.get_calls: list[tuple] = []
        self.post_calls: list[tuple] = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._get_responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._post_responses.pop(0)


def _login_sequence(*, credentials_ok: bool = True) -> tuple[list, list]:
    """GET/POST response queues for one successful (or failed) authenticate()."""
    get_responses = [
        _FakeResponse(200, text=""),  # GET /
        _FakeResponse(302, headers={"Location": AUTHORIZE_URL}),  # GET /home/login
        _FakeResponse(200, text=LOGIN_FORM_HTML),  # GET authorize_url
    ]
    if not credentials_ok:
        post_responses = [_FakeResponse(200, text=INVALID_CREDS_HTML)]
        return get_responses, post_responses

    get_responses.append(_FakeResponse(200, text=HOME_PAGE_HTML))  # GET /Home
    post_responses = [
        _FakeResponse(200, text=FORM_POST_CALLBACK_HTML),  # POST credentials
        _FakeResponse(302),  # POST signin-oidc callback
    ]
    return get_responses, post_responses


async def _authenticated_api(lang: str = "nl-NL") -> MijnIstaAPI:
    get_r, post_r = _login_sequence()
    session = _FakeSession(get_r, post_r)
    api = MijnIstaAPI(session, "user@example.com", "secret", lang=lang)
    await api.authenticate()
    return api


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthenticate:
    async def test_success_sets_csrf_token(self):
        api = await _authenticated_api()
        assert api._csrf_token == "csrf-tok-123"

    async def test_invalid_credentials_raises_auth_error(self):
        get_r, post_r = _login_sequence(credentials_ok=False)
        session = _FakeSession(get_r, post_r)
        api = MijnIstaAPI(session, "user@example.com", "wrong")
        with pytest.raises(MijnIstaAuthError):
            await api.authenticate()

    async def test_challenge_not_redirected_raises_connection_error(self):
        session = _FakeSession(
            get_responses=[_FakeResponse(200, text=""), _FakeResponse(401)],
        )
        api = MijnIstaAPI(session, "u", "p")
        with pytest.raises(MijnIstaConnectionError):
            await api.authenticate()

    async def test_missing_login_form_raises_connection_error(self):
        session = _FakeSession(
            get_responses=[
                _FakeResponse(200, text=""),
                _FakeResponse(302, headers={"Location": AUTHORIZE_URL}),
                _FakeResponse(200, text="<html>not a login form</html>"),
            ],
        )
        api = MijnIstaAPI(session, "u", "p")
        with pytest.raises(MijnIstaConnectionError):
            await api.authenticate()

    async def test_network_error_raises_connection_error(self):
        session = _FakeSession(
            get_responses=[_RaisingContextManager(aiohttp.ClientError("down"))]
        )
        api = MijnIstaAPI(session, "u", "p")
        with pytest.raises(MijnIstaConnectionError):
            await api.authenticate()

    async def test_timeout_raises_connection_error(self):
        session = _FakeSession(get_responses=[_RaisingContextManager(TimeoutError())])
        api = MijnIstaAPI(session, "u", "p")
        with pytest.raises(MijnIstaConnectionError):
            await api.authenticate()


# ---------------------------------------------------------------------------
# Request headers / body construction
# ---------------------------------------------------------------------------


class TestRequestConstruction:
    async def test_data_call_sends_csrf_and_auth_headers(self):
        api = await _authenticated_api()
        api._session = _FakeSession(
            post_responses=[_FakeResponse(200, json_data={"Cus": []})]
        )
        await api.get_user_values()
        _, kwargs = api._session.post_calls[0]
        assert kwargs["headers"]["x-csrf-token-ista-nl_tp"] == "csrf-tok-123"
        assert kwargs["headers"]["Authorization"].startswith("Bearer ")

    def test_body_merges_lang_and_extra(self):
        api = MijnIstaAPI(MagicMock(), "u", "p", lang="nl-NL")
        body = api._body({"extra": "val"})
        assert body == {"LANG": "nl-NL", "extra": "val"}
        assert "JWT" not in body


# ---------------------------------------------------------------------------
# 401 retry mechanism
# ---------------------------------------------------------------------------


class TestRetryOn401:
    async def test_401_triggers_reauth_and_retries(self):
        api = await _authenticated_api()

        get_r, post_r = _login_sequence()
        post_r = (
            [_FakeResponse(401)] + post_r + [_FakeResponse(200, json_data={"Cus": []})]
        )
        api._session = _FakeSession(get_r, post_r)

        result = await api.get_user_values()
        assert result == {"Cus": []}
        # first call 401'd, then a full re-authenticate() ran, then the retry succeeded
        assert api._csrf_token == "csrf-tok-123"


# ---------------------------------------------------------------------------
# 425 / 503 retry mechanism
# ---------------------------------------------------------------------------


class TestRetryOn425And503:
    async def test_503_retries_and_succeeds(self):
        api = await _authenticated_api()
        api._session = _FakeSession(
            post_responses=[
                _FakeResponse(503),
                _FakeResponse(200, json_data={"Cus": []}),
            ]
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await api.get_user_values()
        assert result == {"Cus": []}

    async def test_persistent_503_raises_connection_error(self):
        api = await _authenticated_api()
        api._session = _FakeSession(post_responses=[_FakeResponse(503)] * 4)
        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(MijnIstaConnectionError),
        ):
            await api.get_user_values()


# ---------------------------------------------------------------------------
# MonthValues shard polling
# ---------------------------------------------------------------------------


class TestMonthValuesShardsPolling:
    async def test_polls_until_shards_loaded(self):
        api = await _authenticated_api()
        api._session = _FakeSession(
            post_responses=[
                _FakeResponse(200, json_data={"hs": 1, "sh": 3, "mc": []}),
                _FakeResponse(200, json_data={"hs": 2, "sh": 3, "mc": []}),
                _FakeResponse(
                    200, json_data={"hs": 3, "sh": 3, "mc": [{"y": 2024, "m": 11}]}
                ),
            ]
        )
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await api.get_month_values("cuid-1")
        assert result["hs"] == 3
        assert len(result["mc"]) == 1
        assert mock_sleep.call_count == 2

    async def test_no_shard_info_keeps_polling_until_data_arrives(self):
        """sh == 0 means no shard info yet; keep polling until a real shard count shows up."""
        api = await _authenticated_api()
        api._session = _FakeSession(
            post_responses=[
                _FakeResponse(200, json_data={"sh": 0, "hs": 0, "mc": []}),
                _FakeResponse(200, json_data={"sh": 1, "hs": 1, "mc": []}),
            ]
        )
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await api.get_month_values("cuid-1")
        assert mock_sleep.call_count == 1
        assert result["mc"] == []

    async def test_quick_mode_stops_at_first_mc_entries(self):
        api = await _authenticated_api()
        api._session = _FakeSession(
            post_responses=[
                _FakeResponse(
                    200, json_data={"hs": 1, "sh": 20, "mc": [{"y": 2024, "m": 11}]}
                ),
            ]
        )
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await api.get_month_values("cuid-1", quick=True)
        assert mock_sleep.call_count == 0
        assert len(result["mc"]) == 1

    async def test_401_during_shard_poll_reauthenticates(self):
        api = await _authenticated_api()
        get_r, post_r = _login_sequence()
        post_r = (
            [_FakeResponse(401)]
            + post_r
            + [
                _FakeResponse(
                    200, json_data={"hs": 1, "sh": 1, "mc": [{"y": 2024, "m": 11}]}
                )
            ]
        )
        api._session = _FakeSession(get_r, post_r)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await api.get_month_values("cuid-1")
        assert len(result["mc"]) == 1


# ---------------------------------------------------------------------------
# Endpoint convenience methods
# ---------------------------------------------------------------------------


class TestEndpoints:
    async def test_get_user_values_posts_to_correct_path(self):
        api = await _authenticated_api()
        api._session = _FakeSession(
            post_responses=[_FakeResponse(200, json_data={"DisplayName": "Test"})]
        )
        result = await api.get_user_values()
        assert result["DisplayName"] == "Test"
        url = api._session.post_calls[0][0]
        assert "/api/Values/UserValues" in url

    async def test_get_month_values_sends_cuid(self):
        api = await _authenticated_api()
        api._session = _FakeSession(
            post_responses=[_FakeResponse(200, json_data={"sh": 1, "hs": 1, "mc": []})]
        )
        await api.get_month_values("my-cuid")
        body = api._session.post_calls[0][1]["json"]
        assert body["Cuid"] == "my-cuid"

    async def test_get_consumption_averages_sends_par(self):
        api = await _authenticated_api()
        api._session = _FakeSession(
            post_responses=[_FakeResponse(200, json_data={"Averages": []})]
        )
        await api.get_consumption_averages("cuid-1", "2024-01-01", "2024-12-31")
        body = api._session.post_calls[0][1]["json"]
        assert body["PAR"]["start"] == "2024-01-01"
        assert body["PAR"]["end"] == "2024-12-31"
        assert body["PAR"]["cuid"] == "cuid-1"
