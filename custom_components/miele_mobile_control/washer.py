"""Washing-machine logic on top of the DOP2 codec: program names, context
parsing, parameter building and validation, start request.

Pure Python, no Home Assistant imports. Verified against captures of a
WSI863 (Wi-Fi module EK057, firmware 08.37) made through the Miele app
cloud ("DOP2 tunnel").
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from typing import Any

from .dop2 import decode, encode

_LOGGER = logging.getLogger(__name__)

# Seconds to wait for the washer to confirm a write before giving up. The
# washer answers a write in about 8 s and the cloud can add latency, so the
# bound is generous; a timeout raises WriteNotConfirmedError, never silence.
WRITE_CONFIRM_TIMEOUT = 20.0
_CONFIRM_POLL_INTERVAL = 0.5

# Seconds after a sent start during which a second start is refused. Mobile
# Start is spent once per arming, so a duplicate request costs the owner a
# trip to the machine; when in doubt (cloud hiccup after the PUT) we refuse.
START_COOLDOWN = 30.0

# DOP2 leaves (unit/attribute) of the program selection, per the koti miele-sdk.
LEAF_PROGRAM_LIST = (2, 1584)  # idx 1/1: program ids the machine offers
LEAF_PROGRAM_CONTEXT = (2, 1574)  # idx 0/1: options + current values
LEAF_PROGRAM_SELECT = (2, 1577)  # idx 0/0: what is written
LEAF_USER_REQUEST = (2, 1583)  # idx 0/0: start = request id 1

USER_REQUEST_START = 1

# Device states from /State in which the DOP2 program context exists.
ACTIVE_STATES = (2, 3, 4)

# 2/1577 selection_para_wm extras member ids (koti GlobalPsExtrasWM)
EXTRAS: dict[str, int] = {
    "quick": 1,
    "single": 2,
    "water_plus": 3,
    "rinsing_plus": 4,
    "pre_wash": 5,
    "soak": 6,
    "rinse_hold": 7,
    "extra_quiet": 8,
    "steam_smoothing": 9,
    "pre_rinse": 10,
    "microfibre": 11,
    "gentle": 12,
    "allergo_wash": 13,
    "eco": 14,
    "intensive": 15,
    "starch_hold": 16,
    "hygiene_plus": 18,
}

# Readable names of the extras, in the Miele app's wording. The switch and
# wash binary-sensor translations in strings.json carry the same names;
# tests/test_entities.py keeps the two in sync.
EXTRA_NAMES: dict[str, str] = {
    "quick": "Quick",
    "single": "SingleWash",
    "water_plus": "Water plus",
    "rinsing_plus": "Extra rinse",
    "pre_wash": "Pre-wash",
    "soak": "Soak",
    "rinse_hold": "Rinse hold",
    "extra_quiet": "Extra quiet",
    "steam_smoothing": "Pre-ironing",
    "pre_rinse": "Pre-rinse",
    "microfibre": "Microfibre",
    "gentle": "Extra gentle",
    "allergo_wash": "AllergoWash",
    "eco": "Eco",
    "intensive": "Intensive",
    "starch_hold": "Starch hold",
    "hygiene_plus": "Hygiene plus",
}

# Extras the app writes on a washer (koti WashingMachine::DOP2_EXTRAS);
# a service call writes these off unless listed.
WRITTEN_EXTRAS = (
    "intensive",
    "eco",
    "extra_quiet",
    "allergo_wash",
    "soak",
    "quick",
    "rinse_hold",
    "pre_wash",
    "water_plus",
    "rinsing_plus",
    "steam_smoothing",
    "pre_rinse",
    "microfibre",
    "single",
    "gentle",
    "hygiene_plus",
)

# Program id -> display name. Ids and names from tiehfood/ha-miele-at-lan
# (custom_components/miele_lan/enums.py, WashingMachineProgramId), MIT licensed.
PROGRAM_NAMES: dict[int, str] = {
    1: "Cottons",
    3: "Minimum iron",
    4: "Delicates",
    8: "Woollens",
    9: "Silks",
    17: "Starch",
    18: "Rinse",
    21: "Drain/Spin",
    22: "Curtains",
    23: "Shirts",
    24: "Denim",
    27: "Proofing",
    29: "Sportswear",
    31: "Automatic Plus",
    37: "Outerwear",
    39: "Pillows",
    50: "Dark garments",
    52: "Separate rinse/starch",
    53: "First wash",
    69: "Cottons Hygiene",
    75: "Steam finish",
    76: "Refresh",
    77: "Trainers",
    91: "Clean machine",
    95: "Down duvets",
    122: "Express 20",
    123: "Denim",
    129: "Down-filled items",
    133: "Cottons Eco",
    146: "QuickPowerWash",
    190: "Eco 40-60",
}


def program_name(prog_id: int) -> str:
    """Display name of a program id; unknown ids stay selectable."""
    return PROGRAM_NAMES.get(prog_id, f"Program {prog_id}")


def program_label(prog_id: int, program_ids: list[int] | None = None) -> str:
    """Option label for a program id, consistent with program_id().

    When several ids the machine offers share one name (24 and 123 are both
    "Denim"), the label carries the id so the select shows two distinct
    options that each resolve back to their own id.
    """
    name = program_name(prog_id)
    if program_ids:
        same_name = [p for p in program_ids if program_name(p) == name]
        if len(same_name) > 1:
            return f"{name} ({prog_id})"
    return name


def program_id(
    name_or_id: str | int, program_ids: list[int] | None = None
) -> int:
    """Resolve a program name (case-insensitive) or numeric id to an id.

    With *program_ids* (the list this machine reports, 2/1584) resolution is
    checked against that list: a name resolves to an id the machine offers
    (ids sharing a name resolve to the one in the list, e.g. "Denim" -> 123
    on a WSI863), and a number not in the list raises ValueError naming the
    offered programs.
    """
    if isinstance(name_or_id, int):
        number = name_or_id
    else:
        text = str(name_or_id).strip()
        if text.isdigit():
            number = int(text)
        else:
            folded = text.casefold()
            candidates = (
                list(PROGRAM_NAMES) if program_ids is None else program_ids
            )
            for pid in candidates:
                name = PROGRAM_NAMES.get(pid)
                if name is not None and name.casefold() == folded:
                    return pid
            raise ValueError(f"unknown program: {name_or_id!r}")
    if program_ids is not None and number not in program_ids:
        offered = ", ".join(
            f"{program_name(pid)} ({pid})" for pid in program_ids
        )
        raise ValueError(
            f"program {number} is not offered by this washer; "
            f"offered: {offered}"
        )
    return number


class NotOfferedError(ValueError):
    """The washer does not offer the requested value for this program."""


class NoProgramError(ValueError):
    """The appliance shows no program to set."""


class WriteNotConfirmedError(Exception):
    """The washer did not confirm a write within the bounded wait."""


class StartRefusedError(Exception):
    """Start must not be sent: guard failed or a start went out recently."""


@dataclass
class Choice:
    """One option parameter of the selected program (temperature, spin, ...)."""

    mask: int
    current: int
    options: list[Any] = field(default_factory=list)
    info: int = 0  # temperature only: 0 normal, 1 eco, 2 cold

    @property
    def writable(self) -> bool:
        return bool(self.mask & 1)


@dataclass
class Extra:
    """One extra option of the selected program."""

    mask: int
    on: bool

    @property
    def offered(self) -> bool:
        return self.mask != 0

    @property
    def writable(self) -> bool:
        return bool(self.mask & 1)


@dataclass
class ProgramContext:
    """Parsed 2/1574 program context of the selected program."""

    prog_id: int
    temperature: Choice
    spin_rpm: Choice
    soiling: Choice
    extras: dict[str, Extra]


def mobile_start_armed(state: dict[str, Any]) -> bool:
    """Whether Mobile Start is armed: /State RemoteEnable third value, bit 0."""
    remote_enable = state.get("RemoteEnable") or []
    return len(remote_enable) >= 3 and bool(remote_enable[2] & 1)


def parse_program_list(fields: dict[int, list[Any]]) -> list[int]:
    """Program ids from 2/1584 (field 1 count, field 2 array of ids)."""
    ids = fields.get(2, [None, []])[1]
    return [int(i) for i in ids]


def _choice(para: dict[int, list[Any]], member: int) -> Choice:
    m = para[member][1]
    options = m[4][1] if m[4][0] in (0x11, 0x12) else m[5][1]
    return Choice(mask=m[1][1], current=m[2][1], options=list(options))


def parse_context(fields: dict[int, list[Any]]) -> ProgramContext:
    """Parse 2/1574 (a washing machine's GlobalPsContext)."""
    para = fields[3][1]
    prog = fields[6][1]

    temps = para[3][1]
    spins = para[5][1]
    n_t, n_s = temps[4][1], spins[4][1]
    soiling = _choice(para, 6)
    # Soiling options come zero-padded; only leading non-zero levels count.
    soiling.options = [v for v in soiling.options if v != 0]
    return ProgramContext(
        prog_id=prog[1][1],
        temperature=Choice(
            mask=temps[1][1],
            current=temps[2][1],
            info=temps[3][1],
            options=list(zip(temps[5][1][:n_t], temps[6][1][:n_t])),
        ),
        spin_rpm=Choice(
            mask=spins[1][1],
            current=spins[2][1] * 10,
            options=[v * 10 for v in spins[5][1][:n_s]],
        ),
        soiling=soiling,
        extras={
            name: Extra(mask=para[16][1][i][1][1][1], on=para[16][1][i][1][2][1])
            for name, i in EXTRAS.items()
            if i in para[16][1]
        },
    )


def temperature_label(value: int, info: int) -> str:
    """Label of a temperature option: "Cold", "20 °C", "40 °C Eco"."""
    if value == 0 or info == 2:
        return "Cold"
    if info == 1:
        return f"{value} °C Eco"
    return f"{value} °C"


def spin_label(rpm: int) -> str:
    """Label of a spin speed option."""
    return "No spin" if rpm == 0 else f"{rpm} rpm"


def validate_parameters(
    ctx: ProgramContext,
    *,
    temperature: int | None = None,
    eco: bool = False,
    spin_rpm: int | None = None,
    soiling: int | None = None,
    extras: dict[str, bool] | None = None,
) -> None:
    """Raise NotOfferedError for any value the program does not offer.

    Every message names the refused value and lists the offered ones; the
    strings reach the user verbatim.
    """
    if temperature is not None:
        label = temperature_label(temperature, 2 if temperature == 0 else (1 if eco else 0))
        offered = ", ".join(
            temperature_label(v, i) for v, i in ctx.temperature.options
        )
        if not ctx.temperature.writable:
            raise NotOfferedError(
                f"temperature {label} is not changeable for this program; "
                f"offered: {offered}"
            )
        info = 2 if temperature == 0 else (1 if eco else 0)
        if (temperature, info) not in ctx.temperature.options:
            raise NotOfferedError(
                f"temperature {label} is not offered for this program; "
                f"offered: {offered}"
            )
    if spin_rpm is not None:
        label = spin_label(spin_rpm)
        offered = ", ".join(spin_label(v) for v in ctx.spin_rpm.options)
        if not ctx.spin_rpm.writable:
            raise NotOfferedError(
                f"spin speed {label} is not changeable for this program; "
                f"offered: {offered}"
            )
        if spin_rpm not in ctx.spin_rpm.options:
            raise NotOfferedError(
                f"spin speed {label} is not offered for this program; "
                f"offered: {offered}"
            )
    if soiling is not None:
        offered = ", ".join(str(v) for v in ctx.soiling.options)
        if not ctx.soiling.writable:
            raise NotOfferedError(
                f"soiling level {soiling} is not changeable for this "
                f"program; offered: {offered}"
            )
        if soiling not in ctx.soiling.options:
            raise NotOfferedError(
                f"soiling level {soiling} is not offered for this program; "
                f"offered: {offered}"
            )
    for name, on in (extras or {}).items():
        if name not in EXTRAS:
            raise ValueError(f"unknown extra: {name!r}")
        extra = ctx.extras.get(name)
        offered = ", ".join(
            n for n, e in ctx.extras.items() if e.offered
        )
        if extra is None:
            raise NotOfferedError(
                f"option {name!r} is not available for this program; "
                f"offered: {offered}"
            )
        if on and not extra.offered:
            raise NotOfferedError(
                f"option {name!r} is not offered for this program; "
                f"offered: {offered}"
            )
        if on != extra.on and not extra.writable:
            raise NotOfferedError(
                f"option {name!r} is locked for this program and cannot be "
                f"changed; offered: {offered}"
            )


def build_parameters(
    sel_fields: dict[int, list[Any]],
    ctx_fields: dict[int, list[Any]],
    *,
    temperature: int | None = None,
    eco: bool = False,
    spin_rpm: int | None = None,
    soiling: int | None = None,
    extras: dict[str, bool] | None = None,
) -> dict[int, list[Any]]:
    """2/1577 with 2/1574's current values plus the user's changes.

    *sel_fields* is a fresh decode of 2/1577 (it is modified in place and
    returned). With no keyword arguments it copies the appliance values only,
    which is how a program's defaults are written.
    """
    ctx = parse_context(ctx_fields)
    if not ctx.prog_id:
        raise NoProgramError("the appliance shows no program to set")

    para_ctx = ctx_fields[3][1]
    sel_fields[1][1] = ctx.prog_id
    sel_fields[3][1] = 2  # parametrization
    para = sel_fields[6][1]

    # member in 2/1577 -> (struct id in 2/1574, field holding the current value)
    copied = {
        1: (1, 2),
        2: (2, 3),
        3: (3, 2),
        4: (3, 3),
        5: (5, 2),
        6: (6, 2),
        7: (7, 2),
        8: (8, 2),
        11: (11, 2),
        12: (14, 2),
        15: (17, 2),
        18: (18, 2),
        19: (19, 2),
        20: (20, 2),
    }
    for member, (src, fld) in copied.items():
        if member in para and src in para_ctx and fld in para_ctx[src][1]:
            para[member][1] = para_ctx[src][1][fld][1]
    if 14 in para and 16 in para_ctx:
        for i in para[14][1]:
            if i in para_ctx[16][1]:
                para[14][1][i][1] = para_ctx[16][1][i][1][2][1]
    if 13 in para and 15 in para_ctx:
        current = [c[3][1] for c in para_ctx[15][1][1][1]]
        cont = para[13][1][1][1]
        para[13][1][1][1] = [
            current[i] if i < len(current) else cont[i] for i in range(len(cont))
        ]

    if temperature is not None:
        para[3][1] = temperature
        para[4][1] = 2 if temperature == 0 else (1 if eco else 0)
    if spin_rpm is not None:
        para[5][1] = spin_rpm // 10
    if soiling is not None:
        para[6][1] = soiling
    for name, on in (extras or {}).items():
        member = EXTRAS[name]
        if member in para[14][1]:
            para[14][1][member][1] = bool(on)
    return sel_fields


def build_program_select(
    sel_fields: dict[int, list[Any]], prog_id: int
) -> dict[int, list[Any]]:
    """2/1577 fields that select a program (type 0), leaving the rest as read."""
    sel_fields[1][1] = prog_id
    sel_fields[3][1] = 0
    return sel_fields


def build_start_request(fields: dict[int, list[Any]]) -> dict[int, list[Any]]:
    """2/1577-style fields of 2/1583 that send user request 1 (start).

    Returns the modified fields; encode them with the header the leaf was
    read with.
    """
    fields[1][1] = USER_REQUEST_START
    return fields


def start_allowed(state: dict[str, Any]) -> bool:
    """Whether the app would offer start: Status 3 (programmed) under Mobile
    Start (koti Laundry::Rules#programmed?)."""
    return state.get("Status") == 3 and mobile_start_armed(state)


# ---------- wash record (pure, tested in tests/test_wash.py) ----------

# States of sensor.<machine>_wash_state.
WASH_NONE = "none"
WASH_RUNNING = "running"
WASH_FINISHED = "finished"
WASH_INTERRUPTED = "interrupted"

# /State Status codes in which a started wash keeps running (4 and 5 are
# verified on the WSI863; 6 "paused" is unverified and treated as still
# running). Every other code closes the record.
WASH_KEEP_RUNNING = (4, 5, 6)


@dataclass
class WashRecord:
    """What one wash ran with, frozen when the program began.

    Built from the last program context the integration saw while the washer
    was armed with a program selected; when the wash was started at the
    machine without the integration ever seeing a context, only program_id /
    program_name come from /State ProgramID and every other field stays
    None (or empty) — values are never invented.

    *started* and *finished* are UTC epoch seconds from the poll clock;
    *duration* is whole minutes and is set when the record closes.
    """

    state: str  # WASH_RUNNING / WASH_FINISHED / WASH_INTERRUPTED
    program_id: int | None
    program_name: str | None
    temperature: int | None  # °C; None = unknown (not seen by the integration)
    eco: bool  # the recorded temperature is the eco variant
    spin_rpm: int | None  # 0 = no spin; None = unknown
    soiling: int | None  # None = unknown or not offered
    extras: dict[str, bool]  # every extra the recorded program offered
    extras_offered: tuple[str, ...]  # offered extras, in EXTRAS order
    started: float
    finished: float | None
    duration: int | None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe copy for RestoreEntity storage."""
        return {
            "state": self.state,
            "program_id": self.program_id,
            "program_name": self.program_name,
            "temperature": self.temperature,
            "eco": self.eco,
            "spin_rpm": self.spin_rpm,
            "soiling": self.soiling,
            "extras": dict(self.extras),
            "extras_offered": list(self.extras_offered),
            "started": self.started,
            "finished": self.finished,
            "duration": self.duration,
        }

    @classmethod
    def from_dict(cls, data: Any) -> WashRecord | None:
        """Rebuild from to_dict(); anything unexpected gives None."""
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                state=str(data["state"]),
                program_id=_opt_int(data.get("program_id")),
                program_name=(
                    None if data.get("program_name") is None
                    else str(data["program_name"])
                ),
                temperature=_opt_int(data.get("temperature")),
                eco=bool(data.get("eco")),
                spin_rpm=_opt_int(data.get("spin_rpm")),
                soiling=_opt_int(data.get("soiling")),
                extras={str(k): bool(v) for k, v in (data.get("extras") or {}).items()},
                extras_offered=tuple(str(k) for k in (data.get("extras_offered") or ())),
                started=float(data["started"]),
                finished=(
                    None if data.get("finished") is None
                    else float(data["finished"])
                ),
                duration=_opt_int(data.get("duration")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _opt_int(value: Any) -> int | None:
    """int or None; floats and non-numbers give None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def wash_extras_label(record: WashRecord) -> str:
    """State of the wash extras sensor: readable names joined, or "None".

    Keys without a display name (e.g. from a tampered restore) fall back to
    the raw key instead of raising.
    """
    names = [EXTRA_NAMES.get(name, name) for name, on in record.extras.items() if on]
    return ", ".join(names) if names else "None"


@dataclass
class WashTracker:
    """The none/running/finished/interrupted state machine of one washer.

    Fed with every polled /State status and with every program context the
    coordinator managed to read. The newest context that shows a selected
    program is kept; it is consumed the moment a wash begins, so a context
    can never be attributed to a later wash. The machine dropping to off/on
    (Status 1/2) discards the context too: the selected program is gone.
    """

    record: WashRecord | None = None
    context: ProgramContext | None = None

    def note_context(self, context: ProgramContext | None) -> None:
        """Keep the newest context that shows a selected program."""
        if context is not None and context.prog_id:
            self.context = context

    def update(
        self,
        status: int,
        *,
        now: float,
        state: dict[str, Any] | None = None,
    ) -> WashRecord | None:
        """Feed one poll; returns the record to expose (None before a wash).

        *status* is the /State Status code, *now* the poll time in epoch
        seconds, *state* the raw /State mapping (used for the ProgramID
        fallback when no context was ever seen).
        """
        running = self.record is not None and self.record.state == WASH_RUNNING
        if status in (1, 2):
            # Off or on without a program: the selected program is gone, a
            # kept context belongs to the past.
            self.context = None
        if status in (4, 5) and not running:
            # Waiting to start or running with no open record: the wash
            # begins. Freeze the kept context (or the /State fallback).
            self.record = self._freeze(now=now, state=state)
        elif running and status not in WASH_KEEP_RUNNING:
            # The machine left running (and is not merely paused): close
            # the record. Status 7 (finished) is unverified on a real
            # machine; anything else that ends the run is "interrupted".
            self._close(status=status, now=now)
        return self.record

    def _freeze(self, *, now: float, state: dict[str, Any] | None) -> WashRecord:
        context = self.context
        self.context = None  # consumed: never attributed to a later wash
        if context is not None:
            offered = {
                name: extra.on for name, extra in context.extras.items()
                if extra.offered
            }
            return WashRecord(
                state=WASH_RUNNING,
                program_id=context.prog_id,
                program_name=program_name(context.prog_id),
                temperature=context.temperature.current,
                eco=context.temperature.info == 1,
                spin_rpm=context.spin_rpm.current,
                # A soiling current of 0 means the program offers no levels.
                soiling=context.soiling.current or None,
                extras=offered,
                extras_offered=tuple(offered),
                started=now,
                finished=None,
                duration=None,
            )
        # Started at the machine without the integration ever seeing a
        # context: record what /State carries, mark the rest unknown.
        program = None
        if state is not None:
            try:
                program = int(state.get("ProgramID"))
            except (TypeError, ValueError):
                program = None
            if program is not None and program <= 0:
                program = None
        return WashRecord(
            state=WASH_RUNNING,
            program_id=program,
            program_name=program_name(program) if program is not None else None,
            temperature=None,
            eco=False,
            spin_rpm=None,
            soiling=None,
            extras={},
            extras_offered=(),
            started=now,
            finished=None,
            duration=None,
        )

    def _close(self, *, status: int, now: float) -> None:
        record = self.record
        if record is None:
            return
        record.state = WASH_FINISHED if status == 7 else WASH_INTERRUPTED
        record.finished = now
        record.duration = max(0, int(round((now - record.started) / 60)))


# Read/write callables the session expects from the cloud layer.
ReadLeaf = Callable[[int, int, int, int], Awaitable[bytes | None]]
WriteLeaf = Callable[[int, int, bytes], Awaitable[None]]
Confirm = Callable[[], Awaitable[bool]]
# Provider of a fresh appliance state (GET /State, not the debounced poll).
StateCheck = Callable[[], Awaitable[dict[str, Any]]]


class WasherSession:
    """Serialized read-modify-write for one appliance.

    Pure Python (asyncio only) so the write path can be tested without Home
    Assistant. *read_leaf* and *write_leaf* are the cloud callables. Every
    operation takes the appliance lock before its first read and holds it
    through the write and until the write is confirmed (bounded): a change
    queued behind another then always reads the washer's newest values, so
    back-to-back changes cannot overwrite each other. The written leaf is
    encoded with the header the leaf was read with, as the Miele app does.
    """

    def __init__(
        self,
        read_leaf: ReadLeaf,
        write_leaf: WriteLeaf,
        *,
        confirm_timeout: float = WRITE_CONFIRM_TIMEOUT,
    ) -> None:
        self._read_leaf = read_leaf
        self._write_leaf = write_leaf
        self._confirm_timeout = confirm_timeout
        self._lock = asyncio.Lock()
        self._last_start: float | None = None

    async def select_program(self, prog_id: int, *, confirm: Confirm | None = None) -> None:
        """Write 2/1577 type 0 with the chosen program."""
        async with self._lock:
            raw = await self._read(LEAF_PROGRAM_SELECT, 0, 0)
            header, fields = decode(raw)
            leaf = encode(header, build_program_select(fields, prog_id))
            await self._write_and_confirm(LEAF_PROGRAM_SELECT, leaf, confirm)

    async def set_parameters(
        self,
        *,
        confirm: Confirm | None = None,
        temperature: int | None = None,
        eco: bool = False,
        spin_rpm: int | None = None,
        soiling: int | None = None,
        extras: dict[str, bool] | None = None,
    ) -> None:
        """Read 2/1577 + 2/1574 fresh, apply the one change set, write 2/1577."""
        async with self._lock:
            raw_sel = await self._read(LEAF_PROGRAM_SELECT, 0, 0)
            header, sel_fields = decode(raw_sel)
            raw_ctx = await self._read(LEAF_PROGRAM_CONTEXT, 0, 1)
            _ctx_header, ctx_fields = decode(raw_ctx)
            validate_parameters(
                parse_context(ctx_fields),
                temperature=temperature,
                eco=eco,
                spin_rpm=spin_rpm,
                soiling=soiling,
                extras=extras,
            )
            built = build_parameters(
                sel_fields,
                ctx_fields,
                temperature=temperature,
                eco=eco,
                spin_rpm=spin_rpm,
                soiling=soiling,
                extras=extras,
            )
            await self._write_and_confirm(LEAF_PROGRAM_SELECT, encode(header, built), confirm)

    async def start(
        self,
        *,
        confirm: Confirm | None = None,
        state_check: StateCheck | None = None,
    ) -> None:
        """Write user request 1 (start) to 2/1583.

        *state_check* must return a FRESH appliance state read from the
        cloud (not the debounced poll); it runs under the appliance lock,
        immediately before the write, so the "program selected and Mobile
        Start armed" guard sees the washer's newest state. A second start
        within START_COOLDOWN of one that was possibly sent is refused:
        Mobile Start is spent once per arming, and a duplicate costs the
        owner a trip to the machine.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            if (
                self._last_start is not None
                and loop.time() - self._last_start < START_COOLDOWN
            ):
                raise StartRefusedError(
                    "a start was already sent to this washer a moment ago; "
                    "wait, check the machine, and try again only if it did "
                    "not begin"
                )
            if state_check is not None:
                state = await state_check()
                if not start_allowed(state):
                    raise StartRefusedError(
                        "start is only possible while a program is selected "
                        "and Mobile Start is armed; check the washer"
                    )
            raw = await self._read(LEAF_USER_REQUEST, 0, 0)
            header, fields = decode(raw)
            try:
                await self._write_and_confirm(
                    LEAF_USER_REQUEST,
                    encode(header, build_start_request(fields)),
                    confirm,
                )
            except BaseException:
                # The start may have reached the washer even when the cloud
                # then errored or the confirmation timed out; block a quick
                # retry either way.
                self._last_start = loop.time()
                raise
            self._last_start = loop.time()

    async def _read(self, leaf: tuple[int, int], idx1: int, idx2: int) -> bytes:
        raw = await self._read_leaf(leaf[0], leaf[1], idx1, idx2)
        if raw is None:
            raise NoProgramError(
                "the appliance shows no program data: switch it on and arm Mobile Start"
            )
        return raw

    async def _write_and_confirm(
        self, leaf: tuple[int, int], payload: bytes, confirm: Confirm | None
    ) -> None:
        await self._write_leaf(leaf[0], leaf[1], payload)
        if confirm is None:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._confirm_timeout
        while True:
            await asyncio.sleep(_CONFIRM_POLL_INTERVAL)
            if await self._poll(confirm):
                return
            if loop.time() >= deadline:
                _LOGGER.debug(
                    "the washer did not confirm the write to %s/%s "
                    "within %.0f s",
                    leaf[0],
                    leaf[1],
                    self._confirm_timeout,
                )
                raise WriteNotConfirmedError(
                    "the washer did not confirm the change within "
                    f"{self._confirm_timeout:.0f} seconds; check the machine "
                    "whether it was applied"
                )

    @staticmethod
    async def _poll(confirm: Confirm) -> bool:
        try:
            return bool(await confirm())
        except Exception:  # noqa: BLE001 - a hiccup means "not yet"
            _LOGGER.debug("confirmation poll failed", exc_info=True)
            return False
