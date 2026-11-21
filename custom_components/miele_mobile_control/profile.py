"""Measured power profiles of every wash (pure Python, no Home Assistant).

A power-measuring plug chosen in the integration's options feeds this
module. While the washer really runs (/State Status 5, paused 6 keeps the
clock running; NOT while it waits for a delayed start, Status 4) the plug's
samples are integrated into one value per minute: the time-weighted mean
power of that minute, stored as Wh. A finished wash goes into the matrix
under the key of every setting that changes the power use; the profile of a
key is the per-minute mean over its most recent runs.

The Home Assistant side (options flow, state tracking, Store, entities)
lives in power.py and sensor.py and only feeds and reads this module.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .washer import PROGRAM_NAMES, ProgramContext, WashRecord, program_name

# Runs kept per key; the profile is the mean over these.
MAX_RUNS = 5
# A run with more than this share of its minutes missing (plug unavailable,
# Home Assistant down) is not added to the matrix: a profile averaged over
# holes would understate the wash.
MAX_MISSING_SHARE = 0.10
# Version of the stored data (homeassistant.helpers.storage.Store).
STORAGE_VERSION = 1

# /State Status codes that count as "the wash runs": 5 running, 6 paused.
RECORDING_STATUSES = (5, 6)
# Codes that keep an open recording open (4 cannot follow 5 in practice).
KEEP_STATUSES = (4, 5, 6)
STATUS_FINISHED = 7

# Options of the config entry: {"plugs": {fab: {"power_sensor": ...,
# "energy_sensor": ...}}}.
CONF_PLUGS = "plugs"
CONF_POWER_SENSOR = "power_sensor"
CONF_ENERGY_SENSOR = "energy_sensor"

# Entity keys (unique id suffixes) created only for a washer with a plug.
PROFILE_ENTITY_KEYS = ("wash_energy", "power_profile", "power_profiles", "power_plug")

# Program names that more than one id shares ("Denim" is 24 and 123): their
# key carries the id so two programs never share a profile.
_SHARED_NAMES = {name for name, n in Counter(PROGRAM_NAMES.values()).items() if n > 1}


# ---------- options ----------


@dataclass(frozen=True)
class Plug:
    """The measuring plug of one washer."""

    power_sensor: str
    energy_sensor: str | None = None


def plugs_from_options(options: Any) -> dict[str, Plug]:
    """Washer fab -> Plug; a washer without a power sensor has no plug."""
    plugs: dict[str, Plug] = {}
    # Config entry options arrive as a read-only mapping, not a dict.
    raw = options.get(CONF_PLUGS) if isinstance(options, Mapping) else None
    if not isinstance(raw, Mapping):
        return plugs
    for fab, conf in raw.items():
        if not isinstance(conf, Mapping):
            continue
        power = conf.get(CONF_POWER_SENSOR)
        if not isinstance(power, str) or not power:
            continue
        energy = conf.get(CONF_ENERGY_SENSOR)
        plugs[str(fab)] = Plug(
            power_sensor=power,
            energy_sensor=energy if isinstance(energy, str) and energy else None,
        )
    return plugs


def options_with_plug(
    options: Any, fab: str, power_sensor: str | None, energy_sensor: str | None
) -> dict[str, Any]:
    """New options with the plug of *fab* set; no power sensor removes it."""
    new = dict(options) if isinstance(options, Mapping) else {}
    raw = new.get(CONF_PLUGS)
    plugs = {k: dict(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}
    if power_sensor:
        conf: dict[str, str] = {CONF_POWER_SENSOR: power_sensor}
        if energy_sensor:
            conf[CONF_ENERGY_SENSOR] = energy_sensor
        plugs[fab] = conf
    else:
        plugs.pop(fab, None)
    new[CONF_PLUGS] = plugs
    return new


def profile_entity_keys(plugs: dict[str, Plug], fab: str) -> tuple[str, ...]:
    """The profile entities of a washer: all of them with a plug, none without."""
    return PROFILE_ENTITY_KEYS if fab in plugs else ()


def parse_power(state: Any, unit: Any) -> float | None:
    """A power sensor state in W; unavailable/unknown/garbage give None."""
    value = _finite(state)
    if value is None:
        return None
    if unit == "kW":
        return value * 1000
    if unit == "mW":
        return value / 1000
    return value


def parse_energy(state: Any, unit: Any) -> float | None:
    """An energy sensor state in kWh; unavailable/unknown/garbage give None."""
    value = _finite(state)
    if value is None:
        return None
    if unit == "Wh":
        return value / 1000
    if unit == "MWh":
        return value * 1000
    return value


def _finite(state: Any) -> float | None:
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


# ---------- the key ----------


def _key(
    prog_id: int | None,
    temperature: int | None,
    eco: bool,
    spin_rpm: int | None,
    soiling: int | None,
    extras_on: list[str],
) -> str | None:
    if prog_id is None or temperature is None or spin_rpm is None:
        return None
    name = program_name(prog_id)
    if name in _SHARED_NAMES:
        name = f"{name} ({prog_id})"
    soil = "-" if not soiling else str(soiling)
    extras = "+".join(sorted(extras_on)) or "none"
    return f"{name}|{temperature}|eco={int(bool(eco))}|{spin_rpm}|soil={soil}|{extras}"


def profile_key(record: WashRecord | None) -> str | None:
    """Matrix key of a wash record; None when the record lacks the details.

    Format ``<program>|<°C>|eco=<0/1>|<rpm>|soil=<1-3 or ->|<extras or none>``,
    extras sorted and joined with ``+``. TwinDos / laundry liquid is
    deliberately not part of it: it does not change the power use.
    """
    if record is None:
        return None
    return _key(
        record.program_id,
        record.temperature,
        record.eco,
        record.spin_rpm,
        record.soiling,
        [name for name, on in record.extras.items() if on],
    )


def context_key(context: ProgramContext | None) -> str | None:
    """Matrix key of the combination currently selected on the washer."""
    if context is None or not context.prog_id:
        return None
    return _key(
        context.prog_id,
        context.temperature.current,
        context.temperature.info == 1,
        context.spin_rpm.current,
        context.soiling.current or None,
        [name for name, extra in context.extras.items() if extra.offered and extra.on],
    )


# ---------- one measured wash ----------


@dataclass
class MeasuredRun:
    """One wash as measured by the plug."""

    key: str | None
    started: float  # epoch seconds of minute 0 (the washer started running)
    minutes: int
    wh: list[float | None]  # per minute; None = no valid plug coverage
    kwh: float
    missing: int
    from_meter: bool  # kwh came from the energy sensor, not from wh
    finished: bool  # ended "finished" (Status 7), not interrupted

    @property
    def storable(self) -> bool:
        return (
            self.finished
            and self.key is not None
            and self.minutes > 0
            and self.missing <= self.minutes * MAX_MISSING_SHARE
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "started": self.started,
            "minutes": self.minutes,
            "wh": list(self.wh),
            "kwh": self.kwh,
            "missing": self.missing,
            "from_meter": self.from_meter,
            "finished": self.finished,
        }

    @classmethod
    def from_dict(cls, data: Any) -> MeasuredRun | None:
        if not isinstance(data, dict):
            return None
        try:
            wh = data["wh"]
            if not isinstance(wh, list):
                return None
            key = data.get("key")
            return cls(
                key=None if key is None else str(key),
                started=float(data["started"]),
                minutes=int(data["minutes"]),
                wh=[None if w is None else float(w) for w in wh],
                kwh=float(data["kwh"]),
                missing=int(data["missing"]),
                from_meter=bool(data.get("from_meter")),
                finished=bool(data.get("finished", True)),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class Recording:
    """A wash being measured: per-minute [watt-seconds, covered seconds]."""

    start: float
    key: str | None
    buckets: list[list[float]] = field(default_factory=list)
    at: float = 0.0  # integrated up to here
    value: float | None = None  # power held since *at*; None = unavailable
    energy_start: float | None = None
    meter_ok: bool = True  # the energy meter never went backwards

    def advance(self, until: float) -> None:
        """Integrate the held value from *at* to *until*."""
        _integrate(self.buckets, self.start, self.at, until, self.value)
        self.at = max(self.at, until)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "key": self.key,
            "buckets": [list(b) for b in self.buckets],
            "at": self.at,
            "energy_start": self.energy_start,
            "meter_ok": self.meter_ok,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Recording | None:
        if not isinstance(data, dict):
            return None
        try:
            energy_start = data.get("energy_start")
            key = data.get("key")
            return cls(
                start=float(data["start"]),
                key=None if key is None else str(key),
                buckets=[[float(b[0]), float(b[1])] for b in data.get("buckets") or []],
                at=float(data["at"]),
                # Nothing is known about the plug since the save: the time
                # until the next sample counts as missing.
                value=None,
                energy_start=None if energy_start is None else float(energy_start),
                meter_ok=bool(data.get("meter_ok", True)),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None


def _integrate(
    buckets: list[list[float]], start: float, t0: float, t1: float, value: float | None
) -> None:
    """Add *value* W over [t0, t1) to the per-minute buckets from *start*."""
    t0 = max(t0, start)
    if t1 <= t0:
        return
    while len(buckets) < math.ceil((t1 - start) / 60):
        buckets.append([0.0, 0.0])
    if value is None:
        return
    t = t0
    while t < t1:
        minute = int((t - start) // 60)
        boundary = min(start + (minute + 1) * 60, t1)
        buckets[minute][0] += value * (boundary - t)
        buckets[minute][1] += boundary - t
        t = boundary


def _run_from(
    recording: Recording, end: float, energy_end: float | None, finished: bool
) -> MeasuredRun:
    minutes = max(0, math.ceil((end - recording.start) / 60))
    wh: list[float | None] = []
    for i in range(minutes):
        ws, covered = recording.buckets[i] if i < len(recording.buckets) else (0.0, 0.0)
        if covered <= 0:
            wh.append(None)
            continue
        length = min(60.0, end - recording.start - i * 60)
        # The mean power of the covered part, over the whole minute.
        wh.append(round(ws / covered * length / 3600, 5))
    integrated = sum(w for w in wh if w is not None) / 1000
    from_meter = (
        recording.meter_ok
        and recording.energy_start is not None
        and energy_end is not None
        and energy_end >= recording.energy_start
    )
    kwh = energy_end - recording.energy_start if from_meter else integrated
    return MeasuredRun(
        key=recording.key,
        started=recording.start,
        minutes=minutes,
        wh=wh,
        kwh=round(kwh, 6),
        missing=sum(1 for w in wh if w is None),
        from_meter=from_meter,
        finished=finished,
    )


class PowerRecorder:
    """Per washer: follows the plug always, records while the washer runs."""

    def __init__(self) -> None:
        self.power_value: float | None = None
        self.energy_value: float | None = None
        self.recording: Recording | None = None
        # A wash already running when the recorder first sees the washer
        # (installed or plug chosen mid-wash, restart without a saved
        # recording): its beginning was never measured, so it is skipped.
        self.blind = False

    def join(self, status: int) -> None:
        """The first Status after setup: skip a wash whose start was missed."""
        if self.recording is None and status in RECORDING_STATUSES:
            self.blind = True

    def power(self, t: float, watts: float | None) -> None:
        """A plug sample (None = unavailable/unknown); holds until the next."""
        if self.recording is not None:
            self.recording.advance(t)
            self.recording.value = watts
        self.power_value = watts

    def energy(self, t: float, kwh: float | None) -> None:
        """An energy meter reading in kWh (None = unavailable)."""
        rec = self.recording
        if (
            rec is not None
            and kwh is not None
            and self.energy_value is not None
            and kwh < self.energy_value
        ):
            rec.meter_ok = False
        if rec is not None and rec.energy_start is None and kwh is not None:
            rec.energy_start = kwh
        self.energy_value = kwh

    def status(
        self, status: int, *, now: float, record: WashRecord | None
    ) -> MeasuredRun | None:
        """Feed a polled /State Status; returns the run when one ends."""
        if self.blind:
            self.blind = status in RECORDING_STATUSES
            return None
        if self.recording is None:
            if status in RECORDING_STATUSES:
                self.recording = Recording(
                    start=now,
                    key=profile_key(record),
                    at=now,
                    value=self.power_value,
                    energy_start=self.energy_value,
                )
            return None
        if status in KEEP_STATUSES:
            return None
        recording = self.recording
        self.recording = None
        recording.advance(now)
        return _run_from(
            recording, now, self.energy_value, finished=status == STATUS_FINISHED
        )

    def snapshot(self, now: float) -> MeasuredRun | None:
        """The running wash so far (not finished), without changing it."""
        rec = self.recording
        if rec is None:
            return None
        copy = Recording.from_dict(rec.to_dict())
        copy.value = rec.value
        copy.advance(now)
        return _run_from(copy, now, self.energy_value, finished=False)

    def to_dict(self, *, now: float) -> dict[str, Any]:
        """Persistable state; integrates the open recording up to *now*."""
        if self.recording is not None:
            self.recording.advance(now)
        return {"recording": self.recording.to_dict() if self.recording else None}

    @classmethod
    def from_dict(cls, data: Any) -> PowerRecorder:
        recorder = cls()
        if isinstance(data, dict):
            recorder.recording = Recording.from_dict(data.get("recording"))
        return recorder


# ---------- the matrix ----------


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class ProfileMatrix:
    """Key -> the most recent MAX_RUNS measured runs (oldest first)."""

    def __init__(self) -> None:
        self.runs: dict[str, list[MeasuredRun]] = {}

    def add(self, run: MeasuredRun) -> bool:
        if not run.storable or run.key is None:
            return False
        runs = self.runs.setdefault(run.key, [])
        runs.append(run)
        runs.sort(key=lambda r: r.started)
        del runs[:-MAX_RUNS]
        return True

    def forget(self, key: str) -> bool:
        return self.runs.pop(key, None) is not None

    def profile(self, key: str | None) -> dict[str, Any] | None:
        """Per-minute mean over the stored runs of *key*, or None."""
        runs = self.runs.get(key) if key is not None else None
        if not runs:
            return None
        minutes = int(round(statistics.median(r.minutes for r in runs)))
        wh: list[float | None] = []
        for i in range(minutes):
            values = [r.wh[i] for r in runs if i < len(r.wh) and r.wh[i] is not None]
            wh.append(round(sum(values) / len(values), 5) if values else None)
        return {
            "wh": wh,
            "minutes": minutes,
            "kwh": round(sum(r.kwh for r in runs) / len(runs), 4),
            "runs": len(runs),
            "last_run": _iso(max(r.started for r in runs)),
        }

    def profiles(self) -> dict[str, dict[str, Any]]:
        return {key: self.profile(key) for key in sorted(self.runs)}

    def to_dict(self) -> dict[str, Any]:
        return {key: [r.to_dict() for r in runs] for key, runs in self.runs.items()}

    @classmethod
    def from_dict(cls, data: Any) -> ProfileMatrix:
        matrix = cls()
        if not isinstance(data, dict):
            return matrix
        for key, runs in data.items():
            if not isinstance(runs, list):
                continue
            parsed = [r for r in (MeasuredRun.from_dict(x) for x in runs) if r]
            for run in parsed:
                run.key = str(key)  # the matrix key is authoritative
            if parsed:
                matrix.runs[str(key)] = sorted(parsed, key=lambda r: r.started)[-MAX_RUNS:]
        return matrix


class ProfileData:
    """Everything one config entry stores: matrix, open recordings, last attempts."""

    def __init__(self) -> None:
        self.matrix = ProfileMatrix()
        self.recorders: dict[str, PowerRecorder] = {}
        # The last run of each washer, stored in the matrix or not (diagnostics
        # and the wash_energy sensor).
        self.last_attempt: dict[str, MeasuredRun] = {}

    def recorder(self, fab: str) -> PowerRecorder:
        return self.recorders.setdefault(fab, PowerRecorder())

    def to_dict(self, *, now: float) -> dict[str, Any]:
        return {
            "matrix": self.matrix.to_dict(),
            "recorders": {fab: r.to_dict(now=now) for fab, r in self.recorders.items()},
            "last_attempt": {fab: r.to_dict() for fab, r in self.last_attempt.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> ProfileData:
        """Old, empty or corrupt storage loads as empty."""
        result = cls()
        if not isinstance(data, dict):
            return result
        result.matrix = ProfileMatrix.from_dict(data.get("matrix"))
        recorders = data.get("recorders")
        if isinstance(recorders, dict):
            for fab, raw in recorders.items():
                result.recorders[str(fab)] = PowerRecorder.from_dict(raw)
        attempts = data.get("last_attempt")
        if isinstance(attempts, dict):
            for fab, raw in attempts.items():
                run = MeasuredRun.from_dict(raw)
                if run is not None:
                    result.last_attempt[str(fab)] = run
        return result
