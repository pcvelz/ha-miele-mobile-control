"""Sensors: machine status, remaining time, estimated finish time.

These read WasherData.state only and stay available while Mobile Start is
NOT armed, so the machine's state is visible exactly when the controls
(selects, switches, button) are not.

Two more groups join them, all usable while a program runs:

* live readings /State carries the whole time (target temperature, current
  spin speed, elapsed time, raw program phase, delayed start);
* the wash record: what the running (or last) wash was set to, frozen when
  the program began. These are RestoreSensors and survive a restart; the
  full record also lives in the coordinator, which seeds it from the
  wash_state sensor's stored extra data before the first refresh.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    REVOLUTIONS_PER_MINUTE,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoredExtraData
from homeassistant.util import dt as dt_util

from . import profile, washer
from .const import (
    STATUS_STATES,
    elapsed_minutes,
    finish_time,
    program_phase,
    remaining_minutes,
    spin_speed,
    start_time,
    status_name,
    target_temperature,
)
from .coordinator import MieleConfigEntry, MieleCoordinator, WasherData
from .entity import MieleEntity, MieleStateEntity, async_track_washers


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
            MieleTargetTemperatureSensor(coordinator, washer),
            MieleCurrentSpinSpeedSensor(coordinator, washer),
            MieleElapsedTimeSensor(coordinator, washer),
            MieleProgramPhaseSensor(coordinator, washer),
            MieleStartTimeSensor(coordinator, washer),
            MieleWashStateSensor(coordinator, washer),
            MieleWashProgramSensor(coordinator, washer),
            MieleWashTemperatureSensor(coordinator, washer),
            MieleWashSpinSpeedSensor(coordinator, washer),
            MieleWashSoilingSensor(coordinator, washer),
            MieleWashExtrasSensor(coordinator, washer),
            MieleWashStartedSensor(coordinator, washer),
            MieleWashFinishedSensor(coordinator, washer),
            MieleWashDurationSensor(coordinator, washer),
            MieleTwinDosDosingSensor(coordinator, washer, 1),
            MieleTwinDosDosingSensor(coordinator, washer, 2),
            *_profile_sensors(coordinator, washer),
        ],
    )


def _profile_sensors(coordinator: MieleCoordinator, washer: WasherData) -> list:
    """The measured-power entities; none for a washer without a plug."""
    plugs = coordinator.profiles.plugs if coordinator.profiles is not None else {}
    classes = {
        "wash_energy": MieleWashEnergySensor,
        "power_profile": MielePowerProfileSensor,
        "power_profiles": MielePowerProfilesSensor,
        "power_plug": MielePowerPlugSensor,
    }
    return [
        classes[key](coordinator, washer, key)
        for key in profile.profile_entity_keys(plugs, washer.fab)
    ]


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


class MieleTargetTemperatureSensor(MieleStateEntity, SensorEntity):
    """Target wash temperature /State carries while the program runs."""

    _attr_translation_key = "target_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "target_temperature")

    @property
    def native_value(self) -> float | None:
        washer = self.washer
        return target_temperature(washer.state) if washer else None


class MieleCurrentSpinSpeedSensor(MieleStateEntity, SensorEntity):
    """Spin speed /State reports right now (0 while the drum stands still)."""

    _attr_translation_key = "current_spin_speed"
    _attr_native_unit_of_measurement = REVOLUTIONS_PER_MINUTE
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:sync"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "current_spin_speed")

    @property
    def native_value(self) -> int | None:
        washer = self.washer
        return spin_speed(washer.state) if washer else None


class MieleElapsedTimeSensor(MieleStateEntity, SensorEntity):
    """Minutes the running program has been going (ElapsedTime [h, m])."""

    _attr_translation_key = "elapsed_time"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:progress-clock"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "elapsed_time")

    @property
    def native_value(self) -> int | None:
        washer = self.washer
        return elapsed_minutes(washer.state) if washer else None


class MieleProgramPhaseSensor(MieleStateEntity, SensorEntity):
    """Raw ProgramPhase number /State reports.

    Miele publishes the phase as a number without names; the sensor stays
    diagnostic and no phase names are invented.
    """

    _attr_translation_key = "program_phase"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "program_phase")

    @property
    def native_value(self) -> int | None:
        washer = self.washer
        return program_phase(washer.state) if washer else None


class MieleStartTimeSensor(MieleStateEntity, SensorEntity):
    """Delayed start moment; only set while StartTime counts down."""

    _attr_translation_key = "start_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "start_time")

    @property
    def native_value(self) -> datetime | None:
        washer = self.washer
        if washer is None:
            return None
        return start_time(washer.state, dt_util.utcnow())


# ---------- the wash record ----------


class MieleTwinDosDosingSensor(MieleEntity, SensorEntity):
    """Raw current value of one TwinDos container (diagnostic).

    The pair of values forms a TwinDos pattern (washer.TWINDOS_PATTERNS,
    measured on one machine); these sensors show the values raw so another
    detergent configuration can be recognised. Available whenever the
    program context is.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cup-outline"

    def __init__(
        self, coordinator: MieleCoordinator, washer: WasherData, index: int
    ) -> None:
        super().__init__(coordinator, washer, f"twindos_{index}_dosing")
        self._index = index  # 1-based container number
        self._attr_translation_key = f"twindos_{index}_dosing"

    @property
    def available(self) -> bool:
        if not self._coordinator_up:
            return False
        washer = self.washer
        return washer is not None and washer.context is not None

    @property
    def native_value(self) -> int | None:
        washer = self.washer
        if (
            washer is None
            or washer.context is None
            or washer.context.dosing is None
            or self._index > len(washer.context.dosing.containers)
        ):
            return None
        return washer.context.dosing.containers[self._index - 1].current


