"""Shared runtime state for Wyze sprinkler controllers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from time import time
from typing import Any

from wyzeapy import Wyzeapy
from wyzeapy.exceptions import AccessTokenError, LoginError
from wyzeapy.services.irrigation_service import Irrigation, IrrigationService

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CLIENT,
    CONF_IRRIGATION_COORDINATORS,
    CONF_IRRIGATION_SETUP_LOCK,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=30)
DEVICE_INFO_INTERVAL = timedelta(minutes=5)
SCHEDULE_RUNS_URL = (
    "https://wyze-lockwood-service.wyzecam.com/plugin/irrigation/schedule_runs"
)
DEVICE_INFO_URL = (
    "https://wyze-lockwood-service.wyzecam.com/plugin/irrigation/device_info"
)
PROGRAM_MODE_NONE = "none"
PROGRAM_MODE_SCHEDULED = "scheduled"
PROGRAM_MODE_MANUAL = "manual"


@dataclass
class WyzeIrrigationRuntimeData:
    """Latest state shared by all zones on an irrigation controller."""

    device: Irrigation
    running_zone_number: int | None
    running_zone_name: str | None = None
    run_start: datetime | None = None
    run_end: datetime | None = None
    schedule_name: str | None = None
    schedule_type: str | None = None
    schedules_enabled: bool | None = None
    program_mode: str | None = None


def _timestamp(value: Any) -> int | None:
    """Return a valid Unix timestamp from an API value."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    """Return a normalized boolean from an API value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.casefold()
        if normalized in {"1", "true", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "off", "disabled"}:
            return False
    return None


def parse_schedules_enabled(response: Mapping[str, Any]) -> bool | None:
    """Extract the schedule-enabled setting from a device-info response."""
    data = response.get("data", {})
    properties = data.get("props", data)
    if not isinstance(properties, Mapping):
        return None
    return _boolean(properties.get("enable_schedules"))


def derive_program_mode(
    schedule_type: str | None, schedules_enabled: bool | None
) -> str | None:
    """Translate Wyze schedule state to HomeKit-aligned program modes."""
    if schedule_type and schedule_type.casefold() == PROGRAM_MODE_MANUAL:
        return PROGRAM_MODE_MANUAL
    if schedule_type or schedules_enabled:
        return PROGRAM_MODE_SCHEDULED
    if schedules_enabled is False:
        return PROGRAM_MODE_NONE
    return None


def parse_running_schedule(
    response: Mapping[str, Any], now_timestamp: int | None = None
) -> dict[str, Any]:
    """Normalize the currently running zone from a schedule-runs response."""
    now_timestamp = int(time()) if now_timestamp is None else now_timestamp
    schedules = response.get("data", {}).get("schedules", [])

    for schedule in schedules:
        if schedule.get("schedule_state") != "running":
            continue

        zone_runs = schedule.get("zone_runs") or []
        if not zone_runs:
            return {"running": True}

        current_run = next(
            (
                zone_run
                for zone_run in zone_runs
                if (start := _timestamp(zone_run.get("start_ts"))) is not None
                and start <= now_timestamp
                and (
                    (end := _timestamp(zone_run.get("end_ts"))) is None
                    or now_timestamp < end
                )
            ),
            zone_runs[0],
        )

        return {
            "running": True,
            "zone_number": current_run.get("zone_number"),
            "zone_name": current_run.get("zone_name"),
            "start_ts": _timestamp(current_run.get("start_ts")),
            "end_ts": _timestamp(current_run.get("end_ts")),
            "schedule_name": schedule.get("schedule_name"),
            "schedule_type": current_run.get(
                "schedule_type", schedule.get("schedule_type")
            ),
        }

    return {"running": False}


