"""Wash-record tests: the none/running/finished/interrupted state machine.

Pure Python like test_washer.py: the tracker is fed /State Status codes and
program contexts parsed from the captured WSI863 leaves, and the expected
WashRecord comes out. Everything after "program selected" (Status 4 and 5)
is covered by these tests only — no real wash has been observed yet.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.miele_mobile_control.dop2 import decode
from custom_components.miele_mobile_control import washer

REPO_ROOT = Path(__file__).resolve().parents[1]

NOW = 1_700_000_000.0


def cottons_ctx(fixtures):
    """Parsed Cottons context, as read while armed and program selected."""
    return washer.parse_context(decode(fixtures["cottons_selected_2_1574_0_1"])[1])


def offered_names(context):
    return tuple(n for n, e in context.extras.items() if e.offered)


# ---------- extras naming ----------


def test_extra_names_match_switch_translations():
    """EXTRA_NAMES and the switch names in strings.json are one source."""
    strings = json.loads(
        (REPO_ROOT / "custom_components" / "miele_mobile_control" / "strings.json").read_text()
    )
    switch_names = strings["entity"]["switch"]
    assert set(washer.EXTRA_NAMES) == set(washer.EXTRAS)
    for name, label in washer.EXTRA_NAMES.items():
        assert switch_names[f"extra_{name}"]["name"] == label


def test_wash_extras_label():
    record = washer.WashRecord(
        state=washer.WASH_RUNNING,
        program_id=1,
        program_name="Cottons",
        temperature=40,
        eco=False,
        spin_rpm=1400,
        soiling=None,
        extras={"soak": True, "extra_quiet": True, "quick": False},
        extras_offered=("quick", "soak", "extra_quiet"),
        started=NOW,
        finished=None,
        duration=None,
    )
    # readable names, in EXTRAS order, selected only
    assert washer.wash_extras_label(record) == "Soak, Extra quiet"
    record.extras = {}
    assert washer.wash_extras_label(record) == "None"


# ---------- record (de)serialization ----------


def _record(**overrides):
    data = dict(
        state=washer.WASH_RUNNING,
        program_id=1,
        program_name="Cottons",
        temperature=40,
        eco=True,
        spin_rpm=1400,
        soiling=2,
        extras={"soak": True, "quick": False},
        extras_offered=("quick", "soak"),
        started=NOW,
        finished=None,
        duration=None,
    )
    data.update(overrides)
    return washer.WashRecord(**data)


def test_record_round_trips_through_dict():
    record = _record()
    restored = washer.WashRecord.from_dict(record.to_dict())
    assert restored == record
    # JSON-safe: no tuples left that json would turn into lists silently
    json.dumps(record.to_dict())
    closed = _record(
        state=washer.WASH_FINISHED, finished=NOW + 3600, duration=60
    )
    assert washer.WashRecord.from_dict(closed.to_dict()) == closed


def test_record_from_dict_rejects_garbage():
    assert washer.WashRecord.from_dict(None) is None
    assert washer.WashRecord.from_dict({}) is None
    assert washer.WashRecord.from_dict({"state": "running"}) is None
    assert washer.WashRecord.from_dict("running") is None


def test_record_from_dict_keeps_unknown_fields_none():
    record = washer.WashRecord.from_dict(
        {
            "state": "running",
            "program_id": 1,
            "program_name": "Cottons",
            "temperature": None,
            "eco": False,
            "spin_rpm": None,
            "soiling": None,
            "extras": {},
            "extras_offered": [],
            "started": NOW,
            "finished": None,
            "duration": None,
        }
    )
    assert record is not None
    assert record.temperature is None and record.spin_rpm is None


# ---------- the tracker ----------


def test_no_wash_before_the_first_run():
    tracker = washer.WashTracker()
    assert tracker.update(1, now=NOW) is None
    assert tracker.update(2, now=NOW) is None
    assert tracker.update(3, now=NOW, state={"Status": 3}) is None
    assert tracker.record is None


def test_freeze_at_waiting_to_start(fixtures):
    tracker = washer.WashTracker()
    context = cottons_ctx(fixtures)
    tracker.note_context(context)
    record = tracker.update(4, now=NOW, state={"Status": 4, "ProgramID": 1})
    assert record is not None
    assert record.state == washer.WASH_RUNNING
    assert record.started == NOW
    assert record.finished is None and record.duration is None
    assert record.program_id == 1
    assert record.program_name == "Cottons"
    assert record.temperature == 40
    assert record.eco is False
    assert record.spin_rpm == 1600
    assert record.extras_offered == offered_names(context)
    assert set(record.extras) == set(offered_names(context))


def test_freeze_at_running_without_waiting(fixtures):
    """Status 5 seen first (a poll missed Status 4): freeze just the same."""
    tracker = washer.WashTracker()
    tracker.note_context(cottons_ctx(fixtures))
    record = tracker.update(5, now=NOW, state={"Status": 5, "ProgramID": 1})
    assert record is not None and record.state == washer.WASH_RUNNING


def test_freeze_uses_the_newest_context(fixtures):
    tracker = washer.WashTracker()
    tracker.note_context(cottons_ctx(fixtures))
    tracker.note_context(None)  # a hiccup does not drop the kept context
    tracker.note_context(cottons_ctx(fixtures))
    record = tracker.update(5, now=NOW, state={"Status": 5})
    assert record.program_id == 1


def test_running_keeps_context_updates_from_being_reattributed(fixtures):
    """Context read in Status 4 belongs to the next wash, not this one."""
    tracker = washer.WashTracker()
    tracker.note_context(cottons_ctx(fixtures))
    first = tracker.update(4, now=NOW, state={"Status": 4})
    assert first is not None
    # machine runs; a stale armed read in Status 4 must not reopen a record
    assert tracker.update(5, now=NOW + 60, state={"Status": 5}) is first
    assert tracker.update(5, now=NOW + 90, state={"Status": 5}) is first


def test_new_wash_replaces_the_record(fixtures):
    tracker = washer.WashTracker()
    tracker.note_context(cottons_ctx(fixtures))
    first = tracker.update(5, now=NOW, state={"Status": 5})
    tracker.update(2, now=NOW + 3600, state={"Status": 2})
    assert first.state == washer.WASH_INTERRUPTED
    tracker.note_context(cottons_ctx(fixtures))
    second = tracker.update(4, now=NOW + 7200, state={"Status": 4})
    assert second is not first
    assert second.started == NOW + 7200


def test_finished_closes_with_duration():
    tracker = washer.WashTracker()
    record = tracker.update(4, now=NOW, state={"Status": 4})
    closed = tracker.update(7, now=NOW + 90 * 60, state={"Status": 7})
    assert closed is record
    assert closed.state == washer.WASH_FINISHED
    assert closed.finished == NOW + 90 * 60
    assert closed.duration == 90
    # stays closed while the machine sits at "finished"
    assert tracker.update(7, now=NOW + 95 * 60, state={"Status": 7}) is closed


def test_duration_counts_from_the_real_start_not_from_the_timer():
    """Measured on a real wash: the washer waited on its own timer from 12:01
    to 12:15 and washed until 15:57. The wash took 222 minutes, not 236."""
    tracker = washer.WashTracker()
    record = tracker.update(4, now=NOW, state={"Status": 4})
    assert record.waiting is True
    tracker.update(4, now=NOW + 7 * 60, state={"Status": 4})
    tracker.update(5, now=NOW + 14 * 60, state={"Status": 5})
    assert record.waiting is False
    assert record.started == NOW + 14 * 60
    # a pause and the next running poll do not move the start again
    tracker.update(6, now=NOW + 20 * 60, state={"Status": 6})
    tracker.update(5, now=NOW + 21 * 60, state={"Status": 5})
    assert record.started == NOW + 14 * 60
    closed = tracker.update(7, now=NOW + (14 + 222) * 60, state={"Status": 7})
    assert closed.state == washer.WASH_FINISHED
    assert closed.duration == 222


def test_waiting_survives_a_restart():
    tracker = washer.WashTracker()
    record = tracker.update(4, now=NOW, state={"Status": 4})
    restored = washer.WashRecord.from_dict(record.to_dict())
    assert restored.waiting is True
    tracker = washer.WashTracker()
    tracker.record = restored
    tracker.update(5, now=NOW + 14 * 60, state={"Status": 5})
    assert restored.started == NOW + 14 * 60 and restored.waiting is False
    # a record stored before this field existed counts as already begun
    old = record.to_dict()
    del old["waiting"]
    assert washer.WashRecord.from_dict(old).waiting is False


def test_interrupting_statuses_close_the_record():
    for status in (1, 2, 3, 8, 9, 0):
        tracker = washer.WashTracker()
        tracker.update(5, now=NOW, state={"Status": 5})
        closed = tracker.update(status, now=NOW + 10 * 60, state={"Status": status})
        assert closed.state == washer.WASH_INTERRUPTED, f"status {status}"
        assert closed.duration == 10
        assert closed.finished == NOW + 10 * 60


def test_paused_does_not_close_the_record():
    tracker = washer.WashTracker()
    record = tracker.update(5, now=NOW, state={"Status": 5})
    assert tracker.update(6, now=NOW + 5 * 60, state={"Status": 6}) is record
    assert tracker.update(5, now=NOW + 6 * 60, state={"Status": 5}) is record


def test_cancelled_waiting_closes_as_interrupted(fixtures):
    """Waiting to start, then back to program selected: the wash never ran."""
    tracker = washer.WashTracker()
    tracker.note_context(cottons_ctx(fixtures))
    record = tracker.update(4, now=NOW, state={"Status": 4})
    closed = tracker.update(3, now=NOW + 60, state={"Status": 3})
    assert closed is record
    assert closed.state == washer.WASH_INTERRUPTED
    assert closed.duration == 1


def test_started_at_the_machine_records_program_id_only():
    """No context ever seen: /State ProgramID is recorded, the rest unknown."""
    tracker = washer.WashTracker()
    record = tracker.update(
        5, now=NOW, state={"Status": 5, "ProgramID": 146}
    )
    assert record.program_id == 146
    assert record.program_name == "QuickPowerWash"
    assert record.temperature is None
    assert record.eco is False
    assert record.spin_rpm is None
    assert record.soiling is None
    assert record.extras == {}
    assert record.extras_offered == ()


def test_started_at_the_machine_without_program_id():
    tracker = washer.WashTracker()
    record = tracker.update(5, now=NOW, state={"Status": 5})
    assert record.program_id is None
    assert record.program_name is None
    # ProgramID 0 means "no program": nothing is invented
    tracker2 = washer.WashTracker()
    record2 = tracker2.update(5, now=NOW, state={"Status": 5, "ProgramID": 0})
    assert record2.program_id is None


def test_stale_context_is_discarded_when_the_program_is_gone(fixtures):
    """A kept context must not leak into a wash started days later."""
    tracker = washer.WashTracker()
    tracker.note_context(cottons_ctx(fixtures))
    tracker.update(1, now=NOW, state={"Status": 1})  # switched off
    record = tracker.update(5, now=NOW + 86400, state={"Status": 5, "ProgramID": 3})
    assert record.program_id == 3  # from /State, not the stale context
    assert record.temperature is None


def test_context_is_consumed_by_the_freeze(fixtures):
    tracker = washer.WashTracker()
    tracker.note_context(cottons_ctx(fixtures))
    tracker.update(4, now=NOW, state={"Status": 4})
    tracker.update(2, now=NOW + 3600, state={"Status": 2})
    # second wash at the machine, no new context: ProgramID fallback again
    record = tracker.update(5, now=NOW + 7200, state={"Status": 5, "ProgramID": 8})
    assert record.program_id == 8
    assert record.temperature is None


def test_restored_running_record_closes_with_real_times(fixtures):
    """Restart mid-run: the restored record keeps the original start."""
    tracker = washer.WashTracker(record=_record(started=NOW - 1800))
    closed = tracker.update(7, now=NOW + 1800, state={"Status": 7})
    assert closed.state == washer.WASH_FINISHED
    assert closed.started == NOW - 1800
    assert closed.duration == 60
    assert closed.program_name == "Cottons"
