"""Button: start the selected program (user request 1 on 2/1583)."""

from __future__ import annotations

from time import monotonic

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import const
from .coordinator import MieleConfigEntry, MieleCoordinator, WasherData
from .entity import MieleEntity, async_track_washers
from .washer import start_allowed


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
        lambda washer: [MieleStartButton(coordinator, washer)],
    )


class MieleStartButton(MieleEntity, ButtonEntity):
    """Start the program the machine is waiting with.

    Offered only while Status 3 (program selected) and Mobile Start armed,
    as the Miele app does. After a press the button stays unavailable for
    30 s: Mobile Start is spent once per arming, so a double press must not
    be able to send the start twice (the coordinator refuses a repeat too,
    this only keeps the button honest).
    """

    _attr_translation_key = "start"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "start")
        self._pressed_at: float | None = None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        washer = self.washer
        if washer is None or not start_allowed(washer.state):
            return False
        return not const.within_cooldown(self._pressed_at, monotonic())

    async def async_press(self) -> None:
        # Record the press before the call so the cooldown holds even when
        # the start is refused or fails: the arming may be spent either way.
        self._pressed_at = monotonic()
        self.async_write_ha_state()
        await self.coordinator.async_start(self.fab)
