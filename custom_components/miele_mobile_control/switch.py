"""Switches: one per extra option of the selected program, plus TwinDos."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MieleConfigEntry, MieleCoordinator, WasherData
from .entity import MieleEntity, async_track_washers
from .washer import (
    EXTRAS,
    Dosing,
    Extra,
    detergent_from_mode,
    dosing_values,
    twindos_mode,
    twindos_writable,
)


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
            MieleDetergentSwitch(coordinator, washer),
            *[MieleExtraSwitch(coordinator, washer, name) for name in EXTRAS],
        ],
    )


class MieleExtraSwitch(MieleEntity, SwitchEntity):
    """One extra (e.g. extra_quiet, soak, gentle) of the selected program.

    Unavailable when the program does not offer this extra (mask 0). All
    switches stay present even on machines that never offer them; the
    ``offered`` attribute makes that visible so nothing silently disappears.
    """

    def __init__(
        self, coordinator: MieleCoordinator, washer: WasherData, extra: str
    ) -> None:
        super().__init__(coordinator, washer, f"extra_{extra}")
        self._extra_name = extra
        self._attr_translation_key = f"extra_{extra}"

    @property
    def _option(self) -> Extra | None:
        """The extra's slot in the selected program's context, if any."""
        washer = self.washer
        if washer is None or washer.context is None:
            return None
        return washer.context.extras.get(self._extra_name)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        washer = self.washer
        if washer is None or washer.context is None or washer.context.prog_id == 0:
            return False
        # The single place that decides "this program offers this extra".
        option = self._option
        return option is not None and option.offered

    @property
    def is_on(self) -> bool | None:
        option = self._option
        return option.on if option else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        option = self._option
        return {"offered": bool(option and option.offered)}

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        washer = self.washer
        if washer is None:
            return
        await self.coordinator.async_set_parameters(
            washer.fab, extras={self._extra_name: on}
        )


class MieleDetergentSwitch(MieleEntity, SwitchEntity):
    """TwinDos laundry liquid on or off for the selected program: off washes
    without liquid (the measured OFF pattern), on restores the last non-off
    TwinDos pattern seen for this washer.

    Unavailable unless the washer reports exactly two changeable TwinDos
    containers — a pattern is only written when both can be changed. The
    ``twindos`` attribute exposes the raw container values; the TwinDos
    select offers the measured patterns by name.
    """

    _attr_translation_key = "detergent"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "detergent")

    @property
    def _dosing(self) -> Dosing | None:
        """The dosing block of the selected program's context, if any."""
        washer = self.washer
        if washer is None or washer.context is None:
            return None
        return washer.context.dosing

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return twindos_writable(self._dosing)

    @property
    def is_on(self) -> bool | None:
        dosing = self._dosing
        if dosing is None:
            return None
        # An unknown pattern is unknown, not "off".
        return detergent_from_mode(twindos_mode(dosing_values(dosing)))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dosing = self._dosing
        if dosing is None:
            return {}
        values = [c.current for c in dosing.containers]
        return {"twindos": values, "twindos_mode": twindos_mode(values)}

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        washer = self.washer
        if washer is None:
            return
        await self.coordinator.async_set_parameters(washer.fab, detergent=on)
