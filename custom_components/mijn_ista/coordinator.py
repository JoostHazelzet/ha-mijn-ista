"""Fetch and normalize readings and source-provided metrics from mijn.ista.nl."""

from __future__ import annotations

import asyncio
import logging
import math
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from mijn_ista_api import MijnIstaAuthError, MijnIstaConnectionError

from .api import ResilientMijnIstaAPI
from .const import (
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    LOGIN_ATTEMPT_TIMEOUT_S,
    LOGIN_ATTEMPTS,
    MIN_LOGIN_GAP_S,
    MONTH_BUDGET_FULL_S,
    MONTH_BUDGET_QUICK_S,
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
class ServiceConsumption:
    """API-provided consumption and benchmarks, without calculated rollups."""

    total: float | None = None
    meters: list[dict[str, Any]] = field(default_factory=list)
    monthly: float | None = None
    building_average: float | None = None
    normalized: float | None = None
    has_approximation: bool | None = None


@dataclass
class CustomerData:
    """Property metadata and current raw readings keyed by service and meter."""

    cuid: str
    address: str
    zip_code: str
    city: str
    services: list[ServiceInfo]
    meters: dict[tuple[int, int], MeterReading]
    consumption: dict[int, ServiceConsumption] = field(default_factory=dict)
    month: date | None = None
    outdoor_temperature: float | None = None
    last_sync: datetime | None = None


def _number(value: Any, label: str, *, signed: bool = False) -> float | None:
    if value is None:
        return None
    try:
        if (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and (signed or value >= 0)
        ):
            return float(value)
    except OverflowError:
        pass
    _LOGGER.warning("mijn.ista.nl: invalid %s", label)
    return None


def _parse_totals(customer: CustomerData, payload: dict[str, Any]) -> None:
    for raw in _objects(payload.get("ServicesComp"), "ServicesComp"):
        sid = _identifier(raw.get("Id"), "ServicesComp.Id")
        data = customer.consumption.setdefault(sid, ServiceConsumption())
        meters = _objects(raw.get("CurMeters"), "CurMeters")
        for meter in meters:
            number = meter.get("MeterNr")
            if number is not None and (
                isinstance(number, bool) or not isinstance(number, (str, int))
            ):
                raise TypeError("MeterNr must be a string or integer")
        data.total = _number(raw.get("TotalNow"), "TotalNow")
        if "TotalNow" not in raw and len(meters) == 1:
            data.total = _number(meters[0].get("CValue"), "CValue")
        data.meters = [
            {
                "meter_number": str(meter["MeterNr"])
                if meter.get("MeterNr") is not None
                else None,
                "begin_value": _number(meter.get("BeginValue"), "BeginValue"),
                "end_value": _number(meter.get("EndValue"), "EndValue"),
            }
            for meter in meters
        ]


def _parse_months(customer: CustomerData, payload: dict[str, Any]) -> None:
    today = dt_util.now().date()
    customer.month = today.replace(day=1)
    for raw in _objects(payload.get("mc"), "mc"):
        year = _identifier(raw.get("y"), "mc.y")
        month = _identifier(raw.get("m"), "mc.m")
        date(year, month, 1)
        if (year, month) != (today.year, today.month):
            continue
        customer.outdoor_temperature = _number(raw.get("at"), "at", signed=True)
        for service in _objects(raw.get("ServiceConsumptions"), "ServiceConsumptions"):
            sid = _identifier(service.get("ServiceId"), "ServiceId")
            data = customer.consumption.setdefault(sid, ServiceConsumption())
            data.monthly = _number(service.get("TotalConsumption"), "TotalConsumption")
            data.building_average = _number(
                service.get("BuildingAverage", service.get("BuldingAverage")),
                "BuildingAverage",
            )
            approximation = service.get("HasApproximation")
            if approximation is not None and not isinstance(approximation, bool):
                raise TypeError("HasApproximation must be a boolean")
            data.has_approximation = approximation
    timestamp = payload.get("ed")
    if timestamp is not None:
        parsed = (
            dt_util.parse_datetime(timestamp) if isinstance(timestamp, str) else None
        )
        if parsed is None or parsed.tzinfo is None:
            _LOGGER.warning("mijn.ista.nl: invalid or timezone-less ed timestamp")
        else:
            customer.last_sync = parsed


def _parse_averages(customer: CustomerData, payload: dict[str, Any]) -> None:
    for raw in _objects(payload.get("Averages"), "Averages"):
        sid = _identifier(raw.get("BillingServiceId"), "BillingServiceId")
        customer.consumption.setdefault(sid, ServiceConsumption()).normalized = _number(
            raw.get("NormalizedValue"), "NormalizedValue"
        )


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
    """Extract current meter registers and source-provided service totals."""
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
    customer = CustomerData(
        cuid=cuid,
        address=_text(cus.get("Adress"), "Adress"),
        zip_code=_text(cus.get("Zip"), "Zip"),
        city=_text(cus.get("City"), "City"),
        services=services,
        meters=meters,
    )
    _parse_totals(customer, cur)
    return customer


def login_budget(attempts: int, *, waits: bool) -> float:
    """Worst-case seconds for logins, including login-gate waits."""
    return attempts * ((MIN_LOGIN_GAP_S if waits else 0) + LOGIN_ATTEMPT_TIMEOUT_S)


class MijnIstaCoordinator(DataUpdateCoordinator[dict[str, CustomerData]]):
    """Fetch registers, consumption, and source-provided benchmarks."""

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
            return await self._fetch_all(budget)
        except TimeoutError as exc:
            raise UpdateFailed("mijn.ista.nl data fetch timed out") from exc

    async def _fetch_all(self, budget: float) -> dict[str, CustomerData]:
        try:
            async with asyncio.timeout(budget):
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
                await self._fetch_customer_data(customer, cus)
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

    async def _fetch_customer_data(
        self, customer: CustomerData, raw: dict[str, Any]
    ) -> None:
        """Optional datasets must not make valid physical registers unavailable."""
        cur = raw.get("curConsumption") or {}
        today = dt_util.now().date()
        period = None
        try:
            for candidate in _objects(cur.get("BillingPeriods"), "BillingPeriods"):
                start = _reading_date(candidate.get("s"))
                end = _reading_date(candidate.get("e"))
                if start and end and start <= today.isoformat() <= end:
                    period = candidate
                    break
        except (TypeError, ValueError) as exc:
            _LOGGER.warning("mijn.ista.nl: billing period unavailable: %s", exc)
        operations = [
            ("monthly", self.api.get_month_values, (customer.cuid,), _parse_months)
        ]
        if period is not None:
            operations.extend(
                [
                    (
                        "consumption",
                        self.api.get_consumption_values,
                        (customer.cuid, period),
                        _parse_totals,
                    ),
                    (
                        "averages",
                        self.api.get_consumption_averages,
                        (
                            customer.cuid,
                            _reading_date(period["s"]),
                            _reading_date(period["e"]),
                        ),
                        _parse_averages,
                    ),
                ]
            )
        else:
            _LOGGER.warning("mijn.ista.nl: no active billing period for benchmarks")
        for label, fetch, args, parse in operations:
            try:
                budget = (
                    (
                        MONTH_BUDGET_QUICK_S
                        if not self._first_refresh_done
                        else MONTH_BUDGET_FULL_S
                    )
                    if label == "monthly"
                    else 2 * REQUEST_BUDGET_S
                ) + login_budget(
                    self.api.login_attempts, waits=self._first_refresh_done
                )
                async with asyncio.timeout(budget):
                    payload = (
                        await fetch(*args, quick=not self._first_refresh_done)
                        if label == "monthly"
                        else await fetch(*args)
                    )
                if not isinstance(payload, dict):
                    raise TypeError(f"{label} must be an object")
                required = {
                    "monthly": "mc",
                    "consumption": "ServicesComp",
                    "averages": "Averages",
                }[label]
                if required not in payload:
                    raise ValueError(f"{label} is missing {required}")
                parsed = deepcopy(customer)
                parse(parsed, payload)
                customer.consumption = parsed.consumption
                customer.month = parsed.month
                customer.outdoor_temperature = parsed.outdoor_temperature
                customer.last_sync = parsed.last_sync
            except (
                MijnIstaConnectionError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as exc:
                _LOGGER.warning("mijn.ista.nl: %s data unavailable: %s", label, exc)