class MieleWashRecordSensor(MieleStateEntity, RestoreSensor):
    """One field of the frozen wash record of this washer.

    Available whenever the machine is reachable, armed or not — the record
    is exactly what must stay visible after the controls go unavailable.
    The value comes from the coordinator's WashTracker; when the tracker has
    no record yet (e.g. its restore found nothing but the entity itself was
    stored), the entity's own restored value bridges the first poll.
    """

    def __init__(
        self, coordinator: MieleCoordinator, washer: WasherData, key: str
    ) -> None:
        super().__init__(coordinator, washer, key)
        self._restored_value: Any = None

    @property
    def record(self) -> washer.WashRecord | None:
        data = self.washer
        return data.wash if data is not None else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_sensor_data()) is not None:
            self._restored_value = last_data.native_value

    @property
    def native_value(self) -> Any:
        record = self.record
        if record is None:
            return self._restored_value
        return self._record_value(record)

    def _record_value(self, record: washer.WashRecord) -> Any:
        raise NotImplementedError


class MieleWashStateSensor(MieleWashRecordSensor):
    """none / running / finished / interrupted; none before the first wash.

    The whole record rides along as extra restore data: the coordinator
    seeds its WashTracker from it before the first refresh, so a wash that
    was running when Home Assistant stopped keeps its record.
    """

    _attr_translation_key = "wash_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        washer.WASH_NONE,
        washer.WASH_RUNNING,
        washer.WASH_FINISHED,
        washer.WASH_INTERRUPTED,
    ]
    _attr_icon = "mdi:state-machine"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "wash_state")

    def _record_value(self, record: washer.WashRecord) -> str:
        return record.state

    @property
    def native_value(self) -> str:
        record = self.record
        return record.state if record is not None else washer.WASH_NONE

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        """Hand the full record to the coordinator's restart recovery."""
        record = self.record
        if record is None:
            return None
        return RestoredExtraData({"record": record.to_dict()})


class MieleWashProgramSensor(MieleWashRecordSensor):
    """Name of the program the (last) wash ran with."""

    _attr_translation_key = "wash_program"
    _attr_icon = "mdi:tshirt-crew-outline"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "wash_program")

    def _record_value(self, record: washer.WashRecord) -> str | None:
        return record.program_name


class MieleWashTemperatureSensor(MieleWashRecordSensor):
    """Wash temperature in °C; attribute eco marks the eco variant."""

    _attr_translation_key = "wash_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "wash_temperature")

    def _record_value(self, record: washer.WashRecord) -> int | None:
        return record.temperature

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self.record
        return {"eco": bool(record and record.eco)}


class MieleWashSpinSpeedSensor(MieleWashRecordSensor):
    """Spin speed the (last) wash ran with, in rpm (0 = no spin)."""

    _attr_translation_key = "wash_spin_speed"
    _attr_native_unit_of_measurement = REVOLUTIONS_PER_MINUTE
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:sync"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "wash_spin_speed")

    def _record_value(self, record: washer.WashRecord) -> int | None:
        return record.spin_rpm


class MieleWashSoilingSensor(MieleWashRecordSensor):
    """Soiling level the (last) wash ran with; unknown when not offered."""

    _attr_translation_key = "wash_soiling"
    _attr_icon = "mdi:bucket-outline"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "wash_soiling")

    def _record_value(self, record: washer.WashRecord) -> int | None:
        return record.soiling


class MieleWashExtrasSensor(MieleWashRecordSensor):
    """Extras the (last) wash ran with, as readable names.

    Attribute ``extras`` lists the selected extra keys; every extra the
    recorded program offered is exposed as one boolean attribute, so a
    dashboard can show the full choice set of the wash.
    """

    _attr_translation_key = "wash_extras"
    _attr_icon = "mdi:puzzle-outline"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "wash_extras")
        self._restored_attributes: dict[str, Any] | None = None

    def _record_value(self, record: washer.WashRecord) -> str:
        return washer.wash_extras_label(record)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self.record
        if record is None:
            return self._restored_attributes or {}
        attributes: dict[str, Any] = {
            "extras": [name for name, on in record.extras.items() if on]
        }
        for name in record.extras_offered:
            attributes[name] = bool(record.extras.get(name))
        attributes["twindos"] = list(record.twindos)
        attributes["twindos_mode"] = record.twindos_mode
        return attributes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_extra_data()
        if last is not None and isinstance(last.json_dict.get("attributes"), dict):
            self._restored_attributes = dict(last.json_dict["attributes"])

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        attributes = self.extra_state_attributes
        if not attributes:
            return None
        return RestoredExtraData({"attributes": attributes})


