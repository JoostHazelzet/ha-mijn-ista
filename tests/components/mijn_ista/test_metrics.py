"""Source-provided consumption and benchmark entity coverage."""

from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.helpers import entity_registry as er
from mijn_ista_api import MijnIstaAuthError, MijnIstaConnectionError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mijn_ista.const import DOMAIN
from custom_components.mijn_ista.coordinator import MijnIstaCoordinator
from custom_components.mijn_ista.sensor import _build_sensors

from .conftest import MOCK_USER_VALUES

CUID = "test-cuid-abc123"


@pytest.fixture
def datasets(freezer):
    freezer.move_to("2026-10-12T06:33:00Z")
    raw = deepcopy(MOCK_USER_VALUES["Cus"][0])
    cur = raw["curConsumption"]
    cold = deepcopy(cur["Billingservices"][1])
    cold.update(Id=8, Description="Koud water")
    cur["Billingservices"].append(cold)
    cur["Billingservices"][0]["Id"] = 6
    cur["BillingPeriods"] = [
        {"y": 2026, "s": "2025-07-01", "e": "2026-06-30"},
        {"y": 2027, "s": "2026-07-01", "e": "2027-06-30"},
    ]
    totals = []
    for sid, number, begin, end, total in (
        (2, 516003236, 0.562, 0.773, 0.211),
        (6, 822509484, 31.286, 31.335, 0.049),
        (8, 516003175, 3.094, 5.375, 2.281),
    ):
        totals.append(
            {
                "Id": sid,
                "TotalNow": total,
                "CurMeters": [
                    {
                        "MeterId": sid * 100,
                        "MeterNr": number,
                        "BeginValue": begin,
                        "EndValue": end,
                        "CValue": total,
                    }
                ],
            }
        )
    cur["ServicesComp"] = deepcopy(totals)
    # A separate endpoint supplies updated totals, not a sum of both responses.
    cur["ServicesComp"][0]["TotalNow"] = 0.2
    api = MagicMock()
    api.authenticate = AsyncMock()
    api.get_user_values = AsyncMock(return_value={"Cus": [raw]})
    api.get_consumption_values = AsyncMock(return_value={"ServicesComp": totals})
    api.get_consumption_averages = AsyncMock(
        return_value={"Averages": [{"BillingServiceId": 6, "NormalizedValue": 0.015}]}
    )
    api.get_month_values = AsyncMock(
        return_value={
            "ed": "2026-10-12T08:08:34.7522936+02:00",
            "mc": [
                {"y": 2026, "m": 9, "at": 99, "ServiceConsumptions": []},
                {
                    "y": 2026,
                    "m": 10,
                    "at": 13.4,
                    "ServiceConsumptions": [
                        {
                            "ServiceId": sid,
                            "TotalConsumption": value,
                            "BuldingAverage": average,
                            "HasApproximation": True,
                        }
                        for sid, value, average in (
                            (2, 0.018, 0.331),
                            (6, 0.107, 0.015),
                            (8, 0.283, 0.983),
                        )
                    ],
                },
            ],
        }
    )
    return api


async def make_coordinator(hass, api):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    coord = MijnIstaCoordinator(hass, entry, api)
    coord.async_set_updated_data(await coord._async_update_data())
    return coord


def metrics(coord):
    return {
        sensor.unique_id.removeprefix(f"{DOMAIN}_{CUID}_"): sensor
        for sensor in _build_sensors(coord, CUID, coord.data[CUID])
    }


