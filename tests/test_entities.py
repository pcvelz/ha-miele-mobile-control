"""Pure tests for the entity-side helpers and the translation files.

Everything here imports only the HA-free modules (const, washer) plus the
two JSON translation files, so it runs under the stubbed package in
conftest.py without Home Assistant installed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from custom_components.miele_mobile_control import const, washer

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "miele_mobile_control"


# ---------- status mapping ----------

def test_status_names_verified_codes():
    assert const.status_name(1) == "off"
    assert const.status_name(2) == "on"
    assert const.status_name(3) == "programmed"
    assert const.status_name(4) == "waiting_to_start"
    assert const.status_name(5) == "running"
    assert const.status_name(6) == "paused"
    assert const.status_name(7) == "finished"
    assert const.status_name(8) == "failure"


def test_status_name_unknown_and_garbage():
    assert const.status_name(0) == "unknown"
    assert const.status_name(9) == "unknown"
    assert const.status_name(None) == "unknown"
    assert const.status_name("x") == "unknown"


def test_status_states_cover_every_mapped_code():
    mapped = set(const.STATUS_CODES.values())
    options = set(const.STATUS_STATES)
    assert mapped <= options
    # every option except "unknown" must be a real Status code
    assert options - mapped == {"unknown"}


# ---------- remaining time / finish time ----------

def test_remaining_minutes():
    assert const.remaining_minutes({"RemainingTime": [1, 30]}) == 90
    assert const.remaining_minutes({"RemainingTime": [0, 5]}) == 5
    assert const.remaining_minutes({"RemainingTime": [0, 0]}) == 0
    assert const.remaining_minutes({"RemainingTime": [2]}) is None
    assert const.remaining_minutes({}) is None
    assert const.remaining_minutes({"RemainingTime": "x"}) is None
    assert const.remaining_minutes({"RemainingTime": [None, None]}) is None


def test_finish_time():
    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    assert const.finish_time({"RemainingTime": [1, 30]}, now) == now + timedelta(
        minutes=90
    )
    # no countdown known -> no finish time
    assert const.finish_time({}, now) is None
    # countdown at zero -> no finish time (nothing running)
    assert const.finish_time({"RemainingTime": [0, 0]}, now) is None


# ---------- live readings of the running program ----------


def test_elapsed_minutes():
    assert const.elapsed_minutes({"ElapsedTime": [1, 30]}) == 90
    assert const.elapsed_minutes({"ElapsedTime": [0, 0]}) == 0
    assert const.elapsed_minutes({"ElapsedTime": [2]}) is None
    assert const.elapsed_minutes({}) is None
    assert const.elapsed_minutes({"ElapsedTime": "x"}) is None


def test_start_time():
    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    assert const.start_time({"StartTime": [1, 30]}, now) == now + timedelta(
        minutes=90
    )
    # no delayed start set
    assert const.start_time({"StartTime": [0, 0]}, now) is None
    assert const.start_time({}, now) is None


def test_target_temperature():
    # centi-°C, measured on the WSI863 with Cottons 20 °C selected
    assert const.target_temperature({"TargetTemperature": [2000, -32768, -32768]}) == 20.0
    # the -32768 "no value" marker means none
    assert const.target_temperature({"TargetTemperature": [-32768, -32768]}) is None
    assert const.target_temperature({"TargetTemperature": []}) is None
    assert const.target_temperature({}) is None


def test_spin_speed():
    assert const.spin_speed({"SpinningSpeed": 1400}) == 1400
    assert const.spin_speed({"SpinningSpeed": 0}) == 0
    assert const.spin_speed({}) is None
    assert const.spin_speed({"SpinningSpeed": "1400"}) is None


def test_program_phase():
    # raw number, no names invented
    assert const.program_phase({"ProgramPhase": 260}) == 260
    assert const.program_phase({"ProgramPhase": 0}) == 0
    assert const.program_phase({}) is None


# ---------- translation files ----------

def _load_json(name: str) -> dict:
    return json.loads((COMPONENT_DIR / name).read_text())


def test_strings_and_translations_identical():
    """HA renders translations/<lang>.json at runtime; strings.json feeds the
    translation tooling. Keep them equal so they cannot drift."""
    assert _load_json("strings.json") == _load_json("translations/en.json")


def test_every_extra_switch_has_translation():
    strings = _load_json("strings.json")
    switch_names = strings["entity"]["switch"]
    for extra in washer.EXTRAS:
        key = f"extra_{extra}"
        assert key in switch_names, f"missing translation for switch {key}"
        assert switch_names[key]["name"]


def test_miele_display_names():
    names = _load_json("strings.json")["entity"]["switch"]
    assert names["extra_gentle"]["name"] == "Extra gentle"
    assert names["extra_steam_smoothing"]["name"] == "Pre-ironing"
    assert names["extra_rinsing_plus"]["name"] == "Extra rinse"
    assert names["extra_single"]["name"] == "SingleWash"


def test_new_entities_translated():
    strings = _load_json("strings.json")["entity"]
    assert strings["sensor"]["status"]["name"] == "Status"
    assert strings["sensor"]["remaining_time"]["name"] == "Remaining time"
    assert strings["sensor"]["finish_time"]["name"] == "Finish time"
    assert strings["binary_sensor"]["mobile_start"]["name"] == "Mobile Start"
    assert strings["binary_sensor"]["door"]["name"] == "Door"
    # every enum state of the status sensor must be translated
    states = strings["sensor"]["status"]["state"]
    for state in const.STATUS_STATES:
        assert state in states, f"status state {state!r} has no translation"


def test_wash_record_entities_translated():
    strings = _load_json("strings.json")["entity"]
    sensors = strings["sensor"]
    for key in (
        "wash_state",
        "wash_program",
        "wash_temperature",
        "wash_spin_speed",
        "wash_soiling",
        "wash_extras",
        "wash_started",
        "wash_finished",
        "wash_duration",
    ):
        assert sensors[key]["name"], f"missing sensor translation {key}"
    # every enum state of the wash state sensor must be translated
    states = sensors["wash_state"]["state"]
    for state in ("none", "running", "finished", "interrupted"):
        assert state in states, f"wash state {state!r} has no translation"


def test_live_reading_entities_translated():
    strings = _load_json("strings.json")["entity"]["sensor"]
    for key in (
        "target_temperature",
        "current_spin_speed",
        "elapsed_time",
        "program_phase",
        "start_time",
    ):
        assert strings[key]["name"], f"missing sensor translation {key}"


def test_every_wash_extra_binary_sensor_has_translation():
    strings = _load_json("strings.json")["entity"]["binary_sensor"]
    for extra in washer.EXTRAS:
        key = f"wash_{extra}"
        assert key in strings, f"missing translation for binary_sensor {key}"
        assert strings[key]["name"]


def test_start_field_warns_about_mobile_start():
    fields = _load_json("strings.json")["services"]["set_program"]["fields"]
    description = fields["start"]["description"]
    assert "cannot be undone" in description
    assert "Mobile Start" in description


# ---------- Program select availability (the first-run rule) ----------


def test_program_select_available_when_armed_on_and_list_known():
    # The first thing a new user does: washer on and armed, no program
    # chosen yet. The machine publishes its program list in this state, so
    # the Program select must be available (current option None then).
    assert const.program_select_available(True, 2, [1, 3]) is True
    assert const.program_select_available(True, 3, [1, 3]) is True


def test_program_select_unavailable_without_list_or_armed_or_off():
    assert const.program_select_available(True, 2, []) is False  # no list
    assert const.program_select_available(False, 2, [1]) is False  # not armed
    assert const.program_select_available(True, 1, [1]) is False  # off
    assert const.program_select_available(True, 5, [1]) is False  # running


# ---------- start button cooldown ----------


def test_within_cooldown_blocks_double_press():
    assert const.within_cooldown(None, 1000.0) is False
    assert const.within_cooldown(1000.0, 1000.0) is True
    assert const.within_cooldown(1000.0, 1029.9) is True
    assert const.within_cooldown(1000.0, 1030.0) is False
    assert const.within_cooldown(1000.0, 2000.0) is False
