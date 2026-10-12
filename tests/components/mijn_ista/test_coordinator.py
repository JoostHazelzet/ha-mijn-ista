"""Tests for raw meter normalization and coordinator resilience."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from mijn_ista_api import MijnIstaAuthError, MijnIstaConnectionError

from custom_components.mijn_ista.const import CONF_UPDATE_INTERVAL
from custom_components.mijn_ista.coordinator import (
    MijnIstaCoordinator,
    _parse_customer,
    _reading_date,
    login_budget,
)

from .conftest import MOCK_AVG_VALUES, MOCK_MONTH_VALUES, MOCK_USER_VALUES


@pytest.fixture(autouse=True)
def current_month(freezer):
    freezer.move_to("2024-11-15T12:00:00+00:00")


@pytest.fixture
def raw_customer():
    return deepcopy(MOCK_USER_VALUES["Cus"][0])


def test_raw_register_not_consumption(raw_customer):
    meter = raw_customer["curConsumption"]["ServicesComp"][0]["CurMeters"][0]
    meter.update(BeginValue=31.286, EndValue=31.335, CValue=0.049, DecPos=3)
    result = _parse_customer(raw_customer)
    assert result.meters[(1, 101)].value == 31.335
    assert result.meters[(1, 101)].unit == "Gigajoule"
    assert len(result.meters) == 2
    assert result.address == "Teststraat 1"
    assert result.consumption[1].total == 42.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("07-10-2026", "2026-10-07"),
        ("2026-10-07", "2026-10-07"),
        ("2026-10-07T23:00:00+02:00", "2026-10-07"),
        ("2026-10-07T00:00:00Z", "2026-10-07"),
        ("2024-02-29", "2024-02-29"),
        ("", None),
        (None, None),
        ("31-02-2026", None),
        ("not-a-date", None),
        (False, None),
    ],
)
def test_reading_dates(raw, expected):
    assert _reading_date(raw) == expected


@pytest.mark.parametrize(
    "value", [None, True, "42.5", float("nan"), float("inf"), -1, 10**400]
)
def test_invalid_register_never_becomes_zero(raw_customer, value, caplog):
    raw_customer["curConsumption"]["ServicesComp"][0]["CurMeters"][0]["EndValue"] = (
        value
    )
    result = _parse_customer(raw_customer)
    assert result.meters[(1, 101)].value is None
    assert result.meters[(2, 201)].value == 18
    assert "missing or invalid EndValue" in caplog.text


def test_missing_register_never_becomes_zero(raw_customer):
    del raw_customer["curConsumption"]["ServicesComp"][0]["CurMeters"][0]["EndValue"]
    assert _parse_customer(raw_customer).meters[(1, 101)].value is None


def test_zero_is_valid(raw_customer):
    raw_customer["curConsumption"]["ServicesComp"][0]["CurMeters"][0]["EndValue"] = 0
    assert _parse_customer(raw_customer).meters[(1, 101)].value == 0


@pytest.mark.parametrize("meter_type", ["HCA", "unknown", ""])
def test_unverified_meters_have_no_inferred_unit(raw_customer, meter_type):
    raw_customer["curConsumption"]["Billingservices"][0]["MeterType"] = meter_type
    reading = _parse_customer(raw_customer).meters[(1, 101)]
    assert reading.value == 42.5
    assert reading.unit is None


def test_missing_metadata_preserves_register(raw_customer):
    raw_customer["curConsumption"]["Billingservices"] = []
    result = _parse_customer(raw_customer)
    assert result.meters[(1, 101)].value == 42.5
    assert result.meters[(1, 101)].unit is None


def test_comparisons_ignored(raw_customer):
    raw_customer["curConsumption"]["ServicesComp"][0].update(
        TotalNow="invalid", TotalDiffperc=None, CompMeters="invalid"
    )
    assert len(_parse_customer(raw_customer).meters) == 2


def test_empty_current_meters(raw_customer):
    raw_customer["curConsumption"]["ServicesComp"] = None
    assert _parse_customer(raw_customer).meters == {}


def test_duplicate_meter_rejected(raw_customer):
    meters = raw_customer["curConsumption"]["ServicesComp"][0]["CurMeters"]
    meters.append(deepcopy(meters[0]))
    with pytest.raises(ValueError, match="duplicate"):
        _parse_customer(raw_customer)


def test_serial_string_keeps_leading_zeroes(raw_customer):
    raw_customer["curConsumption"]["ServicesComp"][0]["CurMeters"][0]["MeterNr"] = (
        "00123"
    )
    assert _parse_customer(raw_customer).meters[(1, 101)].serial_nr == "00123"


@pytest.fixture
def mock_api():
    api = MagicMock()
    api.authenticate = AsyncMock()
    api.get_user_values = AsyncMock(return_value=deepcopy(MOCK_USER_VALUES))
    api.get_month_values = AsyncMock(return_value=deepcopy(MOCK_MONTH_VALUES))
    api.get_consumption_averages = AsyncMock(return_value=deepcopy(MOCK_AVG_VALUES))
    api.get_consumption_values = AsyncMock(return_value={"ServicesComp": []})
    return api


@pytest.fixture
def mock_entry():
    entry = MagicMock()
    entry.options = {CONF_UPDATE_INTERVAL: 6}
    entry.entry_id = "test-entry-id"
    return entry


async def test_all_datasets_requested(hass, mock_api, mock_entry):
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api)
    for _ in range(2):
        result = await coord._async_update_data()
        assert len(result["test-cuid-abc123"].meters) == 2
    assert mock_api.get_user_values.await_count == 2
    assert mock_api.get_month_values.await_count == 2
    mock_api.get_month_values.assert_any_await("test-cuid-abc123", quick=True)
    mock_api.get_month_values.assert_any_await("test-cuid-abc123", quick=False)
    assert mock_api.get_consumption_averages.await_count == 2
    mock_api.get_consumption_averages.assert_awaited_with(
        "test-cuid-abc123", "2024-01-01", "2024-12-31"
    )
    assert mock_api.get_consumption_values.await_count == 2


async def test_multiple_properties(hass, mock_api, mock_entry):
    second = deepcopy(MOCK_USER_VALUES["Cus"][0])
    second["Cuid"] = "second-cuid"
    mock_api.get_user_values.return_value["Cus"].append(second)
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api)
    result = await coord._async_update_data()
    assert set(result) == {"test-cuid-abc123", "second-cuid"}
    mock_api.get_user_values.assert_awaited_once()
    assert mock_api.get_month_values.await_count == 2
    assert mock_api.get_consumption_values.await_count == 2
    assert mock_api.get_consumption_averages.await_count == 2


async def test_auth_failure(hass, mock_api, mock_entry):
    mock_api.authenticate.side_effect = MijnIstaAuthError("bad credentials")
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_failure_shortens_interval_and_success_restores_it(
    hass, mock_api, mock_entry
):
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api)
    mock_api.get_user_values.side_effect = MijnIstaConnectionError("down")
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord.update_interval == timedelta(minutes=30)
    mock_api.get_user_values.side_effect = None
    await coord._async_update_data()
    assert coord.update_interval == timedelta(hours=6)


@pytest.mark.parametrize(
    "payload", [{}, {"Cus": None}, {"Cus": [{}]}, {"Cus": "invalid"}]
)
async def test_malformed_payload_is_explicit_failure(
    hass, mock_api, mock_entry, payload
):
    mock_api.get_user_values.return_value = payload
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api)
    with pytest.raises(UpdateFailed, match="Invalid mijn.ista.nl payload"):
        await coord._async_update_data()


async def test_empty_properties(hass, mock_api, mock_entry):
    mock_api.get_user_values.return_value = {"Cus": []}
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api)
    assert await coord._async_update_data() == {}


async def test_first_refresh_login_policy(hass, mock_api, mock_entry):
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api)
    await coord._async_update_data()
    assert mock_api.login_attempts == 1
    assert mock_api.login_max_wait == 0
    await coord._async_update_data()
    assert mock_api.login_attempts == 3
    assert mock_api.login_max_wait is None


async def test_first_refresh_policy_survives_failure(hass, mock_api, mock_entry):
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api)
    mock_api.authenticate.side_effect = MijnIstaConnectionError("rate limited")
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    mock_api.authenticate.side_effect = None
    await coord._async_update_data()
    assert mock_api.login_attempts == 1
    assert mock_api.login_max_wait == 0


async def test_handed_over_session_skips_first_login_only(hass, mock_api, mock_entry):
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api, authenticated=True)
    await coord._async_update_data()
    mock_api.authenticate.assert_not_awaited()
    await coord._async_update_data()
    mock_api.authenticate.assert_awaited_once()


async def test_timeout_is_update_failed(hass, mock_api, mock_entry):
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api)
    mock_api.get_user_values.side_effect = TimeoutError
    with pytest.raises(UpdateFailed, match="timed out"):
        await coord._async_update_data()


async def test_timeout_covers_401_retry(hass, mock_api, mock_entry):
    coord = MijnIstaCoordinator(hass, mock_entry, mock_api, authenticated=True)
    with patch("custom_components.mijn_ista.coordinator.asyncio.timeout") as timeout:
        await coord._async_update_data()
    assert timeout.call_args_list[0].args == (210,)
    assert timeout.call_count == 4


def test_login_budget():
    assert login_budget(0, waits=True) == 0
    assert login_budget(1, waits=False) == 90
    assert login_budget(3, waits=True) == 3 * (120 + 90)