class WyzeIrrigationCoordinator(DataUpdateCoordinator[WyzeIrrigationRuntimeData]):
    """Coordinate one status request for all zones on a controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        irrigation_service: IrrigationService,
        device: Irrigation,
    ) -> None:
        """Initialize the irrigation coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Wyze irrigation {device.mac}",
            update_interval=UPDATE_INTERVAL,
        )
        self.irrigation_service = irrigation_service
        self.device = device
        self.command_lock = asyncio.Lock()
        self._schedules_enabled: bool | None = None
        self._device_info_refresh_at = 0.0

    async def _async_get_schedule(self) -> dict[str, Any]:
        """Fetch the raw schedule response so timestamps are not discarded."""
        raw_getter = getattr(self.irrigation_service, "_get_schedule_runs", None)
        if raw_getter is None:
            return await self.irrigation_service.get_schedule_runs(self.device)

        response = await raw_getter(SCHEDULE_RUNS_URL, self.device, limit=2)
        schedule = parse_running_schedule(response)
        _LOGGER.debug(
            "Wyze sprinkler %s active run: zone=%s start=%s end=%s type=%s",
            self.device.mac,
            schedule.get("zone_number"),
            schedule.get("start_ts"),
            schedule.get("end_ts"),
            schedule.get("schedule_type"),
        )
        return schedule

    async def _async_get_schedules_enabled(self) -> bool | None:
        """Return the cached controller schedule-enabled setting."""
        now = time()
        if now < self._device_info_refresh_at:
            return self._schedules_enabled

        raw_getter = getattr(self.irrigation_service, "_get_iot_prop", None)
        if raw_getter is None:
            return self._schedules_enabled

        try:
            response = await raw_getter(
                DEVICE_INFO_URL, self.device, "enable_schedules"
            )
        except (AccessTokenError, LoginError):
            raise
        except Exception as err:
            self._device_info_refresh_at = now + DEVICE_INFO_INTERVAL.total_seconds()
            _LOGGER.warning(
                "Unable to fetch schedule state for Wyze sprinkler %s: %s",
                self.device.mac,
                err,
            )
            return self._schedules_enabled

        self._schedules_enabled = parse_schedules_enabled(response)
        self._device_info_refresh_at = now + DEVICE_INFO_INTERVAL.total_seconds()
        _LOGGER.debug(
            "Wyze sprinkler %s schedules enabled: %s",
            self.device.mac,
            self._schedules_enabled,
        )
        return self._schedules_enabled

    async def _async_update_data(self) -> WyzeIrrigationRuntimeData:
        """Fetch controller, zone, and running-schedule state."""
        try:
            self.device = await self.irrigation_service.update(self.device)
            schedule = await self._async_get_schedule()
            schedules_enabled = await self._async_get_schedules_enabled()
        except (AccessTokenError, LoginError) as err:
            raise ConfigEntryAuthFailed(
                "Unable to authenticate with Wyze; please reauthenticate"
            ) from err
        except Exception as err:
            raise UpdateFailed(
                f"Unable to update Wyze sprinkler {self.device.nickname}: {err}"
            ) from err

        schedule_type = schedule.get("schedule_type")
        program_mode = derive_program_mode(schedule_type, schedules_enabled)
        _LOGGER.debug(
            "Wyze sprinkler %s program mode: %s",
            self.device.mac,
            program_mode,
        )

        if not schedule.get("running"):
            return WyzeIrrigationRuntimeData(
                device=self.device,
                running_zone_number=None,
                schedules_enabled=schedules_enabled,
                program_mode=program_mode,
            )

        start_ts = schedule.get("start_ts")
        end_ts = schedule.get("end_ts")
        return WyzeIrrigationRuntimeData(
            device=self.device,
            running_zone_number=schedule.get("zone_number"),
            running_zone_name=schedule.get("zone_name"),
            run_start=(
                dt_util.utc_from_timestamp(start_ts) if start_ts is not None else None
            ),
            run_end=dt_util.utc_from_timestamp(end_ts) if end_ts is not None else None,
            schedule_name=schedule.get("schedule_name"),
            schedule_type=schedule_type,
            schedules_enabled=schedules_enabled,
            program_mode=program_mode,
        )

    def set_running_zone(
        self, zone_number: int | None, duration: int | None = None
    ) -> None:
        """Optimistically update the active zone after a successful command."""
        if zone_number is None:
            schedules_enabled = (
                self.data.schedules_enabled if self.data is not None else None
            )
            self.async_set_updated_data(
                WyzeIrrigationRuntimeData(
                    device=self.device,
                    running_zone_number=None,
                    schedules_enabled=schedules_enabled,
                    program_mode=derive_program_mode(None, schedules_enabled),
                )
            )
            return

        schedules_enabled = (
            self.data.schedules_enabled if self.data is not None else None
        )
        run_start = dt_util.utcnow()
        run_end = (
            run_start + timedelta(seconds=duration) if duration is not None else None
        )
        self.async_set_updated_data(
            WyzeIrrigationRuntimeData(
                device=self.device,
                running_zone_number=zone_number,
                run_start=run_start,
                run_end=run_end,
                schedule_type="MANUAL",
                schedules_enabled=schedules_enabled,
                program_mode=PROGRAM_MODE_MANUAL,
            )
        )


async def async_get_irrigation_coordinators(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, WyzeIrrigationCoordinator]:
    """Return shared sprinkler coordinators for a config entry."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    coordinators = entry_data.setdefault(CONF_IRRIGATION_COORDINATORS, {})
    lock = entry_data.setdefault(CONF_IRRIGATION_SETUP_LOCK, asyncio.Lock())

    async with lock:
        if coordinators:
            return coordinators

        client: Wyzeapy = entry_data[CONF_CLIENT]
        irrigation_service = await client.irrigation_service
        for device in await irrigation_service.get_irrigations():
            coordinator = WyzeIrrigationCoordinator(hass, irrigation_service, device)
            await coordinator.async_config_entry_first_refresh()
            coordinators[device.mac] = coordinator

    return coordinators