async def test_requested_values_and_metadata(hass, datasets):
    coord = await make_coordinator(hass, datasets)
    built = metrics(coord)
    assert len(built) == 15
    for sid, total, monthly, average, number, begin, end, unit in (
        (2, 0.211, 0.018, 0.331, "516003236", 0.562, 0.773, "m³"),
        (6, 0.049, 0.107, 0.015, "822509484", 31.286, 31.335, "GJ"),
        (8, 2.281, 0.283, 0.983, "516003175", 3.094, 5.375, "m³"),
    ):
        sensor = built[f"svc{sid}_total"]
        assert sensor.native_value == total
        assert sensor.native_unit_of_measurement == unit
        assert sensor.device_class == (
            SensorDeviceClass.ENERGY if sid == 6 else SensorDeviceClass.WATER
        )
        assert sensor.state_class == SensorStateClass.TOTAL_INCREASING
        assert sensor.extra_state_attributes == {
            "meter_number": number,
            "begin_value": begin,
            "end_value": end,
        }
        month = built[f"svc{sid}_current_month"]
        assert month.native_value == monthly
        assert month.extra_state_attributes == {
            "month": "2026-10-01",
            "has_approximation": True,
        }
        assert month.state_class == SensorStateClass.TOTAL
        assert month.last_reset.date().isoformat() == "2026-10-01"
        assert built[f"svc{sid}_building_average"].native_value == average
    assert built["svc6_normalized"].native_value == 0.015
    assert built["outdoor_average_temperature"].native_value == 13.4
    assert (
        built["outdoor_average_temperature"].device_class
        == SensorDeviceClass.TEMPERATURE
    )
    assert built["outdoor_average_temperature"].native_unit_of_measurement == "°C"
    sync = built["last_sync"].native_value
    assert isinstance(sync, datetime)
    assert sync.utcoffset().total_seconds() == 7200
    datasets.get_consumption_values.assert_awaited_with(
        CUID, {"y": 2027, "s": "2026-07-01", "e": "2027-06-30"}
    )


async def test_stale_month_is_not_current(hass, datasets, freezer):
    coord = await make_coordinator(hass, datasets)
    built = metrics(coord)
    freezer.move_to("2026-11-01T12:00:00Z")
    assert not built["svc6_current_month"].available
    assert not built["svc6_building_average"].available
    assert not built["outdoor_average_temperature"].available
    coord.async_set_updated_data(await coord._async_update_data())
    assert not built["svc6_current_month"].available
    assert built["svc6_total"].available


@pytest.mark.parametrize("value", [None, True, -1, "0.015", float("nan"), float("inf")])
async def test_invalid_benchmark_is_unavailable(hass, datasets, value):
    datasets.get_consumption_averages.return_value["Averages"][0]["NormalizedValue"] = (
        value
    )
    built = metrics(await make_coordinator(hass, datasets))
    assert not built["svc6_normalized"].available
    assert built["svc6_total"].available


@pytest.mark.parametrize(
    "dataset", ["month_values", "consumption_values", "consumption_averages"]
)
async def test_optional_endpoint_failure_preserves_core(
    hass, datasets, dataset, caplog
):

    getattr(datasets, f"get_{dataset}").side_effect = MijnIstaConnectionError("offline")
    built = metrics(await make_coordinator(hass, datasets))
    assert built["svc6_dev600_reading"].available
    assert built["svc6_total"].available
    assert "data unavailable" in caplog.text


async def test_reauthentication_is_not_hidden(hass, datasets):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    datasets.get_month_values.side_effect = MijnIstaAuthError("invalid credentials")
    with pytest.raises(ConfigEntryAuthFailed):
        await make_coordinator(hass, datasets)


async def test_partial_response_replaces_old_metrics(hass, datasets):
    coord = await make_coordinator(hass, datasets)
    built = metrics(coord)
    datasets.get_month_values.return_value["mc"][1]["ServiceConsumptions"] = []
    datasets.get_consumption_averages.return_value["Averages"] = []
    coord.async_set_updated_data(await coord._async_update_data())
    assert not built["svc6_current_month"].available
    assert not built["svc6_building_average"].available
    assert not built["svc6_normalized"].available
    assert built["svc6_total"].available


