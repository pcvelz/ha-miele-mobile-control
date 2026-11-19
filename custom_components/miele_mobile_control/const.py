"""Constants for the Miele Mobile Control integration."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final, Mapping

from . import washer

DOMAIN: Final = "miele_mobile_control"
NAME: Final = "Miele Mobile Control"
MANUFACTURER: Final = "Miele"

CONF_COUNTRY: Final = "country"
DEFAULT_COUNTRY: Final = "NL"

# /State poll interval.
UPDATE_INTERVAL: Final = timedelta(seconds=30)

# After a write, give the appliance a moment, then refresh.
POST_WRITE_DELAY: Final = 2.0

PLATFORMS: Final = ["binary_sensor", "button", "select", "sensor", "switch"]

# Service schema keys.
ATTR_PROGRAM: Final = "program"
ATTR_TEMPERATURE: Final = "temperature"
ATTR_ECO: Final = "eco"
ATTR_SPIN_SPEED: Final = "spin_speed"
ATTR_SOILING: Final = "soiling"
ATTR_EXTRAS: Final = "extras"
ATTR_START: Final = "start"

SERVICE_SET_PROGRAM: Final = "set_program"

# /State Status codes (verified on the WSI863) -> states of the status
# sensor. The sensor must also make sense while Mobile Start is not armed,
# so this mapping lives here, not in the write path.
#
# Codes 1-5 were observed on the real machine. Codes 6, 7 and 8 are taken
# from Miele's public 3rd Party API documentation and are UNVERIFIED on a
# real appliance; every code not in this mapping (including 0) reports as
# "unknown".
STATUS_CODES: Final = {
    1: "off",
    2: "on",
    3: "programmed",
    4: "waiting_to_start",
    5: "running",
    # Unverified, from Miele's public API docs:
    6: "paused",
    7: "finished",
    8: "failure",
}

# Every value the status sensor can report (options of its enum device
# class, in display order).
STATUS_STATES: Final = (
    "off",
    "on",
    "programmed",
    "waiting_to_start",
    "running",
    "paused",
    "finished",
    "failure",
    "unknown",
)


def status_name(code: Any) -> str:
    """Enum state for a /State Status code; unknown codes map to "unknown"."""
    try:
        number = int(code)
    except (TypeError, ValueError):
        return "unknown"
    return STATUS_CODES.get(number, "unknown")


def remaining_minutes(state: Mapping[str, Any]) -> int | None:
    """Remaining program time in minutes from RemainingTime [h, m].

    None when the appliance reports no countdown (e.g. switched off or no
    program selected). A reported [0, 0] is a real 0, not "no data".
    """
    remaining = state.get("RemainingTime")
    if not isinstance(remaining, (list, tuple)) or len(remaining) < 2:
        return None
    try:
        hours, minutes = int(remaining[0]), int(remaining[1])
    except (TypeError, ValueError):
        return None
    return hours * 60 + minutes


def finish_time(state: Mapping[str, Any], now: datetime) -> datetime | None:
    """Estimated finish moment: now + remaining time.

    None when no remaining time is known or the countdown has reached zero.
    """
    remaining = remaining_minutes(state)
    if not remaining:
        return None
    return now + timedelta(minutes=remaining)


def elapsed_minutes(state: Mapping[str, Any]) -> int | None:
    """Elapsed program time in minutes from ElapsedTime [h, m].

    None when the appliance reports no elapsed time. A reported [0, 0] is a
    real 0 (the program just started), not "no data".
    """
    return _hm_minutes(state.get("ElapsedTime"))


def start_time(state: Mapping[str, Any], now: datetime) -> datetime | None:
    """Delayed start moment: now + StartTime, when one is set.

    None when StartTime is [0, 0] or missing — the machine starts
    immediately or not at all.
    """
    delay = _hm_minutes(state.get("StartTime"))
    if not delay:
        return None
    return now + timedelta(minutes=delay)


def target_temperature(state: Mapping[str, Any]) -> float | None:
    """Target wash temperature in °C: TargetTemperature[0] / 100.

    None when the slot is absent or carries the -32768 "no value" marker.
    """
    return _centi_value(state.get("TargetTemperature"))


def spin_speed(state: Mapping[str, Any]) -> int | None:
    """Current spin speed in rpm (State.SpinningSpeed); None when absent."""
    value = state.get("SpinningSpeed")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def program_phase(state: Mapping[str, Any]) -> int | None:
    """Raw State.ProgramPhase number; None when absent.

    The numbers are published without names, so this is exposed as a
    diagnostic sensor and no phase names are invented.
    """
    value = state.get("ProgramPhase")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _hm_minutes(value: Any) -> int | None:
    """h/min pair as total minutes; None when not a usable pair."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        hours, minutes = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    return hours * 60 + minutes