class MieleWashStartedSensor(MieleWashRecordSensor):
    """Moment the (last) wash began (entering waiting-to-start or running)."""

    _attr_translation_key = "wash_started"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "wash_started")

    def _record_value(self, record: washer.WashRecord) -> datetime | None:
        return dt_util.utc_from_timestamp(record.started)


class MieleWashFinishedSensor(MieleWashRecordSensor):
    """Moment the (last) wash ended; unknown while it runs."""

    _attr_translation_key = "wash_finished"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "wash_finished")

    def _record_value(self, record: washer.WashRecord) -> datetime | None:
        if record.finished is None:
            return None
        return dt_util.utc_from_timestamp(record.finished)


class MieleWashDurationSensor(MieleWashRecordSensor):
    """How long the (last) wash ran, in minutes; known once it ended."""

    _attr_translation_key = "wash_duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:progress-clock"

    def __init__(self, coordinator: MieleCoordinator, washer: WasherData) -> None:
        super().__init__(coordinator, washer, "wash_duration")

    def _record_value(self, record: washer.WashRecord) -> int | None:
        return record.duration


# ---------- measured power profiles (only with a plug in the options) ----------


class MieleProfileEntity(MieleEntity, SensorEntity):
    """Base of the profile sensors: local data, available while loaded."""

    def __init__(
        self, coordinator: MieleCoordinator, washer: WasherData, key: str
    ) -> None:
        super().__init__(coordinator, washer, key)
        self._attr_translation_key = key

    @property
    def profiles(self) -> Any:
        return self.coordinator.profiles

    @property
    def available(self) -> bool:
        return self.profiles is not None


class MieleWashEnergySensor(MieleProfileEntity):
    """kWh of the wash being measured, or of the last measured wash.

    The per-minute detail rides along as attributes (wh, minutes,
    missing_minutes, key) instead of a separate wash_power sensor: it has no
    natural state of its own and belongs to exactly this wash.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _unrecorded_attributes = frozenset({"wh"})

    def _run(self) -> tuple[Any, bool]:
        if self.profiles is None:
            return None, False
        return self.profiles.current_run(self.fab)

    @property
    def native_value(self) -> float | None:
        run, _ = self._run()
        return run.kwh if run is not None else None

    @property
    def last_reset(self) -> datetime | None:
        run, _ = self._run()
        return dt_util.utc_from_timestamp(run.started) if run is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        run, measuring = self._run()
        if run is None:
            return {}
        return {
            "key": run.key,
            "minutes": run.minutes,
            "missing_minutes": run.missing,
            "wh": run.wh,
            "from_energy_sensor": run.from_meter,
            "measuring": measuring,
            "stored": (not measuring) and run.storable,
        }


class MielePowerProfileSensor(MieleProfileEntity):
    """Profile of the combination selected now (or of the running wash).

    State = number of stored runs for that key (0 = never measured).
    """

    _attr_icon = "mdi:chart-bell-curve"
    _attr_native_unit_of_measurement = "runs"
    _unrecorded_attributes = frozenset({"wh"})

    @property
    def native_value(self) -> int | None:
        if self.profiles is None:
            return None
        found = self.profiles.data.matrix.profile(self.profiles.current_key(self.fab))
        return found["runs"] if found else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.profiles is None:
            return {}
        key = self.profiles.current_key(self.fab)
        found = self.profiles.data.matrix.profile(key) or {}
        return {
            "key": key,
            "wh": found.get("wh", []),
            "minutes": found.get("minutes"),
            "kwh": found.get("kwh"),
            "runs": found.get("runs", 0),
            "last_run": found.get("last_run"),
        }


class MielePowerProfilesSensor(MieleProfileEntity):
    """The whole matrix for templates; state = number of keys."""

    _attr_icon = "mdi:table"
    _attr_native_unit_of_measurement = "profiles"
    _unrecorded_attributes = frozenset({"profiles"})

    @property
    def native_value(self) -> int | None:
        if self.profiles is None:
            return None
        return len(self.profiles.data.matrix.runs)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.profiles is None:
            return {}
        return {"profiles": self.profiles.data.matrix.profiles()}


class MielePowerPlugSensor(MieleProfileEntity):
    """Diagnostic: the power sensor measuring this washer."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:power-plug-outline"

    @property
    def native_value(self) -> str | None:
        plug = self.profiles.plugs.get(self.fab) if self.profiles else None
        return plug.power_sensor if plug else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plug = self.profiles.plugs.get(self.fab) if self.profiles else None
        return {"energy_sensor": plug.energy_sensor if plug else None}
