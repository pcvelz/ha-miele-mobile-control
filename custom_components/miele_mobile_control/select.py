"""Selects: program, temperature, spin speed, soiling, TwinDos."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import const
from .coordinator import MieleConfigEntry, MieleCoordinator, WasherData
from .entity import MieleEntity, async_track_washers
from .washer import (
    TWINDOS_PATTERNS,
    ProgramContext,
    dosing_values,
    program_label,
    spin_label,
    temperature_label,
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
        lambda washer: [MieleProgramSelect(coordinator, washer)],
    )
    async_track_washers(
        coordinator,
        entry,
        async_add_entities,
        lambda washer: [
            MieleTemperatureSelect(coordinator, washer),
            MieleSpinSpeedSelect(coordinator, washer),
            MieleSoilingSelect(coordinator, washer),
            MieleTwinDosSelect(coordinator, washer),
        ],
    )


class MieleSelect(MieleEntity, SelectEntity):
    """Common base: needs a selected program and a writable mask."""

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData, key: str) -> None:
        super().__init__(coordinator, washer, key)

    @property
    def context(self) -> ProgramContext | None:
        washer = self.washer
        return washer.context if washer else None

    def _writable(self) -> bool:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        context = self.context
        return context is not None and context.prog_id != 0 and self._writable()


class MieleProgramSelect(MieleSelect):
    """Choose a program from the machine's program list (write type 0).

    Unlike the option selects this one is available before a program is
    chosen: an armed, running washer with prog_id 0 still publishes its
    program list, and picking the program is the first thing a new user
    does. With no program chosen the current option is None.
    """

    _attr_translation_key = "program"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "program")

    @property
    def available(self) -> bool:
        if not self._coordinator_up:
            return False
        washer = self.washer
        if washer is None:
            return False
        return const.program_select_available(
            washer.armed, washer.status, washer.program_ids
        )

    @property
    def options(self) -> list[str]:
        washer = self.washer
        if washer is None:
            return []
        return [program_label(pid, washer.program_ids) for pid in washer.program_ids]

    @property
    def current_option(self) -> str | None:
        washer = self.washer
        if washer is None:
            return None
        prog_id = int(washer.state.get("ProgramID", 0) or 0)
        if prog_id and prog_id in washer.program_ids:
            return program_label(prog_id, washer.program_ids)
        return None

    async def async_select_option(self, option: str) -> None:
        washer = self.washer
        if washer is None:
            return
        # Match against the labels this select shows: program_label()
        # disambiguates ids that share a name (e.g. "Denim (123)"), and
        # this comparison resolves each label back to its own id.
        for pid in washer.program_ids:
            if program_label(pid, washer.program_ids) == option:
                await self.coordinator.async_select_program(washer.fab, pid)
                return


class MieleTemperatureSelect(MieleSelect):
    """Choose the wash temperature; eco and non-eco variants stay distinct."""

    _attr_translation_key = "temperature"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "temperature")

    def _writable(self) -> bool:
        context = self.context
        return context is not None and context.temperature.writable

    @property
    def options(self) -> list[str]:
        context = self.context
        if context is None:
            return []
        return [temperature_label(v, i) for v, i in context.temperature.options]

    @property
    def current_option(self) -> str | None:
        context = self.context
        if context is None:
            return None
        wanted = (context.temperature.current, context.temperature.info)
        for value, info in context.temperature.options:
            if (value, info) == wanted:
                return temperature_label(value, info)
        return None

    async def async_select_option(self, option: str) -> None:
        context = self.context
        washer = self.washer
        if context is None or washer is None:
            return
        for value, info in context.temperature.options:
            if temperature_label(value, info) == option:
                await self.coordinator.async_set_parameters(
                    washer.fab, temperature=value, eco=info == 1
                )
                return


class MieleSpinSpeedSelect(MieleSelect):
    """Choose the spin speed."""

    _attr_translation_key = "spin_speed"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "spin_speed")

    def _writable(self) -> bool:
        context = self.context
        return context is not None and context.spin_rpm.writable

    @property
    def options(self) -> list[str]:
        context = self.context
        if context is None:
            return []
        return [spin_label(rpm) for rpm in context.spin_rpm.options]

    @property
    def current_option(self) -> str | None:
        context = self.context
        if context is None:
            return None
        # Current rpm must be one of the offered options, like soiling;
        # otherwise current_option would name a value not in options.
        if context.spin_rpm.current not in context.spin_rpm.options:
            return None
        return spin_label(context.spin_rpm.current)

    async def async_select_option(self, option: str) -> None:
        context = self.context
        washer = self.washer
        if context is None or washer is None:
            return
        for rpm in context.spin_rpm.options:
            if spin_label(rpm) == option:
                await self.coordinator.async_set_parameters(washer.fab, spin_rpm=rpm)
                return


class MieleSoilingSelect(MieleSelect):
    """Choose the soiling level."""

    _attr_translation_key = "soiling"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "soiling")

    def _writable(self) -> bool:
        context = self.context
        return context is not None and context.soiling.writable

    @property
    def options(self) -> list[str]:
        context = self.context
        if context is None:
            return []
        return [str(level) for level in context.soiling.options]

    @property
    def current_option(self) -> str | None:
        context = self.context
        if context is None:
            return None
        if context.soiling.current in context.soiling.options:
            return str(context.soiling.current)
        return None

    async def async_select_option(self, option: str) -> None:
        washer = self.washer
        if washer is None:
            return
        await self.coordinator.async_set_parameters(
            washer.fab, soiling=int(option)
        )


# Option label per TwinDos pattern name, in TWINDOS_PATTERNS order.
TWINDOS_LABELS: dict[str, str] = {
    "off": "Off",
    "whites": "Whites",
    "colours": "Colours",
}


class MieleTwinDosSelect(MieleSelect):
    """Choose the TwinDos laundry liquid pattern: Off, Whites or Colours.

    The current option is the measured pattern matching the washer's
    container values; unknown when none matches (never guessed). Available
    only while both TwinDos containers can be changed.
    """

    _attr_translation_key = "twindos"
    _attr_options = [TWINDOS_LABELS[name] for name in TWINDOS_PATTERNS]

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "twindos")

    def _writable(self) -> bool:
        context = self.context
        return context is not None and twindos_writable(context.dosing)

    @property
    def current_option(self) -> str | None:
        context = self.context
        if context is None or context.dosing is None:
            return None
        return TWINDOS_LABELS.get(twindos_mode(dosing_values(context.dosing)))

    async def async_select_option(self, option: str) -> None:
        washer = self.washer
        if washer is None:
            return
        for name, label in TWINDOS_LABELS.items():
            if label == option:
                await self.coordinator.async_set_parameters(washer.fab, twindos=name)
                return
