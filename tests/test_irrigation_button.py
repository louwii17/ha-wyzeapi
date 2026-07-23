"""Tests for Wyze sprinkler compatibility buttons."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.wyzeapi.button import (
    WyzeIrrigationStopAllButton,
    WyzeIrrigationZoneButton,
)
from custom_components.wyzeapi.irrigation import WyzeIrrigationRuntimeData


@pytest.fixture
def coordinator() -> Mock:
    """Return a coordinator used by legacy and global-stop buttons."""
    device = SimpleNamespace(
        mac="AA:BB:CC:DD:EE:FF",
        nickname="Backyard Sprinkler",
        product_model="BS_WK1",
        sn="SPRINKLER123",
        available=True,
    )
    coordinator = Mock()
    coordinator.device = device
    coordinator.data = WyzeIrrigationRuntimeData(device, None)
    coordinator.async_start_zone = AsyncMock()
    coordinator.async_stop = AsyncMock()
    return coordinator


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


def test_legacy_zone_button_is_disabled_by_default(
    coordinator: Mock,
    zone: SimpleNamespace,
) -> None:
    """New users get valves while existing button registry entries survive."""
    button = WyzeIrrigationZoneButton(coordinator, zone)

    assert button.entity_registry_enabled_default is False
    assert button.unique_id == "Start AA:BB:CC:DD:EE:FF-zone-2"
    assert button.name == "Back Lawn"


@pytest.mark.asyncio
async def test_legacy_zone_button_uses_shared_coordinator(
    coordinator: Mock,
    zone: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy button automations use the same locked command path as valves."""
    monkeypatch.setattr(
        "custom_components.wyzeapi.button.get_quickrun_duration",
        Mock(return_value=900),
    )
    button = WyzeIrrigationZoneButton(coordinator, zone)
    button.hass = Mock()

    await button.async_press()

    coordinator.async_start_zone.assert_awaited_once_with(2, 900)


@pytest.mark.asyncio
async def test_stop_all_button_uses_shared_coordinator(
    coordinator: Mock,
) -> None:
    """Global stop participates in the controller command lock."""
    button = WyzeIrrigationStopAllButton(coordinator)

    await button.async_press()

    coordinator.async_stop.assert_awaited_once()
    assert button.unique_id == "Stop All AA:BB:CC:DD:EE:FF"