def _centi_value(value: Any) -> float | None:
    """First slot of a centi-unit array as a float; None when absent."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        number = float(value[0])
    except (TypeError, ValueError):
        return None
    if number == -32768:
        return None
    return number / 100


# ---------- set_program action helpers (pure, tested in tests/test_action.py) ----------


def build_changes(
    *,
    temperature: int | None = None,
    eco: bool = False,
    spin_rpm: int | None = None,
    soiling: int | None = None,
    extras: list[str] | None = None,
) -> dict[str, Any]:
    """Turn action data into a washer change dict, checking everything that
    can be checked without the program's context.

    Raises ValueError for an eco request without a temperature (the eco
    variant cannot be told apart) and for extra names the washer does not
    know. Offered-value checks happen later, against the program context.
    """
    changes: dict[str, Any] = {}
    if temperature is not None:
        changes["temperature"] = temperature
        changes["eco"] = eco
    elif eco:
        raise ValueError("eco needs a temperature to disambiguate the eco variant")
    if spin_rpm is not None:
        changes["spin_rpm"] = spin_rpm
    if soiling is not None:
        changes["soiling"] = soiling
    if extras is not None:
        unknown = [name for name in extras if name not in washer.EXTRAS]
        if unknown:
            raise ValueError(f"unknown extras: {', '.join(unknown)}")
    return changes


def extras_change(requested: list[str], context: Any) -> dict[str, bool]:
    """The extras write for the action: every listed extra goes on, every
    other extra the program offers goes off (koti's chosen()).

    An extra is only written OFF when the program offers it AND it is
    writable; writing a locked or unoffered extra off is refused by the
    washer. A listed extra is always written ON — washer.validate_parameters
    then refuses it by name when the program does not offer it or it is
    locked, before anything is sent.
    """
    requested_set = set(requested)
    changes: dict[str, bool] = {}
    for name in washer.WRITTEN_EXTRAS:
        option = (context.extras or {}).get(name)
        if name in requested_set:
            changes[name] = True
        elif option is not None and option.offered and option.writable:
            changes[name] = False
    return changes


def plan_set_program(
    *,
    program_value: Any,
    program_ids: list[int],
    context: Any,
    temperature: int | None = None,
    eco: bool = False,
    spin_rpm: int | None = None,
    soiling: int | None = None,
    extras: list[str] | None = None,
) -> tuple[int, dict[str, Any], bool]:
    """Decide what the set_program action writes and in which order.

    Returns (program id, changes, select_first). When the requested program
    is already selected, every option is validated against the current
    context before anything is written and select_first is False. When it is
    a different program, the options can only be validated after the program
    change, so select_first is True and the caller validates the returned
    changes against the new context before writing them.

    Raises ValueError (or washer.NotOfferedError) before any write when the
    program is unknown, an extra name is unknown, or an option is not
    offered by the already-selected program.
    """
    pid = washer.program_id(program_value, program_ids or None)
    changes = build_changes(
        temperature=temperature,
        eco=eco,
        spin_rpm=spin_rpm,
        soiling=soiling,
        extras=extras,
    )
    if context is not None and context.prog_id == pid:
        # Same program: everything is validated now, before any write.
        if extras is not None:
            changes["extras"] = extras_change(extras, context)
        washer.validate_parameters(context, **changes)
        return pid, changes, False
    return pid, changes, True


def options_rejected_after_program_change(pid: int, err: ValueError) -> str:
    """The message when validation fails only after the program was already
    changed: say exactly that, so the user knows the state of the machine."""
    return (
        f"the program was changed to {washer.program_name(pid)}, but the "
        f"options were not applied: {err}. The program stays selected; set "
        "the options separately."
    )


def verify_written(context: Any, changes: Mapping[str, Any]) -> list[str]:
    """One human-readable problem per change the appliance did not keep.

    Empty list means everything stuck. The strings reach the user verbatim.
    """
    problems: list[str] = []
    temp = changes.get("temperature")
    if temp is not None:
        if context.temperature.current != temp:
            problems.append(
                f"the appliance kept temperature {context.temperature.current} °C"
            )
        elif changes.get("eco") and context.temperature.info != 1:
            problems.append("the appliance kept the non-eco temperature variant")
    spin = changes.get("spin_rpm")
    if spin is not None and context.spin_rpm.current != spin:
        problems.append(
            f"the appliance kept spin speed {context.spin_rpm.current} rpm"
        )
    soiling = changes.get("soiling")
    if soiling is not None and context.soiling.current != soiling:
        problems.append(
            f"the appliance kept soiling level {context.soiling.current}"
        )
    for name, on in (changes.get("extras") or {}).items():
        extra = context.extras.get(name)
        if extra is None or extra.on != on:
            problems.append(
                f"the appliance kept option {name!r} {'off' if on else 'on'}"
            )
    return problems


# ---------- entity availability rules (pure, tested in tests/test_entities.py) ----------


def program_select_available(armed: bool, status: int, program_ids: list[int]) -> bool:
    """The Program select works whenever the washer is armed and on, even
    before a program is chosen: the machine publishes its program list in
    that state. The option selects and extras still need a selected program.
    """
    return armed and status in (2, 3) and bool(program_ids)


def within_cooldown(
    pressed_at: float | None, now: float, period: float = washer.START_COOLDOWN
) -> bool:
    """The start button stays unavailable this long after a press, so a
    double press cannot send the start twice (Mobile Start is spent once).
    Same number as the coordinator's start cooldown."""
    return pressed_at is not None and now - pressed_at < period
