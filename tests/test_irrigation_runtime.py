"""Tests for shared Wyze sprinkler runtime data."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.wyzeapi.irrigation import (
    WyzeIrrigationRuntimeData,
    parse_running_schedule,
)
from custom_components.wyzeapi.sensor import WyzeIrrigationZoneEndTime


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
