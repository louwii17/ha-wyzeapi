"""Tests for shared Wyze sprinkler runtime data."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.components.sensor import SensorDeviceClass
import pytest
from wyzeapy.services.irrigation_service import (
    IrrigationControllerInfo,
    IrrigationRun,
)

from custom_components.wyzeapi.irrigation import (
    PROGRAM_MODE_MANUAL,
    PROGRAM_MODE_NONE,
    PROGRAM_MODE_SCHEDULED,
    WyzeIrrigationCoordinator,
    WyzeIrrigationRuntimeData,
    async_get_irrigation_coordinators,
    derive_program_mode,
)
from custom_components.wyzeapi.const import (
    CONF_CLIENT,
    CONF_IRRIGATION_COORDINATORS,
    DOMAIN,
)
from custom_components.wyzeapi.sensor import (
    WyzeIrrigationProgramMode,
    WyzeIrrigationZoneEndTime,
)


def test_derive_program_mode_matches_homekit_semantics() -> None:
    """Manual runs override the configured automatic schedule state."""
    assert derive_program_mode("MANUAL", True) == PROGRAM_MODE_MANUAL
    assert derive_program_mode("FIXED", False) == PROGRAM_MODE_SCHEDULED
    assert derive_program_mode(None, True) == PROGRAM_MODE_SCHEDULED
    assert derive_program_mode(None, False) == PROGRAM_MODE_NONE
    assert derive_program_mode(None, None) is None


def test_zone_end_time_sensor_only_reports_for_active_zone() -> None:
    """Only the active zone exposes the shared run end timestamp."""
    end_time = datetime(2026, 7, 23, 18, 30, tzinfo=UTC)
    device = SimpleNamespace(
        mac="AA:BB:CC:DD:EE:FF",
        nickname="Backyard Sprinkler",
        product_model="BS_WK1",
        sn="SPRINKLER123",
        available=True,
    )
    zone = SimpleNamespace(zone_number=2, name="Back Lawn")
    coordinator = Mock()
    coordinator.device = device
    coordinator.last_update_success = True
    coordinator.data = WyzeIrrigationRuntimeData(
        device=device,
        running_zone_number=2,
        run_end=end_time,
    )

    sensor = WyzeIrrigationZoneEndTime(coordinator, zone)

    assert sensor.device_class is SensorDeviceClass.TIMESTAMP
    assert sensor.unique_id == "AA:BB:CC:DD:EE:FF-zone-2-end-time"
    assert sensor.native_value == end_time
    assert sensor.available is True

    coordinator.data = WyzeIrrigationRuntimeData(device, 1, run_end=end_time)
    assert sensor.native_value is None


def test_program_mode_sensor_reports_coordinator_state() -> None:
    """The program-mode sensor exposes normalized coordinator state."""
    device = SimpleNamespace(
        mac="AA:BB:CC:DD:EE:FF",
        nickname="Backyard Sprinkler",
        product_model="BS_WK1",
        sn="SPRINKLER123",
        available=True,
    )
    coordinator = Mock()
    coordinator.device = device
    coordinator.data = WyzeIrrigationRuntimeData(
        device=device,
        running_zone_number=None,
        schedules_enabled=True,
        program_mode=PROGRAM_MODE_SCHEDULED,
    )

    sensor = WyzeIrrigationProgramMode(coordinator)

    assert sensor.device_class is SensorDeviceClass.ENUM
    assert sensor.unique_id == "AA:BB:CC:DD:EE:FF-program-mode"
    assert sensor.options == [
        PROGRAM_MODE_NONE,
        PROGRAM_MODE_SCHEDULED,
        PROGRAM_MODE_MANUAL,
    ]
    assert sensor.native_value == PROGRAM_MODE_SCHEDULED
    assert sensor.extra_state_attributes == {
        "schedules_enabled": True,
        "schedule_type": None,
    }

    coordinator.data.program_mode = PROGRAM_MODE_MANUAL
    coordinator.data.schedule_type = "MANUAL"
    assert sensor.native_value == PROGRAM_MODE_MANUAL


def _coordinator() -> tuple[
    WyzeIrrigationCoordinator,
    SimpleNamespace,
    SimpleNamespace,
]:
    """Create a coordinator with a representative service and controller."""
    device = SimpleNamespace(
        mac="AA:BB:CC:DD:EE:FF",
        nickname="Backyard Sprinkler",
        product_model="BS_WK1",
        sn="SPRINKLER123",
        available=True,
    )
    service = SimpleNamespace(
        update=AsyncMock(return_value=device),
        get_current_run=AsyncMock(return_value=None),
        get_controller_info=AsyncMock(
            return_value=IrrigationControllerInfo(schedules_enabled=True)
        ),
        start_zone=AsyncMock(),
        stop_running_schedule=AsyncMock(),
    )
    config_entry = Mock()
    config_entry.async_on_unload = Mock()
    coordinator = WyzeIrrigationCoordinator(
        Mock(),
        config_entry,
        service,
        device,
    )
    coordinator.data = WyzeIrrigationRuntimeData(device, None)
    return coordinator, service, config_entry


@pytest.mark.asyncio
async def test_coordinator_maps_typed_runtime_data() -> None:
    """Typed wyzeapy runtime data is converted to HA-aware values."""
    coordinator, service, config_entry = _coordinator()
    service.get_current_run.return_value = IrrigationRun(
        zone_number=2,
        zone_name="Back Lawn",
        start_ts=1_800_000_000,
        end_ts=1_800_000_600,
        schedule_name="Morning Watering",
        schedule_type="FIXED",
    )

    data = await coordinator._async_update_data()

    assert coordinator.config_entry is config_entry
    assert data.running_zone_number == 2
    assert data.running_zone_name == "Back Lawn"
    assert data.run_start == datetime.fromtimestamp(1_800_000_000, UTC)
    assert data.run_end == datetime.fromtimestamp(1_800_000_600, UTC)
    assert data.schedule_name == "Morning Watering"
    assert data.schedule_type == "FIXED"
    assert data.schedules_enabled is True
    assert data.program_mode == PROGRAM_MODE_SCHEDULED


@pytest.mark.asyncio
async def test_controller_info_is_cached() -> None:
    """Controller configuration is not fetched on every 30-second update."""
    coordinator, service, _ = _coordinator()

    first = await coordinator._async_update_data()
    coordinator.data = first
    second = await coordinator._async_update_data()

    assert second.schedules_enabled is True
    service.get_controller_info.assert_awaited_once()
    assert service.get_current_run.await_count == 2


@pytest.mark.asyncio
async def test_coordinator_switches_zones_under_one_command_lock() -> None:
    """Starting another zone stops the active run before starting the next."""
    coordinator, service, _ = _coordinator()
    coordinator.data = WyzeIrrigationRuntimeData(coordinator.device, 1)
    calls: list[str] = []
    service.stop_running_schedule.side_effect = lambda *_: calls.append("stop")
    service.start_zone.side_effect = lambda *_: calls.append("start")

    await coordinator.async_start_zone(2, 900)

    assert calls == ["stop", "start"]
    service.stop_running_schedule.assert_awaited_once_with(coordinator.device)
    service.start_zone.assert_awaited_once_with(coordinator.device, 2, 900)
    assert coordinator.data.running_zone_number == 2
    assert coordinator.data.program_mode == PROGRAM_MODE_MANUAL


@pytest.mark.asyncio
async def test_coordinator_stop_publishes_idle_state() -> None:
    """The global stop command updates every zone through shared state."""
    coordinator, service, _ = _coordinator()
    coordinator.data = WyzeIrrigationRuntimeData(
        coordinator.device,
        2,
        schedules_enabled=True,
        program_mode=PROGRAM_MODE_MANUAL,
    )

    await coordinator.async_stop()

    service.stop_running_schedule.assert_awaited_once_with(coordinator.device)
    assert coordinator.data.running_zone_number is None
    assert coordinator.data.program_mode == PROGRAM_MODE_SCHEDULED


@pytest.mark.asyncio
async def test_zone_close_does_not_stop_a_newly_active_zone() -> None:
    """An expected-zone recheck protects a newer run after lock contention."""
    coordinator, service, _ = _coordinator()
    coordinator.data = WyzeIrrigationRuntimeData(coordinator.device, 1)

    await coordinator.async_stop(expected_zone_number=2)

    service.stop_running_schedule.assert_not_awaited()
    assert coordinator.data.running_zone_number == 1


@pytest.mark.asyncio
async def test_empty_coordinator_discovery_is_cached() -> None:
    """Platforms share an empty discovery result without repeated API calls."""
    service = SimpleNamespace(get_irrigations=AsyncMock(return_value=[]))
    service_future = asyncio.Future()
    service_future.set_result(service)
    config_entry = SimpleNamespace(entry_id="entry")
    entry_data = {CONF_CLIENT: SimpleNamespace(irrigation_service=service_future)}
    hass = SimpleNamespace(data={DOMAIN: {"entry": entry_data}})

    first = await async_get_irrigation_coordinators(hass, config_entry)
    second = await async_get_irrigation_coordinators(hass, config_entry)

    assert first == {}
    assert second is first
    service.get_irrigations.assert_awaited_once()


@pytest.mark.asyncio
async def test_coordinator_discovery_does_not_publish_partial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed controller refresh leaves discovery ready for a clean retry."""
    devices = [
        SimpleNamespace(mac="controller-1"),
        SimpleNamespace(mac="controller-2"),
    ]
    service = SimpleNamespace(get_irrigations=AsyncMock(return_value=devices))
    service_future = asyncio.Future()
    service_future.set_result(service)
    config_entry = SimpleNamespace(entry_id="entry")
    entry_data = {CONF_CLIENT: SimpleNamespace(irrigation_service=service_future)}
    hass = SimpleNamespace(data={DOMAIN: {"entry": entry_data}})

    class TestCoordinator:
        def __init__(self, hass, config_entry, irrigation_service, device) -> None:
            self.device = device

        async def async_config_entry_first_refresh(self) -> None:
            if self.device.mac == "controller-2":
                raise RuntimeError("refresh failed")

    monkeypatch.setattr(
        "custom_components.wyzeapi.irrigation.WyzeIrrigationCoordinator",
        TestCoordinator,
    )

    with pytest.raises(RuntimeError, match="refresh failed"):
        await async_get_irrigation_coordinators(hass, config_entry)

    assert CONF_IRRIGATION_COORDINATORS not in entry_data
