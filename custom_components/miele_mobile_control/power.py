"""Home Assistant side of the measured power profiles (thin by design).

Created only when a washer of the entry has a power sensor in the options.
It feeds the plug's state changes and the coordinator's polled Status into
profile.PowerRecorder, keeps the matrix in a versioned Store (one per
config entry) and saves an open recording at most once a minute, so a
restart only loses its downtime. All logic lives in profile.py.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .profile import (
    STORAGE_VERSION,
    MeasuredRun,
    Plug,
    ProfileData,
    context_key,
    parse_energy,
    parse_power,
    profile_key,
)
from .washer import WASH_RUNNING

if TYPE_CHECKING:
    from .coordinator import MieleConfigEntry, MieleCoordinator

_LOGGER = logging.getLogger(__name__)

# An open recording is saved at most this often (seconds).
SAVE_INTERVAL = 60.0


class PowerProfiles:
    """Recorders and matrix of one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MieleConfigEntry,
        coordinator: MieleCoordinator,
        plugs: dict[str, Plug],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.plugs = plugs
        self.data = ProfileData()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.profiles"
        )
        self._last_save = 0.0
        # entity id -> (fab, "power" | "energy")
        self._sources: dict[str, tuple[str, str]] = {}
        # washers whose first polled Status was seen since setup
        self._joined: set[str] = set()

    async def async_setup(self) -> None:
        try:
            stored = await self._store.async_load()
        except Exception as err:  # corrupt file: start empty, keep running
            _LOGGER.debug("power profile storage unreadable, starting empty: %s", err)
            stored = None
        self.data = ProfileData.from_dict(stored)
        for fab in list(self.data.recorders):
            if fab not in self.plugs:
                del self.data.recorders[fab]
        for fab, plug in self.plugs.items():
            self._sources[plug.power_sensor] = (fab, "power")
            if plug.energy_sensor:
                self._sources[plug.energy_sensor] = (fab, "energy")
        now = time.time()
        for entity_id in self._sources:
            self._feed(entity_id, self.hass.states.get(entity_id), now)
        self.entry.async_on_unload(
            async_track_state_change_event(
                self.hass, list(self._sources), self._async_state_changed
            )
        )
        self.entry.async_on_unload(
            self.coordinator.async_add_listener(self._async_polled)
        )
        self._async_polled()

    async def async_unload(self) -> None:
        await self._store.async_save(self._to_store())

    def _to_store(self) -> dict[str, Any]:
        return self.data.to_dict(now=time.time())

    def _feed(self, entity_id: str, state: Any, t: float) -> None:
        fab, kind = self._sources[entity_id]
        recorder = self.data.recorder(fab)
        value = None if state is None else state.state
        unit = None if state is None else state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if kind == "power":
            recorder.power(t, parse_power(value, unit))
        else:
            recorder.energy(t, parse_energy(value, unit))

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id in self._sources:
            self._feed(entity_id, event.data["new_state"], event.time_fired.timestamp())

    @callback
    def _async_polled(self) -> None:
        """Run the recorders on the newest polled Status of each washer."""
        now = time.time()
        recording = False
        for fab in self.plugs:
            washer = self.coordinator.data_for(fab)
            if washer is None or not washer.reachable:
                continue
            recorder = self.data.recorder(fab)
            if fab not in self._joined:
                self._joined.add(fab)
                recorder.join(washer.status)
            run = recorder.status(washer.status, now=now, record=washer.wash)
            if run is not None:
                self._async_finished(fab, run)
            recording = recording or recorder.recording is not None
        if recording and now - self._last_save >= SAVE_INTERVAL:
            self._async_save(now)

    @callback
    def _async_finished(self, fab: str, run: MeasuredRun) -> None:
        self.data.last_attempt[fab] = run
        if self.data.matrix.add(run):
            _LOGGER.debug("%s: wash measured and stored under %s", fab, run.key)
        else:
            _LOGGER.debug(
                "%s: wash measured but not stored (finished=%s, key=%s, missing=%d/%d)",
                fab, run.finished, run.key, run.missing, run.minutes,
            )
        self._async_save(time.time())

    @callback
    def _async_save(self, now: float) -> None:
        self._last_save = now
        self._store.async_delay_save(self._to_store, 1)

    # ---------- read side (entities, service) ----------

    def current_key(self, fab: str) -> str | None:
        """Key of the running/waiting wash, else the armed selection, else the last wash."""
        washer = self.coordinator.data_for(fab)
        if washer is None:
            return None
        if washer.wash is not None and washer.wash.state == WASH_RUNNING:
            return profile_key(washer.wash)
        if washer.context is not None:
            return context_key(washer.context)
        return profile_key(washer.wash)

    def current_run(self, fab: str) -> tuple[MeasuredRun | None, bool]:
        """(run, measuring): the open recording so far, else the last attempt."""
        recorder = self.data.recorders.get(fab)
        if recorder is not None and recorder.recording is not None:
            return recorder.snapshot(time.time()), True
        return self.data.last_attempt.get(fab), False

    @callback
    def async_forget(self, key: str) -> bool:
        removed = self.data.matrix.forget(key)
        if removed:
            self._async_save(time.time())
            self.coordinator.async_update_listeners()
        return removed
