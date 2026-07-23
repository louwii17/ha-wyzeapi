"""Shared runtime state for Wyze sprinkler controllers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from time import time

from wyzeapy import Wyzeapy
from wyzeapy.exceptions import AccessTokenError, LoginError
from wyzeapy.services.irrigation_service import (
    Irrigation,
    IrrigationRun,
    IrrigationService,
)

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

    async def _async_get_current_run(self) -> IrrigationRun | None:
        """Fetch the current zone run through wyzeapy's public API."""
        current_run = await self.irrigation_service.get_current_run(self.device)
        _LOGGER.debug(
            "Wyze sprinkler %s active run: zone=%s start=%s end=%s type=%s",
            self.device.mac,
            current_run.zone_number if current_run else None,
            current_run.start_ts if current_run else None,
            current_run.end_ts if current_run else None,
            current_run.schedule_type if current_run else None,
        )
        return current_run

    async def _async_get_schedules_enabled(self) -> bool | None:
        """Return the cached controller schedule-enabled setting."""
        now = time()
        if now < self._device_info_refresh_at:
            return self._schedules_enabled

        try:
            controller_info = await self.irrigation_service.get_controller_info(
                self.device
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

        self._schedules_enabled = controller_info.schedules_enabled
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
            current_run = await self._async_get_current_run()
            schedules_enabled = await self._async_get_schedules_enabled()
        except (AccessTokenError, LoginError) as err:
            raise ConfigEntryAuthFailed(
                "Unable to authenticate with Wyze; please reauthenticate"
            ) from err
        except Exception as err:
            raise UpdateFailed(
                f"Unable to update Wyze sprinkler {self.device.nickname}: {err}"
            ) from err

        schedule_type = current_run.schedule_type if current_run else None
        program_mode = derive_program_mode(schedule_type, schedules_enabled)
        _LOGGER.debug(
            "Wyze sprinkler %s program mode: %s",
            self.device.mac,
            program_mode,
        )

        if current_run is None:
            return WyzeIrrigationRuntimeData(
                device=self.device,
                running_zone_number=None,
                schedules_enabled=schedules_enabled,
                program_mode=program_mode,
            )

        return WyzeIrrigationRuntimeData(
            device=self.device,
            running_zone_number=current_run.zone_number,
            running_zone_name=current_run.zone_name,
            run_start=(
                dt_util.utc_from_timestamp(current_run.start_ts)
                if current_run.start_ts is not None
                else None
            ),
            run_end=(
                dt_util.utc_from_timestamp(current_run.end_ts)
                if current_run.end_ts is not None
                else None
            ),
            schedule_name=current_run.schedule_name,
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
