"""Tests for Wyze sprinkler zone valve entities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp.client_exceptions import ClientConnectionError
from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.wyzeapi import PLATFORMS
from custom_components.wyzeapi.irrigation import WyzeIrrigationRuntimeData
from custom_components.wyzeapi.valve import (
    WyzeIrrigationZoneValve,
)


@pytest.fixture
def irrigation() -> SimpleNamespace:
    """Return a representative sprinkler controller."""
    return SimpleNamespace(
        mac="AA:BB:CC:DD:EE:FF",
        nickname="Backyard Sprinkler",
        product_model="BS_WK1",
        sn="SPRINKLER123",
        available=True,
    )


@pytest.fixture
def zone() -> SimpleNamespace:
    """Return a representative sprinkler zone."""
    return SimpleNamespace(
        zone_number=2,
        name="Back Lawn",
        enabled=True,
        zone_id="zone-2",
        quickrun_duration=600,
    )


@pytest.fixture
def coordinator(
    irrigation: SimpleNamespace,
) -> Mock:
    """Return a mocked irrigation coordinator."""
    coordinator = Mock()
    coordinator.device = irrigation
    coordinator.data = WyzeIrrigationRuntimeData(irrigation, None)
    coordinator.last_update_success = True

    async def async_start_zone(zone_number: int, duration: int) -> None:
        coordinator.data = WyzeIrrigationRuntimeData(irrigation, zone_number)

    async def async_stop(expected_zone_number: int | None = None) -> None:
        if (
            expected_zone_number is not None
            and coordinator.data.running_zone_number != expected_zone_number
        ):
            return
        coordinator.data = WyzeIrrigationRuntimeData(irrigation, None)

    coordinator.async_start_zone = AsyncMock(side_effect=async_start_zone)
    coordinator.async_stop = AsyncMock(side_effect=async_stop)
    return coordinator


@pytest.fixture
def entity(coordinator: Mock, zone: SimpleNamespace) -> WyzeIrrigationZoneValve:
    """Return a sprinkler zone valve."""
    valve = WyzeIrrigationZoneValve(coordinator, zone)
    valve._quickrun_duration = Mock(return_value=900)
    return valve


def test_valve_platform_is_registered() -> None:
    """The integration loads the valve platform."""
    assert "valve" in PLATFORMS


def test_state_and_device_information(
    entity: WyzeIrrigationZoneValve,
    coordinator: Mock,
) -> None:
    """Valve state and metadata reflect the controller and active zone."""
    assert entity.device_class is ValveDeviceClass.WATER
    assert entity.supported_features == (
        ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    )
    assert entity.unique_id == "AA:BB:CC:DD:EE:FF-zone-2-valve"
    assert entity.name == "Back Lawn"
    assert entity.is_closed is True
    assert entity.available is True
    assert entity.extra_state_attributes == {
        "zone_number": 2,
        "zone_id": "zone-2",
        "enabled": True,
    }

    coordinator.data = WyzeIrrigationRuntimeData(coordinator.device, 2)
    assert entity.is_closed is False

    coordinator.data = WyzeIrrigationRuntimeData(coordinator.device, 1)
    assert entity.is_closed is True


@pytest.mark.asyncio
async def test_open_valve_uses_configured_duration(
    entity: WyzeIrrigationZoneValve,
    coordinator: Mock,
) -> None:
    """Opening a zone starts it for the configured quick-run duration."""
    await entity.async_open_valve()

    coordinator.async_start_zone.assert_awaited_once_with(2, 900)
    assert entity.is_closed is False


@pytest.mark.asyncio
async def test_open_valve_delegates_when_another_zone_is_running(
    entity: WyzeIrrigationZoneValve,
    coordinator: Mock,
) -> None:
    """The shared coordinator enforces the controller's single-zone limit."""
    coordinator.data = WyzeIrrigationRuntimeData(coordinator.device, 1)

    await entity.async_open_valve()

    coordinator.async_start_zone.assert_awaited_once_with(2, 900)
    assert entity.is_closed is False


@pytest.mark.asyncio
async def test_close_active_valve_uses_global_stop(
    entity: WyzeIrrigationZoneValve,
    coordinator: Mock,
) -> None:
    """Closing the active zone uses the controller's global stop operation."""
    coordinator.data = WyzeIrrigationRuntimeData(coordinator.device, 2)

    await entity.async_close_valve()

    coordinator.async_stop.assert_awaited_once_with(2)
    assert entity.is_closed is True


@pytest.mark.asyncio
async def test_close_inactive_valve_is_a_noop(
    entity: WyzeIrrigationZoneValve,
    coordinator: Mock,
) -> None:
    """Closing an inactive zone does not stop the active zone."""
    coordinator.data = WyzeIrrigationRuntimeData(coordinator.device, 1)

    await entity.async_close_valve()

    coordinator.async_stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_failure_preserves_confirmed_state(
    entity: WyzeIrrigationZoneValve,
    coordinator: Mock,
) -> None:
    """A failed start does not publish optimistic running state."""
    coordinator.async_start_zone.side_effect = ClientConnectionError("offline")

    with pytest.raises(HomeAssistantError):
        await entity.async_open_valve()

    assert entity.is_closed is True
