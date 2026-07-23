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
SCHEDULE_RUNS_URL = (
    "https://wyze-lockwood-service.wyzecam.com/plugin/irrigation/schedule_runs"
)


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


def _timestamp(value: Any) -> int | None:
    """Return a valid Unix timestamp from an API value."""
    try:
        return int(value)
    except (TypeError, ValueError):
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

    async def _async_update_data(self) -> WyzeIrrigationRuntimeData:
        """Fetch controller, zone, and running-schedule state."""
        try:
            self.device = await self.irrigation_service.update(self.device)
            schedule = await self._async_get_schedule()
        except (AccessTokenError, LoginError) as err:
            raise ConfigEntryAuthFailed(
                "Unable to authenticate with Wyze; please reauthenticate"
            ) from err
        except Exception as err:
            raise UpdateFailed(
                f"Unable to update Wyze sprinkler {self.device.nickname}: {err}"
            ) from err

        if not schedule.get("running"):
            return WyzeIrrigationRuntimeData(self.device, None)

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
            schedule_type=schedule.get("schedule_type"),
        )

    def set_running_zone(
        self, zone_number: int | None, duration: int | None = None
    ) -> None:
        """Optimistically update the active zone after a successful command."""
        if zone_number is None:
            self.async_set_updated_data(WyzeIrrigationRuntimeData(self.device, None))
            return

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
                schedule_type="manual",
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
