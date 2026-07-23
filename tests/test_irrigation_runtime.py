"""Tests for shared Wyze sprinkler runtime data."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.wyzeapi.irrigation import (
    PROGRAM_MODE_MANUAL,
    PROGRAM_MODE_NONE,
    PROGRAM_MODE_SCHEDULED,
    WyzeIrrigationRuntimeData,
    derive_program_mode,
    parse_running_schedule,
    parse_schedules_enabled,
)
from custom_components.wyzeapi.sensor import (
    WyzeIrrigationProgramMode,
    WyzeIrrigationZoneEndTime,
)


def test_parse_running_schedule_selects_current_sequential_zone() -> None:
    """The active time interval wins instead of the first scheduled zone."""
    response = {
        "data": {
            "schedules": [
                {
                    "schedule_state": "running",
                    "schedule_name": "Morning",
                    "schedule_type": "FIXED",
                    "zone_runs": [
                        {
                            "zone_number": 1,
                            "zone_name": "Front",
                            "start_ts": 1_000,
                            "end_ts": 1_100,
                        },
                        {
                            "zone_number": 2,
                            "zone_name": "Back",
                            "start_ts": 1_100,
                            "end_ts": 1_200,
                        },
                    ],
                }
            ]
        }
    }

    assert parse_running_schedule(response, now_timestamp=1_150) == {
        "running": True,
        "zone_number": 2,
        "zone_name": "Back",
        "start_ts": 1_100,
        "end_ts": 1_200,
        "schedule_name": "Morning",
        "schedule_type": "FIXED",
    }


def test_parse_running_schedule_handles_idle_controller() -> None:
    """Past schedules do not make a zone active."""
    response = {
        "data": {
            "schedules": [
                {
                    "schedule_state": "past",
                    "zone_runs": [{"zone_number": 1}],
                }
            ]
        }
    }

    assert parse_running_schedule(response, now_timestamp=1_150) == {"running": False}


def test_parse_schedules_enabled_handles_device_info_shapes() -> None:
    """Schedule-enabled state is normalized from observed response shapes."""
    assert (
        parse_schedules_enabled({"data": {"props": {"enable_schedules": True}}}) is True
    )
    assert parse_schedules_enabled({"data": {"enable_schedules": "0"}}) is False
    assert (
        parse_schedules_enabled(
            {"data": {"properties": [{"key": "enable_schedules", "value": "enabled"}]}}
        )
        is True
    )
    assert (
        parse_schedules_enabled({"data": '{"props": {"enable_schedules": false}}'})
        is False
    )
    assert parse_schedules_enabled({"data": {}}) is None


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
