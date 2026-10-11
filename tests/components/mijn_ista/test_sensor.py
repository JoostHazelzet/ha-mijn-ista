"""Tests for raw meter sensors and retirement of derived entities."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mijn_ista.const import DOMAIN
from custom_components.mijn_ista.coordinator import MijnIstaCoordinator, _parse_customer
from custom_components.mijn_ista.sensor import (
    _build_sensors,
    _remove_stale_sensors,
    _translate_service,
    async_setup_entry,
)

from .conftest import MOCK_USER_VALUES

CUID = "test-cuid-abc123"


@pytest.fixture
def coordinator(hass):
    coord = MagicMock()
    coord.hass = hass
    coord.last_update_success = True
    coord.data = {CUID: _parse_customer(deepcopy(MOCK_USER_VALUES["Cus"][0]))}
    return coord


def sensors(coordinator):
    return _build_sensors(coordinator, CUID, coordinator.data[CUID])


@pytest.mark.parametrize(
    ("description", "language", "expected"),
    [
        ("Verwarming", "en", "Heating"),
        ("Warm water", "en-GB", "Hot water"),
        ("Verwarming", "nl", "Verwarming"),
        ("Verwarming", "de", "Verwarming"),
        ("Zonnepanelen", "en", "Zonnepanelen"),
    ],
)
def test_translation(description, language, expected):
    assert _translate_service(description, language) == expected


def test_one_sensor_per_meter_no_derived_sensors(coordinator):
    built = sensors(coordinator)
    assert len(built) == 2
    assert {s.unique_id for s in built} == {
        f"{DOMAIN}_{CUID}_svc1_dev101_reading",
        f"{DOMAIN}_{CUID}_svc2_dev201_reading",
    }


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", ["Heating 12345", "Hot water 67890"]),
        ("nl", ["Verwarming 12345", "Warm water 67890"]),
    ],
)
def test_sensor_names(coordinator, language, expected):
    coordinator.hass.config.language = language
    assert [s.name for s in sensors(coordinator)] == expected


def test_energy_and_water_metadata(coordinator):
    heating, water = sensors(coordinator)
    assert heating.device_class == SensorDeviceClass.ENERGY
    assert heating.native_unit_of_measurement == UnitOfEnergy.GIGA_JOULE
    assert water.device_class == SensorDeviceClass.WATER
    assert water.native_unit_of_measurement == UnitOfVolume.CUBIC_METERS
    for sensor in (heating, water):
        assert sensor.state_class == SensorStateClass.TOTAL_INCREASING
        assert sensor.last_reset is None


def test_kwh_metadata(coordinator):
    raw = deepcopy(MOCK_USER_VALUES["Cus"][0])
    raw["curConsumption"]["Billingservices"][0]["Unit"] = "kWh"
    coordinator.data[CUID] = _parse_customer(raw)
    heating = sensors(coordinator)[0]
    assert heating.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert heating.device_class == SensorDeviceClass.ENERGY
    assert heating.state_class == SensorStateClass.TOTAL_INCREASING


def test_unverified_type_remains_raw_and_unclassified(coordinator, caplog):
    raw = deepcopy(MOCK_USER_VALUES["Cus"][0])
    raw["curConsumption"]["Billingservices"][0]["MeterType"] = "HCA"
    coordinator.data[CUID] = _parse_customer(raw)
    sensor = sensors(coordinator)[0]
    assert sensor.native_value == 42.5
    assert sensor.native_unit_of_measurement is None
    assert sensor.device_class is None
    assert sensor.state_class is None
    assert "unverified register" in caplog.text


def test_statistics_metadata_tracks_partial_service_metadata(coordinator):
    sensor = sensors(coordinator)[0]
    raw = deepcopy(MOCK_USER_VALUES["Cus"][0])
    raw["curConsumption"]["Billingservices"] = []
    coordinator.data[CUID] = _parse_customer(raw)
    assert sensor.native_value == 42.5
    assert sensor.native_unit_of_measurement is None
    assert sensor.device_class is None
    assert sensor.state_class is None
    coordinator.data[CUID] = _parse_customer(MOCK_USER_VALUES["Cus"][0])
    assert sensor.device_class == SensorDeviceClass.ENERGY
    assert sensor.state_class == SensorStateClass.TOTAL_INCREASING


def test_raw_register_not_period_consumption(coordinator):
    raw = deepcopy(MOCK_USER_VALUES["Cus"][0])
    meter = raw["curConsumption"]["ServicesComp"][0]["CurMeters"][0]
    meter.update(BeginValue=31.286, EndValue=31.335, CValue=0.049, EsDate="07-10-2026")
    coordinator.data[CUID] = _parse_customer(raw)
    sensor = sensors(coordinator)[0]
    assert sensor.native_value == 31.335
    assert sensor.extra_state_attributes == {
        "meter_id": 101,
        "serial_nr": "12345",
        "meter_type": "ultego III smart - optosonic",
        "reading_date": "2026-10-07",
    }


def test_bill_rollover_does_not_reset_register(coordinator):
    raw = deepcopy(MOCK_USER_VALUES["Cus"][0])
    meter = raw["curConsumption"]["ServicesComp"][0]["CurMeters"][0]
    meter.update(BeginValue=31, EndValue=32, CValue=1)
    coordinator.data[CUID] = _parse_customer(raw)
    sensor = sensors(coordinator)[0]
    assert sensor.native_value == 32
    meter.update(BeginValue=32, EndValue=32.5, CValue=0.5)
    coordinator.data[CUID] = _parse_customer(raw)
    assert sensor.native_value == 32.5
    assert sensor.last_reset is None
    meter.update(EndValue=0.1)
    coordinator.data[CUID] = _parse_customer(raw)
    assert sensor.native_value == 0.1
    assert sensor.state_class == SensorStateClass.TOTAL_INCREASING


@pytest.mark.parametrize("data", [{}, None])
def test_missing_property_unavailable(coordinator, data):
    sensor = sensors(coordinator)[0]
    coordinator.data = data
    assert sensor.native_value is None
    assert not sensor.available
    assert sensor.extra_state_attributes == {}


def test_missing_meter_unavailable_and_recovers(coordinator):
    sensor = sensors(coordinator)[0]
    reading = coordinator.data[CUID].meters.pop((1, 101))
    assert not sensor.available
    assert sensor.native_value is None
    coordinator.data[CUID].meters[(1, 101)] = reading
    assert sensor.available


def test_invalid_value_unavailable(coordinator):
    raw = deepcopy(MOCK_USER_VALUES["Cus"][0])
    raw["curConsumption"]["ServicesComp"][0]["CurMeters"][0]["EndValue"] = None
    coordinator.data[CUID] = _parse_customer(raw)
    sensor = sensors(coordinator)[0]
    assert sensor.native_value is None
    assert not sensor.available


def test_failed_update_unavailable(coordinator):
    sensor = sensors(coordinator)[0]
    coordinator.last_update_success = False
    assert not sensor.available


@pytest.mark.parametrize(
    "suffix",
    [
        "svc1_annual_current",
        "svc1_annual_previous",
        "svc1_annual_diff_pct",
        "svc1_building_avg_annual",
        "svc1_month_latest",
        "svc1_month_building_avg",
        "svc1_dev101_annual",
        "svc1_dev101_month",
        "avg_temp_current_period",
        "avg_temp_previous_period",
    ],
)
async def test_retired_sensor_cleanup(hass, suffix):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    retired = registry.async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}_{CUID}_{suffix}", config_entry=entry
    )
    kept = registry.async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}_{CUID}_svc1_dev101_reading", config_entry=entry
    )
    other = registry.async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}_{CUID}_unrelated_month", config_entry=entry
    )
    _remove_stale_sensors(hass, entry)
    assert registry.async_get(retired.entity_id) is None
    assert registry.async_get(kept.entity_id) is not None
    assert registry.async_get(other.entity_id) is not None


async def test_cleanup_does_not_touch_other_config_entry(hass):
    own = MockConfigEntry(domain=DOMAIN, data={})
    other = MockConfigEntry(domain=DOMAIN, data={})
    own.add_to_hass(hass)
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    kept = registry.async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}_{CUID}_svc1_annual_current", config_entry=other
    )
    _remove_stale_sensors(hass, own)
    assert registry.async_get(kept.entity_id) is not None


async def test_late_meters_added_without_duplicates(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coord = MijnIstaCoordinator(hass, entry, MagicMock())
    entry.runtime_data = coord
    coord.async_set_updated_data({})
    add_entities = MagicMock()
    await async_setup_entry(hass, entry, add_entities)
    add_entities.assert_not_called()
    customer = _parse_customer(deepcopy(MOCK_USER_VALUES["Cus"][0]))
    coord.async_set_updated_data({CUID: customer})
    assert add_entities.call_count == 1
    assert len(add_entities.call_args.args[0]) == 2
    coord.async_set_updated_data({})
    coord.async_set_updated_data({CUID: customer})
    assert add_entities.call_count == 1


async def test_real_sensor_states_and_metadata(hass):
    """Validate published HA states, not just entity properties."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={"username": "test@example.com", "password": "secret"},
    )
    entry.add_to_hass(hass)
    raw = deepcopy(MOCK_USER_VALUES["Cus"][0])
    meter = raw["curConsumption"]["ServicesComp"][0]["CurMeters"][0]
    meter.update(BeginValue=31.286, EndValue=31.335, CValue=0.049, EsDate="07-10-2026")
    api = MagicMock()
    api.authenticate = AsyncMock()
    api.get_user_values = AsyncMock(return_value={"Cus": [raw]})
    with patch("custom_components.mijn_ista.ResilientMijnIstaAPI", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    states = hass.states.async_all("sensor")
    assert len(states) == 2
    heating = next(s for s in states if s.attributes.get("meter_id") == 101)
    assert float(heating.state) == 31.335
    assert heating.attributes["state_class"] == "total_increasing"
    assert heating.attributes["device_class"] == "energy"
    assert heating.attributes["unit_of_measurement"] == "GJ"
    assert heating.attributes["reading_date"] == "2026-10-07"
    assert "last_reset" not in heating.attributes
    assert "prior_months" not in heating.attributes
    assert await hass.config_entries.async_unload(entry.entry_id)
