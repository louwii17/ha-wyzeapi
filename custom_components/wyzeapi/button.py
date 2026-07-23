"""Platform for button integration."""

from collections.abc import Callable
import logging
from typing import Any

from aiohttp.client_exceptions import ClientConnectionError
from wyzeapy import Wyzeapy
from wyzeapy.exceptions import ParameterError, UnknownApiError
from wyzeapy.services.irrigation_service import Zone
from wyzeapy.services.switch_service import Switch

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_registry import EntityCategory

from .const import CONF_CLIENT, DOMAIN, RESET_BUTTON_PRESSED
from .irrigation import (
    WyzeIrrigationCoordinator,
    async_get_irrigation_coordinators,
    get_quickrun_duration,
)
from .token_manager import token_exception_handler

_LOGGER = logging.getLogger(__name__)
ATTRIBUTION = "Data provided by Wyze"
OUTDOOR_PLUGS = ["WLPPO"]


@token_exception_handler
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Callable[[list[Any], bool], None],
) -> None:
    """This function sets up the config entry.

    :param hass: The Home Assistant Instance
    :param config_entry: The current config entry
    :param async_add_entities: This function adds entities to the config entry
    :return:
    """

    _LOGGER.debug("""Creating new Wyze button component""")
    client: Wyzeapy = hass.data[DOMAIN][config_entry.entry_id][CONF_CLIENT]
    switch_service = await client.switch_service

    buttons = []
    irrigation_coordinators = await async_get_irrigation_coordinators(
        hass, config_entry
    )
    for coordinator in irrigation_coordinators.values():
        buttons.extend(
            WyzeIrrigationZoneButton(coordinator, zone)
            for zone in coordinator.data.device.zones
            if zone.enabled
        )
        buttons.append(WyzeIrrigationStopAllButton(coordinator))

    plugs = await switch_service.get_switches()
    buttons.extend(
        [
            WyzePowerSensorResetButton(plug)
            for plug in plugs
            if plug.product_model in OUTDOOR_PLUGS
        ]
    )

    async_add_entities(buttons, True)


class WyzeIrrigationZoneButton(ButtonEntity):
    """Legacy sprinkler start button retained for existing automations."""

    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:sprinkler"

    def __init__(
        self,
        coordinator: WyzeIrrigationCoordinator,
        zone: Zone,
    ) -> None:
        """Initialize the legacy irrigation zone button."""
        self._coordinator = coordinator
        self._zone = zone
        self._attr_name = zone.name
        self._attr_unique_id = f"Start {coordinator.device.mac}-zone-{zone.zone_number}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return information about the sprinkler controller."""
        device = self._coordinator.device
        return DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
            name=device.nickname,
            manufacturer="WyzeLabs",
            model=device.product_model,
            serial_number=device.sn,
            connections={(dr.CONNECTION_NETWORK_MAC, device.mac)},
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details retained by the legacy entity."""
        return {
            "zone_number": self._zone.zone_number,
            "zone_id": self._zone.zone_id,
            "enabled": self._zone.enabled,
            "quickrun_duration": get_quickrun_duration(
                self.hass,
                self._coordinator,
                self._zone,
            ),
        }

    @token_exception_handler
    async def async_press(self) -> None:
        """Start this zone using the configured quick-run duration."""
        duration = get_quickrun_duration(self.hass, self._coordinator, self._zone)
        if duration <= 0:
            raise HomeAssistantError(
                f"Invalid quick-run duration for zone {self._zone.name}"
            )

        try:
            await self._coordinator.async_start_zone(
                self._zone.zone_number,
                duration,
            )
        except (ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(
                f"Unable to start Wyze sprinkler zone {self._zone.name}: {err}"
            ) from err


class WyzeIrrigationStopAllButton(ButtonEntity):
    """Representation of a Wyze Irrigation Stop All Schedules Button."""

    _attr_name = "Stop All Zones"

    def __init__(self, coordinator: WyzeIrrigationCoordinator) -> None:
        """Initialize the irrigation stop all button."""
        self._coordinator = coordinator

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the button."""
        return f"Stop All {self._coordinator.device.mac}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this entity."""
        device = self._coordinator.device
        return DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
            name=device.nickname,
            manufacturer="WyzeLabs",
            model=device.product_model,
            serial_number=device.sn,
            connections={(dr.CONNECTION_NETWORK_MAC, device.mac)},
        )

    @property
    def device_class(self) -> str:
        """Return the device class of the button."""
        return ButtonDeviceClass.RESTART

    @property
    def icon(self) -> str:
        """Return the icon for the stop all button."""
        return "mdi:octagon"

    async def async_press(self) -> None:
        """Stop all running irrigation schedules.

        This method is called when the button is pressed in Home Assistant.
        It will stop all running irrigation schedules for the device.

        Raises:
            HomeAssistantError: If the schedules cannot be stopped.
        """
        try:
            await self._coordinator.async_stop()
        except (ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(f"Failed to stop schedules: {err}") from err


class WyzePowerSensorResetButton(ButtonEntity):
    """Wyze Power Sensor Reset Button."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Energy Usage Reset"

    def __init__(self, switch: Switch) -> None:
        """Initialize a power sensor reset button."""
        self._switch = switch

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this entity."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._switch.mac)},
            name=self._switch.nickname,
        )

    @property
    def unique_id(self) -> str:
        """Create a unique ID for the button."""
        return f"{self._switch.mac} Reset button"

    async def async_press(self) -> None:
        """Reset the sensor usage."""
        async_dispatcher_send(
            self.hass,
            f"{RESET_BUTTON_PRESSED}-{self._switch.mac}",
            self._switch,
        )
