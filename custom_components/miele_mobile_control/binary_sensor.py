"""Binary sensors: Mobile Start armed, door open, wash extras.

These read WasherData.state only and stay available while Mobile Start is
NOT armed — that is when knowing "Mobile Start is not armed" matters most.
The wash extra sensors read the frozen wash record instead: one per extra,
on when that extra was selected for the (last) wash, so each can be charted
and used in automations.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MieleConfigEntry, MieleCoordinator, WasherData
from .entity import MieleStateEntity, async_track_washers
from .washer import EXTRAS


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
            *[MieleWashExtraBinarySensor(coordinator, washer, name) for name in EXTRAS],
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


class MieleWashExtraBinarySensor(MieleStateEntity, BinarySensorEntity):
    """One extra of the frozen wash record: on when that wash selected it.

    unknown while no wash is recorded or the recorded program did not offer
    this extra (the ``offered`` attribute tells those apart). All extras
    stay present even on machines that never offer them.
    """

    def __init__(
        self, coordinator: MieleCoordinator, washer: WasherData, extra: str
    ) -> None:
        super().__init__(coordinator, washer, f"wash_{extra}")
        self._extra = extra
        self._attr_translation_key = f"wash_{extra}"

    @property
    def record(self):
        data = self.washer
        return data.wash if data is not None else None

    @property
    def is_on(self) -> bool | None:
        record = self.record
        if record is None or self._extra not in record.extras:
            return None
        return record.extras[self._extra]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self.record
        offered = None if record is None else self._extra in record.extras_offered
        return {"offered": offered}
