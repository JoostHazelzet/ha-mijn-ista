"""Raw meter reading sensors for mijn.ista.nl."""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import (
    async_entries_for_config_entry,
)
from homeassistant.helpers.entity_registry import (
    async_get as async_get_entity_registry,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, SERVICE_NAME_TRANSLATIONS
from .coordinator import CustomerData, MeterReading, MijnIstaCoordinator

PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)

_UNIT_MAP: dict[str, str] = {
    "Gigajoule": UnitOfEnergy.GIGA_JOULE,
    "kWh": UnitOfEnergy.KILO_WATT_HOUR,
    "m3": UnitOfVolume.CUBIC_METERS,
    "m³": UnitOfVolume.CUBIC_METERS,
}
_DEVICE_CLASS_MAP: dict[str, SensorDeviceClass] = {
    "Gigajoule": SensorDeviceClass.ENERGY,
    "kWh": SensorDeviceClass.ENERGY,
    "m3": SensorDeviceClass.WATER,
    "m³": SensorDeviceClass.WATER,
}
_OBSOLETE_SENSOR = re.compile(
    rf"{DOMAIN}_.+_(?:"
    r"svc\d+_(?:annual_current|annual_previous|annual_diff_pct|"
    r"building_avg_annual|month_latest|month_building_avg|dev\d+_(?:annual|month))"
    r"|avg_temp_(?:current|previous)_period)"
)


def _translate_service(description: str, ha_language: str) -> str:
    """Preserve existing Dutch/English service naming."""
    if ha_language.startswith("en"):
        return SERVICE_NAME_TRANSLATIONS.get(description, description)
    return description


def _unique_id(cuid: str, meter: MeterReading) -> str:
    return f"{DOMAIN}_{cuid}_svc{meter.service_id}_dev{meter.meter_id}_reading"


class MijnIstaSensor(CoordinatorEntity[MijnIstaCoordinator], SensorEntity):
    """One physical meter's current register, with no derived consumption."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MijnIstaCoordinator,
        cuid: str,
        meter: MeterReading,
        label: str,
    ) -> None:
        super().__init__(coordinator)
        self._cuid = cuid
        self._key = (meter.service_id, meter.meter_id)
        self._attr_unique_id = _unique_id(cuid, meter)
        self._attr_name = f"{label} {meter.serial_nr or meter.meter_id}"
        if meter.unit is None:
            _LOGGER.warning(
                "mijn.ista.nl: service %s meter %s has an unverified register "
                "type or unit; publishing without statistics metadata",
                meter.service_id,
                meter.meter_id,
            )

    @property
    def _reading(self) -> MeterReading | None:
        customer = (self.coordinator.data or {}).get(self._cuid)
        return customer.meters.get(self._key) if customer else None

    @property
    def available(self) -> bool:
        reading = self._reading
        return super().available and reading is not None and reading.value is not None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._cuid)},
            name=f"ista NL {self._cuid[:8]}",
            manufacturer=MANUFACTURER,
            model="mijn.ista.nl",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://mijn.ista.nl",
        )

    @property
    def native_value(self) -> float | None:
        reading = self._reading
        return reading.value if reading else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        reading = self._reading
        return _UNIT_MAP.get(reading.unit or "") if reading else None

    @property
    def device_class(self) -> SensorDeviceClass | None:
        reading = self._reading
        return _DEVICE_CLASS_MAP.get(reading.unit or "") if reading else None

    @property
    def state_class(self) -> SensorStateClass | None:
        return (
            SensorStateClass.TOTAL_INCREASING if self.device_class is not None else None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reading = self._reading
        if reading is None:
            return {}
        attrs: dict[str, Any] = {
            "meter_id": reading.meter_id,
            "serial_nr": reading.serial_nr,
            "meter_type": reading.meter_type,
        }
        if reading.reading_date is not None:
            attrs["reading_date"] = reading.reading_date
        return attrs


def _build_sensors(
    coordinator: MijnIstaCoordinator,
    cuid: str,
    customer: CustomerData,
    *,
    known_ids: set[str] | None = None,
) -> list[MijnIstaSensor]:
    """Build one raw sensor per meter, never service or period totals."""
    services = {s.id: s for s in customer.services}
    sensors: list[MijnIstaSensor] = []
    for meter in customer.meters.values():
        if known_ids is not None and _unique_id(cuid, meter) in known_ids:
            continue
        service = services.get(meter.service_id)
        label = (
            _translate_service(service.description, coordinator.hass.config.language)
            if service
            else f"Service {meter.service_id}"
        )
        sensors.append(MijnIstaSensor(coordinator, cuid, meter, label))
    return sensors


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up raw sensors, including meters first appearing after a partial response."""
    coordinator: MijnIstaCoordinator = config_entry.runtime_data
    known_ids: set[str] = set()

    @callback
    def add_new_meters() -> None:
        entities: list[MijnIstaSensor] = []
        for cuid, customer in (coordinator.data or {}).items():
            entities.extend(
                _build_sensors(coordinator, cuid, customer, known_ids=known_ids)
            )
        if entities:
            known_ids.update(e.unique_id for e in entities if e.unique_id is not None)
            async_add_entities(entities)

    add_new_meters()
    config_entry.async_on_unload(coordinator.async_add_listener(add_new_meters))
    _remove_stale_sensors(hass, config_entry)


def _remove_stale_sensors(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Retire only known derived entity families, never absent raw meters."""
    registry = async_get_entity_registry(hass)
    for entry in async_entries_for_config_entry(registry, config_entry.entry_id):
        if (
            entry.domain == "sensor"
            and entry.platform == DOMAIN
            and _OBSOLETE_SENSOR.fullmatch(entry.unique_id)
        ):
            _LOGGER.info("Removing retired derived sensor %s", entry.entity_id)
            registry.async_remove(entry.entity_id)
