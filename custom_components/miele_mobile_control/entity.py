"""Base entity for Miele Mobile Control."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MieleCoordinator, WasherData


@callback
def async_track_washers(
    coordinator: MieleCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[WasherData], Iterable],
) -> None:
    """Add entities for every washer now, and for any washer that appears
    later on the Miele account (a machine added after setup used to never
    get entities).
    """
    seen: set[str] = set()

    @callback
    def _sync() -> None:
        for washer in coordinator.data or []:
            if washer.fab in seen:
                continue
            seen.add(washer.fab)
            async_add_entities(list(factory(washer)))

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class MieleEntity(CoordinatorEntity[MieleCoordinator]):
    """One control entity of one washing machine.

    Available only while Mobile Start is armed and the appliance is on
    (Status 2/3) — the states in which the app offers remote control.
    Entities that reflect the machine state regardless of arming derive
    from MieleStateEntity instead.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData, key: str) -> None:
        super().__init__(coordinator)
        self.fab = washer.fab
        self._attr_unique_id = f"{washer.fab}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, washer.fab)},
            name=washer.tech_type,
            manufacturer=MANUFACTURER,
            model=washer.tech_type,
        )

    @property
    def washer(self) -> WasherData | None:
        return self.coordinator.data_for(self.fab)

    @property
    def _coordinator_up(self) -> bool:
        """Last poll succeeded (CoordinatorEntity.available)."""
        return super().available

    @property
    def available(self) -> bool:
        if not self._coordinator_up:
            return False
        washer = self.washer
        return washer is not None and washer.armed and washer.status in (2, 3)


class MieleStateEntity(MieleEntity):
    """Entity that follows the appliance state even when Mobile Start is
    not armed or the machine is off.

    Status, times and door must be visible exactly when the controls are
    not, so availability only requires a successful poll and a reachable
    appliance. Uses WasherData.reachable when the coordinator provides it.
    """

    @property
    def available(self) -> bool:
        if not self._coordinator_up:
            return False
        washer = self.washer
        return washer is not None and getattr(washer, "reachable", True)
