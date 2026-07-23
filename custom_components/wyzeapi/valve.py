"""Valve entities for Wyze sprinkler zones."""

from collections.abc import Callable
import logging
from typing import Any

from aiohttp.client_exceptions import ClientConnectionError
from wyzeapy.exceptions import (
    ParameterError,
    UnknownApiError,
)
from wyzeapy.services.irrigation_service import Zone

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .const import DOMAIN, IRRIGATION_UPDATED
from .irrigation import (
    WyzeIrrigationCoordinator,
    async_get_irrigation_coordinators,
)
from .token_manager import token_exception_handler

_LOGGER = logging.getLogger(__name__)
ATTRIBUTION = "Data provided by Wyze"


@token_exception_handler
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Callable[[list[Any], bool], None],
) -> None:
    """Set up Wyze sprinkler zone valves."""
    entities: list[WyzeIrrigationZoneValve] = []
    coordinators = await async_get_irrigation_coordinators(hass, config_entry)
    for coordinator in coordinators.values():
        device = coordinator.data.device
        config_entry.async_on_unload(
            async_dispatcher_connect(
                hass,
                f"{IRRIGATION_UPDATED}-{device.mac}",
                coordinator.set_running_zone,
            )
        )
        entities.extend(
            WyzeIrrigationZoneValve(coordinator, zone)
            for zone in coordinator.data.device.zones
            if zone.enabled
        )

    async_add_entities(entities)


class WyzeIrrigationZoneValve(
    CoordinatorEntity[WyzeIrrigationCoordinator], ValveEntity
):
    """Representation of a Wyze sprinkler zone."""

    _attr_attribution = ATTRIBUTION
    _attr_device_class = ValveDeviceClass.WATER
    _attr_has_entity_name = True
    _attr_reports_position = False
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    def __init__(
        self,
        coordinator: WyzeIrrigationCoordinator,
        zone: Zone,
    ) -> None:
        """Initialize a sprinkler zone valve."""
        super().__init__(coordinator)
        self._zone = zone
        self._attr_name = zone.name
        self._attr_unique_id = f"{coordinator.device.mac}-zone-{zone.zone_number}-valve"

    @property
    def device_info(self) -> DeviceInfo:
        """Return information about the sprinkler controller."""
        device = self.coordinator.device
        return DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
            name=device.nickname,
            manufacturer="WyzeLabs",
            model=device.product_model,
            serial_number=device.sn,
            connections={(dr.CONNECTION_NETWORK_MAC, device.mac)},
        )

    @property
    def available(self) -> bool:
        """Return whether the controller is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.device.available
        )

    @property
    def is_closed(self) -> bool:
        """Return whether this zone is not currently watering."""
        return self.coordinator.data.running_zone_number != self._zone.zone_number

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return zone details."""
        return {
            "zone_number": self._zone.zone_number,
            "zone_id": self._zone.zone_id,
            "enabled": self._zone.enabled,
        }

    def _quickrun_duration(self) -> int:
        """Return the configured quick-run duration in seconds."""
        unique_id = (
            f"{self.coordinator.device.mac}-zone-"
            f"{self._zone.zone_number}-quickrun-duration"
        )
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "number", DOMAIN, unique_id
        )
        if entity_id and (state := self.hass.states.get(entity_id)) is not None:
            try:
                duration = int(float(state.state) * 60)
            except (TypeError, ValueError):
                duration = 0
            if duration > 0:
                return duration

        return self._zone.quickrun_duration

    @token_exception_handler
    async def async_open_valve(self) -> None:
        """Start watering this zone."""
        duration = self._quickrun_duration()
        if duration <= 0:
            raise HomeAssistantError(
                f"Invalid quick-run duration for zone {self._zone.name}"
            )

        try:
            async with self.coordinator.command_lock:
                running_zone = self.coordinator.data.running_zone_number
                if running_zone is not None and running_zone != self._zone.zone_number:
                    await self.coordinator.irrigation_service.stop_running_schedule(
                        self.coordinator.device
                    )
                    self.coordinator.set_running_zone(None)

                await self.coordinator.irrigation_service.start_zone(
                    self.coordinator.device,
                    self._zone.zone_number,
                    duration,
                )
        except (ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(
                f"Unable to start Wyze sprinkler zone {self._zone.name}: {err}"
            ) from err

        self.coordinator.set_running_zone(self._zone.zone_number, duration)

    @token_exception_handler
    async def async_close_valve(self) -> None:
        """Stop watering this zone."""
        if self.is_closed:
            return

        try:
            async with self.coordinator.command_lock:
                if self.is_closed:
                    return
                await self.coordinator.irrigation_service.stop_running_schedule(
                    self.coordinator.device
                )
        except (ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(
                f"Unable to stop Wyze sprinkler zone {self._zone.name}: {err}"
            ) from err

        self.coordinator.set_running_zone(None)
