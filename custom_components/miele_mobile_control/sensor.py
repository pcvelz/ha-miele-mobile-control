"""Sensors: machine status, remaining time, estimated finish time.

These read WasherData.state only and stay available while Mobile Start is
NOT armed, so the machine's state is visible exactly when the controls
(selects, switches, button) are not.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import STATUS_STATES, finish_time, remaining_minutes, status_name
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
            MieleStatusSensor(coordinator, washer),
            MieleRemainingTimeSensor(coordinator, washer),
            MieleFinishTimeSensor(coordinator, washer),
        ],
    )


class MieleStatusSensor(MieleStateEntity, SensorEntity):
    """What the machine is doing: off, on, programmed, running, ..."""

    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATUS_STATES)
    _attr_icon = "mdi:washing-machine"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "status")

    @property
    def native_value(self) -> str | None:
        washer = self.washer
        return status_name(washer.status) if washer else None


class MieleRemainingTimeSensor(MieleStateEntity, SensorEntity):
    """Minutes left of the running program; unknown without a countdown."""

    _attr_translation_key = "remaining_time"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:timer-outline"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "remaining_time")

    @property
    def native_value(self) -> int | None:
        washer = self.washer
        return remaining_minutes(washer.state) if washer else None


class MieleFinishTimeSensor(MieleStateEntity, SensorEntity):
    """Estimated finish moment; only set while a remaining time is known."""

    _attr_translation_key = "finish_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "finish_time")

    @property
    def native_value(self) -> datetime | None:
        washer = self.washer
        if washer is None:
            return None
        return finish_time(washer.state, dt_util.utcnow())
