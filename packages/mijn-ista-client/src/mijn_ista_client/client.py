"""Home Assistant-independent cookie handling, login retries and shard polling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from mijn_ista_api import MijnIstaAPI, MijnIstaAuthError, MijnIstaConnectionError
from yarl import URL

from .const import (
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
    """Send all ASP.NET session chunks, including tickets larger than 8190 bytes."""

    # Private aiohttp switch; harmless on versions without the header limit.
    _limits_enabled = False


class MijnIstaRateLimited(MijnIstaConnectionError):
    """A login cannot start within the caller's maximum wait."""

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
        """Wait for the gap, or raise MijnIstaRateLimited if it exceeds max_wait."""
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
    """Rate limits and credential-rejection state shared within one event loop.

    Reuse this object across clients/accounts to enforce process-wide spacing.
    Session ownership and application-specific state belong to the caller.
    """

    login_gate: Throttle = field(
        default_factory=lambda: Throttle("mijn_ista login gate", MIN_LOGIN_GAP_S)
    )
    request_throttle: Throttle = field(
        default_factory=lambda: Throttle(
            "mijn_ista request throttle", MIN_REQUEST_GAP_S
        )
    )
    invalid_streak: dict[str, int] = field(default_factory=dict)


def throttle_middleware(throttle: Throttle) -> aiohttp.ClientMiddlewareType:
    """Space data requests, including upstream shard polls and internal retries."""

    async def middleware(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        if request.url.host == API_HOST and request.url.path.startswith("/api/"):
            await throttle.acquire()
        return await handler(request)

    return middleware


def create_session(shared: MijnIstaShared) -> aiohttp.ClientSession:
    """Create a private session inside an event loop; the caller must close it."""
    return aiohttp.ClientSession(
        cookie_jar=IstaCookieJar(),
        middlewares=(throttle_middleware(shared.request_throttle),),
    )


def missing_session_cookie_chunks(jar: aiohttp.abc.AbstractCookieJar) -> list[str]:
    """Session-cookie chunks that would not be sent to mijn.ista.nl."""
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
    expected = [
        _SESSION_COOKIE,
        *(f"{_SESSION_COOKIE}C{i}" for i in range(1, count + 1)),
    ]
    return [name for name in expected if name not in sent]


def _is_invalid_credentials(exc: Exception) -> bool:
    return (
        isinstance(exc, MijnIstaAuthError) and "invalid credentials" in str(exc).lower()
    )


class ResilientMijnIstaAPI(MijnIstaAPI):
    """Upstream API contract with gated logins and throttle-aware month polling.

    With an externally supplied session, use IstaCookieJar and throttle_middleware
    as create_session does. The caller owns and closes that session.
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
        """The caller-owned session used by this API."""
        return self._session

    def matches(self, username: str, password: str, lang: str) -> bool:
        """Whether this client was created for exactly these settings."""
        return (self._username, self._password, self._lang) == (
            username,
            password,
            lang,
        )

    async def authenticate(self) -> None:
        """Retry transient failures; only confirmed credential rejections are auth errors."""
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
        """Return upstream MonthValues payloads, tolerating partially loaded shards."""
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


@asynccontextmanager
async def open_client(
    username: str,
    password: str,
    *,
    shared: MijnIstaShared | None = None,
    lang: str = "nl-NL",
) -> AsyncGenerator[ResilientMijnIstaAPI]:
    """Own a private session for a script; authentication remains explicit.

    Pass the same shared object to all clients in an event loop to coordinate limits.
    Without it, rate limits apply only to this context.
    """
    if shared is None:
        shared = MijnIstaShared()
    async with create_session(shared) as session:
        yield ResilientMijnIstaAPI(
            session, username, password, shared=shared, lang=lang
        )
