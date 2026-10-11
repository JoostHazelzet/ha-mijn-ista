"""Resilient mijn.ista.nl client: cookie handling, rate limiting and login retries.

Wraps ``mijn_ista_api.MijnIstaAPI`` without changing the library. See REQUIREMENTS.md
(3.7, NFR-2, NFR-5) and notebooks/auth_playground.ipynb for the background.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util.hass_dict import HassKey
from mijn_ista_api import MijnIstaAPI, MijnIstaAuthError, MijnIstaConnectionError
from yarl import URL

from .const import (
    DOMAIN,
    HANDOFF_MAX_AGE_S,
    INVALID_CREDENTIALS_CONFIRMATIONS,
    LOGIN_ATTEMPTS,
    MIN_LOGIN_GAP_S,
    MIN_REQUEST_GAP_S,
    MONTH_BUDGET_FULL_S,
    MONTH_BUDGET_QUICK_S,
    MONTH_POLL_INTERVAL_S,
)

_LOGGER = logging.getLogger(__name__)

API_HOST = "mijn.ista.nl"
_HOME_URL = URL(f"https://{API_HOST}/Home")
_MONTH_VALUES = "/api/Consumption/MonthValues"
_SESSION_COOKIE = ".AspNetCore.Cookies"


class IstaCookieJar(aiohttp.CookieJar):
    """Cookie jar that always sends every cookie, regardless of the total header size.

    mijn.ista.nl stores its ASP.NET session ticket as a chunked cookie (``.AspNetCore.Cookies``
    = "chunks-3" plus ``C1``..``C3``, about 8.8 kB). aiohttp >= 3.14.4 caps the Cookie header at
    8190 bytes and silently drops what doesn't fit, so ``/Home`` answers 401 after every login.
    """

    # Private aiohttp switch (also used by DummyCookieJar); harmless on versions without it.
    _limits_enabled = False


class MijnIstaRateLimited(MijnIstaConnectionError):
    """A login is not allowed yet and the caller chose not to wait for it."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"login rate limit: next login allowed in {retry_after:.0f} s")
        self.retry_after = retry_after


class Throttle:
    """Enforce a minimum gap between the starts of consecutive operations."""

    def __init__(self, name: str, min_gap: float) -> None:
        self.name = name
        self.min_gap = min_gap
        self._lock = asyncio.Lock()
        self._last: float | None = None

    def remaining(self) -> float:
        """Seconds until the next operation may start."""
        if self._last is None:
            return 0.0
        return max(0.0, self._last + self.min_gap - time.monotonic())

    async def acquire(self, max_wait: float | None = None) -> None:
        """Wait for the gap, then mark the start of an operation.

        Raises MijnIstaRateLimited if the wait would exceed ``max_wait`` seconds.
        """
        async with self._lock:
            wait = self.remaining()
            if max_wait is not None and wait > max_wait:
                raise MijnIstaRateLimited(wait)
            if wait > 0:
                _LOGGER.debug("%s: waiting %.1f s", self.name, wait)
                await asyncio.sleep(wait)
            self._last = time.monotonic()


@dataclass
class MijnIstaShared:
    """State shared by all mijn_ista config entries and flows."""

    login_gate: Throttle = field(
        default_factory=lambda: Throttle("mijn_ista login gate", MIN_LOGIN_GAP_S)
    )
    request_throttle: Throttle = field(
        default_factory=lambda: Throttle("mijn_ista request throttle", MIN_REQUEST_GAP_S)
    )
    # Consecutive "invalid credentials" responses per username (lower case).
    invalid_streak: dict[str, int] = field(default_factory=dict)
    # Authenticated clients from the config flow, waiting to be picked up by setup.
    handoff: dict[str, tuple[ResilientMijnIstaAPI, float]] = field(default_factory=dict)


DATA_SHARED: HassKey[MijnIstaShared] = HassKey(DOMAIN)


def get_shared(hass: HomeAssistant) -> MijnIstaShared:
    """Return the domain-wide shared state, creating it on first use."""
    if (shared := hass.data.get(DATA_SHARED)) is None:
        shared = hass.data[DATA_SHARED] = MijnIstaShared()
    return shared


def api_language(hass: HomeAssistant) -> str:
    """API language derived from the Home Assistant language."""
    return "nl-NL" if hass.config.language.startswith("nl") else "en-GB"


def throttle_middleware(throttle: Throttle):
    """aiohttp client middleware spacing out data requests to mijn.ista.nl/api/*.

    Runs for every request the session sends, so the library's shard polls and internal
    retries are throttled too. Login requests are not affected (they have their own gate).
    """

    async def middleware(
        request: aiohttp.ClientRequest, handler: Any
    ) -> aiohttp.ClientResponse:
        if request.url.host == API_HOST and request.url.path.startswith("/api/"):
            await throttle.acquire()
        return await handler(request)

    return middleware


def async_create_ista_session(
    hass: HomeAssistant, shared: MijnIstaShared, *, auto_cleanup: bool = True
) -> aiohttp.ClientSession:
    """Private session with an IstaCookieJar and the shared request throttle.

    Inside config entry setup, ``auto_cleanup`` detaches the session on unload. Flow
    sessions use ``auto_cleanup=False`` and are detached by their owner.
    """
    return async_create_clientsession(
        hass,
        auto_cleanup=auto_cleanup,
        cookie_jar=IstaCookieJar(),
        middlewares=(throttle_middleware(shared.request_throttle),),
    )