async def test_revised_month_and_zero_values(hass, datasets):
    coord = await make_coordinator(hass, datasets)
    built = metrics(coord)
    datasets.get_month_values.return_value["mc"][1]["ServiceConsumptions"][1].update(
        TotalConsumption=0, BuldingAverage=0, HasApproximation=False
    )
    datasets.get_month_values.return_value["mc"][1]["at"] = 0
    coord.async_set_updated_data(await coord._async_update_data())
    assert built["svc6_current_month"].native_value == 0
    assert built["svc6_current_month"].available
    assert built["svc6_current_month"].state_class == SensorStateClass.TOTAL
    assert built["svc6_building_average"].native_value == 0
    assert built["outdoor_average_temperature"].native_value == 0


async def test_multiple_meters_do_not_double_count_service_total(hass, datasets):
    total = datasets.get_consumption_values.return_value["ServicesComp"][1]
    total["CurMeters"].append(deepcopy(total["CurMeters"][0]))
    built = metrics(await make_coordinator(hass, datasets))
    assert built["svc6_total"].native_value == 0.049
    assert len(built["svc6_total"].extra_state_attributes["meters"]) == 2
    assert "meter_number" not in built["svc6_total"].extra_state_attributes


async def test_single_meter_cvalue_fallback(hass, datasets):
    del datasets.get_consumption_values.return_value["ServicesComp"][1]["TotalNow"]
    built = metrics(await make_coordinator(hass, datasets))
    assert built["svc6_total"].native_value == 0.049


async def test_billing_rollover_and_empty_period_metadata(hass, datasets, caplog):
    coord = await make_coordinator(hass, datasets)
    built = metrics(coord)
    datasets.get_consumption_values.return_value["ServicesComp"][1]["TotalNow"] = 0
    coord.async_set_updated_data(await coord._async_update_data())
    assert built["svc6_total"].native_value == 0
    assert built["svc6_total"].state_class == SensorStateClass.TOTAL_INCREASING
    assert built["svc6_dev600_reading"].native_value == 31.335
    datasets.get_user_values.return_value["Cus"][0]["curConsumption"][
        "BillingPeriods"
    ] = "invalid"
    datasets.get_consumption_values.reset_mock()
    coord.async_set_updated_data(await coord._async_update_data())
    assert built["svc6_dev600_reading"].available
    datasets.get_consumption_values.assert_not_awaited()
    assert "billing period unavailable" in caplog.text


@pytest.mark.parametrize(
    "dataset", ["month_values", "consumption_values", "consumption_averages"]
)
async def test_empty_endpoint_response_is_reported(hass, datasets, dataset, caplog):
    getattr(datasets, f"get_{dataset}").return_value = {}
    built = metrics(await make_coordinator(hass, datasets))
    assert built["svc6_dev600_reading"].available
    assert "is missing" in caplog.text


async def test_malformed_month_does_not_publish_partial_success(hass, datasets):
    datasets.get_month_values.return_value["mc"][1]["ServiceConsumptions"].append(
        {"ServiceId": "invalid"}
    )
    built = metrics(await make_coordinator(hass, datasets))
    assert not built["svc2_current_month"].available
    assert not built["outdoor_average_temperature"].available


async def test_missing_knmi_and_timezone(hass, datasets):
    datasets.get_month_values.return_value["mc"][1]["at"] = None
    datasets.get_month_values.return_value["ed"] = "2026-10-12T08:08:34"
    built = metrics(await make_coordinator(hass, datasets))
    assert not built["outdoor_average_temperature"].available
    assert not built["last_sync"].available
    assert built["svc6_current_month"].available


@pytest.mark.parametrize(
    "language,total_name", [("en", "Heating total"), ("nl", "Verwarming totaal")]
)
async def test_published_ha_entities(hass, datasets, language, total_name):
    hass.config.language = language
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={"username": "test@example.com", "password": "secret"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.mijn_ista.ResilientMijnIstaAPI", return_value=datasets
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert len(hass.states.async_all("sensor")) == 15
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{CUID}_svc6_total"
    )
    state = hass.states.get(entity_id)
    assert state.state == "0.049"
    assert state.attributes["friendly_name"].endswith(total_name)
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total_increasing"
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{CUID}_last_sync"
    )
    state = hass.states.get(entity_id)
    assert state.attributes["device_class"] == "timestamp"
    assert state.state != "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)
