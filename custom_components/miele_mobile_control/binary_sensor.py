"""Binary sensors: Mobile Start armed, door open.

These read WasherData.state only and stay available while Mobile Start is
NOT armed — that is when knowing "Mobile Start is not armed" matters most.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MieleConfigEntry, MieleCoordinator, WasherData
from .entity import MieleStateEntity, async_track_washers


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MieleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_track_washers(
        coordinator,
        entry,
        async_add_entities,
        lambda washer: [
            MieleMobileStartBinarySensor(coordinator, washer),
            MieleDoorBinarySensor(coordinator, washer),
        ],
    )


class MieleMobileStartBinarySensor(MieleStateEntity, BinarySensorEntity):
    """On while Mobile Start is armed at the machine.

    Remote controls only work in this state; the sensor says why they are
    unavailable. Mobile Start must be re-armed at the machine for every
    cycle.
    """

    _attr_translation_key = "mobile_start"
    _attr_icon = "mdi:cellphone-wireless"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "mobile_start")

    @property
    def is_on(self) -> bool | None:
        washer = self.washer
        return washer.armed if washer else None


class MieleDoorBinarySensor(MieleStateEntity, BinarySensorEntity):
    """On while the door is open (from State.SignalDoor)."""

    _attr_translation_key = "door"
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "door")

    @property
    def is_on(self) -> bool | None:
        washer = self.washer
        if washer is None:
            return None
        signal = washer.state.get("SignalDoor")
        return None if signal is None else bool(signal)
