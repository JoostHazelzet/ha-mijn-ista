"""Fetch and normalize raw meter readings from mijn.ista.nl."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from mijn_ista_api import MijnIstaAuthError, MijnIstaConnectionError

from .api import ResilientMijnIstaAPI
from .const import (
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    LOGIN_ATTEMPT_TIMEOUT_S,
    LOGIN_ATTEMPTS,
    MIN_LOGIN_GAP_S,
    REQUEST_BUDGET_S,
    RETRY_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceInfo:
    """Metadata about one billing service."""

    id: int
    description: str
    meter_type: str
    unit: str


@dataclass(frozen=True)
class MeterReading:
    """An API register value, not consumption over a billing period."""

    meter_id: int
    service_id: int
    serial_nr: str
    meter_type: str
    value: float | None
    reading_date: str | None
    unit: str | None


@dataclass
class CustomerData:
    """Property metadata and current raw readings keyed by service and meter."""

    cuid: str
    address: str
    zip_code: str
    city: str
    services: list[ServiceInfo]
    meters: dict[tuple[int, int], MeterReading]


def _objects(value: Any, field: str) -> list[dict[str, Any]]:
    """Validate an optional API collection."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise TypeError(f"{field} must be a list of objects")
    return value


def _identifier(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _reading_date(value: Any) -> str | None:
    """Normalize a date without inventing a time or timezone."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
        try:
            return datetime.strptime(value, "%d-%m-%Y").date().isoformat()
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            pass
    _LOGGER.warning("mijn.ista.nl: invalid meter reading date")
    return None


def _register_unit(service: ServiceInfo | None) -> str | None:
    """Use service units only for physical register families verified in API samples.

    Heat-cost allocators can have a service total in GJ but dimensionless registers.
    Unknown families stay unclassified until their register contract is verified.
    """
    if service is None:
        return None
    meter_type = service.meter_type.casefold()
    if meter_type.startswith("domaqua") and service.unit in ("m3", "m³"):
        return service.unit
    if meter_type.startswith("ultego") and service.unit in ("Gigajoule", "kWh"):
        return service.unit
    return None


def _parse_customer(cus: dict[str, Any]) -> CustomerData:
    """Extract current meter registers, ignoring all comparisons and rollups."""
    cuid = _text(cus.get("Cuid"), "Cuid")
    if not cuid:
        raise ValueError("Cuid is required")
    cur = cus.get("curConsumption")
    if cur is None:
        cur = {}
    if not isinstance(cur, dict):
        raise TypeError("curConsumption must be an object")
    services = [
        ServiceInfo(
            id=_identifier(s.get("Id"), "Billingservices.Id"),
            description=_text(s.get("Description"), "Description"),
            meter_type=_text(s.get("MeterType"), "MeterType"),
            unit=_text(s.get("Unit"), "Unit"),
        )
        for s in _objects(cur.get("Billingservices"), "Billingservices")
    ]
    svc_by_id = {s.id: s for s in services}
    meters: dict[tuple[int, int], MeterReading] = {}
    for sc in _objects(cur.get("ServicesComp"), "ServicesComp"):
        sid = _identifier(sc.get("Id"), "ServicesComp.Id")
        service = svc_by_id.get(sid)
        unit = _register_unit(service)
        for raw in _objects(sc.get("CurMeters"), "CurMeters"):
            mid = _identifier(raw.get("MeterId"), "MeterId")
            key = (sid, mid)
            if key in meters:
                raise ValueError("duplicate current meter reading")
            serial = raw.get("MeterNr")
            if serial is not None and (
                isinstance(serial, bool) or not isinstance(serial, (str, int))
            ):
                raise TypeError("MeterNr must be a string or integer")
            value = raw.get("EndValue")
            try:
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                    and (unit is None or value >= 0)
                )
            except OverflowError:
                valid = False
            if not valid:
                _LOGGER.warning(
                    "mijn.ista.nl: missing or invalid EndValue for service %s meter %s",
                    sid,
                    mid,
                )
                value = None
            meters[key] = MeterReading(
                meter_id=mid,
                service_id=sid,
                serial_nr=str(serial) if serial is not None else "",
                meter_type=service.meter_type if service else "",
                value=float(value) if value is not None else None,
                reading_date=_reading_date(raw.get("EsDate")),
                unit=unit,
            )
    return CustomerData(
        cuid=cuid,
        address=_text(cus.get("Adress"), "Adress"),
        zip_code=_text(cus.get("Zip"), "Zip"),
        city=_text(cus.get("City"), "City"),
        services=services,
        meters=meters,
    )


def login_budget(attempts: int, *, waits: bool) -> float:
    """Worst-case seconds for logins, including login-gate waits."""
    return attempts * ((MIN_LOGIN_GAP_S if waits else 0) + LOGIN_ATTEMPT_TIMEOUT_S)


class MijnIstaCoordinator(DataUpdateCoordinator[dict[str, CustomerData]]):
    """Fetch raw current registers from UserValues only."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: ResilientMijnIstaAPI,
        *,
        authenticated: bool = False,
    ) -> None:
        self.api = api
        self._first_refresh_done = False
        self._skip_next_login = authenticated
        self._regular_interval = timedelta(
            hours=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        )
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=f"{DOMAIN}-{entry.entry_id}",
            update_method=self._async_update_data,
            update_interval=self._regular_interval,
        )

    async def _async_update_data(self) -> dict[str, CustomerData]:
        try:
            data = await self._fetch_with_timeout()
        except UpdateFailed:
            self.update_interval = min(
                self._regular_interval, timedelta(minutes=RETRY_INTERVAL_MINUTES)
            )
            raise
        self.update_interval = self._regular_interval
        return data

    async def _fetch_with_timeout(self) -> dict[str, CustomerData]:
        first = not self._first_refresh_done
        self.api.login_attempts = 1 if first else LOGIN_ATTEMPTS
        self.api.login_max_wait = 0 if first else None
        login = 0 if self._skip_next_login else self.api.login_attempts
        # UserValues may reauthenticate once after a 401, even with a handed-over session.
        budget = (
            login_budget(login, waits=not first)
            + login_budget(1, waits=not first)
            + 2 * REQUEST_BUDGET_S
        )
        try:
            async with asyncio.timeout(budget):
                return await self._fetch_all()
        except TimeoutError as exc:
            raise UpdateFailed("mijn.ista.nl data fetch timed out") from exc

    async def _fetch_all(self) -> dict[str, CustomerData]:
        try:
            if self._skip_next_login:
                self._skip_next_login = False
            else:
                await self.api.authenticate()
            user_data = await self.api.get_user_values()
            if not isinstance(user_data, dict) or not isinstance(
                user_data.get("Cus"), list
            ):
                raise TypeError("UserValues must contain a Cus list")
            customers = _objects(user_data["Cus"], "Cus")
            result: dict[str, CustomerData] = {}
            for cus in customers:
                customer = _parse_customer(cus)
                if customer.cuid in result:
                    raise ValueError("duplicate property in UserValues")
                result[customer.cuid] = customer
                _LOGGER.debug(
                    "mijn.ista.nl: loaded %d raw meter readings",
                    len(customer.meters),
                )
            self._first_refresh_done = True
            return result
        except MijnIstaAuthError as exc:
            raise ConfigEntryAuthFailed from exc
        except MijnIstaConnectionError as exc:
            raise UpdateFailed(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise UpdateFailed(f"Invalid mijn.ista.nl payload: {exc}") from exc