def missing_session_cookie_chunks(jar: aiohttp.abc.AbstractCookieJar) -> list[str]:
    """Names of session-cookie chunks that are stored but would not be sent to mijn.ista.nl."""
    header = next(
        (c for c in jar if c.key == _SESSION_COOKIE and c["domain"] in ("", API_HOST)),
        None,
    )
    if header is None or not header.value.startswith("chunks-"):
        return []
    try:
        count = int(header.value.removeprefix("chunks-"))
    except ValueError:
        return []
    sent = jar.filter_cookies(_HOME_URL)
    expected = [_SESSION_COOKIE, *(f"{_SESSION_COOKIE}C{i}" for i in range(1, count + 1))]
    return [name for name in expected if name not in sent]


def _is_invalid_credentials(exc: Exception) -> bool:
    return isinstance(exc, MijnIstaAuthError) and "invalid credentials" in str(exc).lower()


class ResilientMijnIstaAPI(MijnIstaAPI):
    """MijnIstaAPI with gated, retrying logins and month polling suited to the request throttle.

    ``login_attempts``, ``login_max_wait`` and ``invalid_confirmations`` may be adjusted per
    call site: setup must not sleep on the login gate, flows report a rejection at once.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        shared: MijnIstaShared,
        lang: str = "nl-NL",
    ) -> None:
        super().__init__(session, username, password, lang=lang)
        self._shared = shared
        self.login_attempts = LOGIN_ATTEMPTS
        self.login_max_wait: float | None = None
        self.invalid_confirmations = INVALID_CREDENTIALS_CONFIRMATIONS

    @property
    def session(self) -> aiohttp.ClientSession:
        """The client session this API uses."""
        return self._session

    def matches(self, username: str, password: str, lang: str) -> bool:
        """Whether this client was created for exactly these settings."""
        return (self._username, self._password, self._lang) == (username, password, lang)

    async def authenticate(self) -> None:
        """Log in, retrying transient failures and classifying credential rejections.

        Every attempt passes the shared login gate and starts from an empty cookie jar.
        Raises MijnIstaAuthError only after ``invalid_confirmations`` consecutive
        "invalid credentials" responses for this username; any other failure ends in
        MijnIstaConnectionError (MijnIstaRateLimited if the gate is closed and waiting
        is not allowed).
        """
        key = self._username.lower()
        streak = self._shared.invalid_streak
        last: Exception | None = None

        for attempt in range(1, self.login_attempts + 1):
            await self._shared.login_gate.acquire(self.login_max_wait)
            self._session.cookie_jar.clear()
            try:
                await super().authenticate()
            except (MijnIstaAuthError, MijnIstaConnectionError) as exc:
                last = exc
                if _is_invalid_credentials(exc):
                    streak[key] = streak.get(key, 0) + 1
                    if streak[key] >= self.invalid_confirmations:
                        _LOGGER.warning(
                            "mijn.ista.nl rejected the credentials %d time(s) in a row",
                            streak[key],
                        )
                        raise
                else:
                    streak.pop(key, None)
                missing = missing_session_cookie_chunks(self._session.cookie_jar)
                _LOGGER.warning(
                    "mijn.ista.nl login attempt %d/%d failed: %s%s",
                    attempt,
                    self.login_attempts,
                    exc,
                    f" (session cookie chunks not sent: {', '.join(missing)})"
                    if missing
                    else "",
                )
                continue
            streak.pop(key, None)
            if attempt > 1:
                _LOGGER.info("mijn.ista.nl login succeeded on attempt %d", attempt)
            return

        raise MijnIstaConnectionError(
            f"login failed after {self.login_attempts} attempt(s): {last}"
        ) from last

    async def get_month_values(
        self, cuid: str, *, quick: bool = False
    ) -> dict[str, Any]:
        """Poll MonthValues shards at the request-throttle pace.

        Same contract as the library method, but with an interval and budgets suited to
        MIN_REQUEST_GAP_S (the library polls every 2 s within 15 s / 60 s).
        """
        budget = MONTH_BUDGET_QUICK_S if quick else MONTH_BUDGET_FULL_S
        deadline = time.monotonic() + budget
        data: dict[str, Any] = {}

        while True:
            polled = await self._poll_shard(_MONTH_VALUES, {"Cuid": cuid})
            if polled is not None:
                data = polled
                total, loaded = data.get("sh", 0), data.get("hs", 0)
                if total > 0 and loaded >= total:
                    break
                if quick and data.get("mc"):
                    break
            if time.monotonic() + MONTH_POLL_INTERVAL_S >= deadline:
                break
            await asyncio.sleep(MONTH_POLL_INTERVAL_S)

        if not quick and data.get("sh", 0) > data.get("hs", 0):
            _LOGGER.warning(
                "mijn.ista.nl: MonthValues time budget exceeded, returning %d/%d shards",
                data.get("hs", 0),
                data.get("sh", 0),
            )
        return data


def stash_handoff(shared: MijnIstaShared, api: ResilientMijnIstaAPI, username: str) -> None:
    """Keep a flow-validated client so the entry's setup can reuse its session."""
    previous = shared.handoff.pop(username.lower(), None)
    if previous is not None:
        previous[0].session.detach()
    shared.handoff[username.lower()] = (api, time.monotonic())


def take_handoff(shared: MijnIstaShared, username: str) -> ResilientMijnIstaAPI | None:
    """Return a fresh flow-validated client for ``username``; discard stale ones."""
    now = time.monotonic()
    for key, (api, created) in list(shared.handoff.items()):
        if now - created > HANDOFF_MAX_AGE_S:
            del shared.handoff[key]
            api.session.detach()
    item = shared.handoff.pop(username.lower(), None)
    return item[0] if item else None
