"""Measured power profiles: per-minute recording of a wash and the matrix.

The owner's words this implements:

    "have a way to measure the program … empirical, so that it actually
    refers to the previous runs. This moves a bit of the logic to the
    integration, so the integration needs to store a matrix of all the
    possible things. We have to see which settings affect the power usage …
    we are going to be reading the 1 minute precise power measurements FROM
    the integration"

    "the liquid selection does not affect the program power usage, so you
    don't have to keep a special delta of that in the matrix. But each extra
    function selected should definitely keep a unique combination in the
    matrix and the integration should be storing this, and also it should be
    persisting."

    "in configuration of the integration we can select a plug to measure the
    usage from. We should have a TDD for both the scenario with the plug and
    without the plug. Ideally we should focus on the scenario with the power
    measuring plug."

Pure Python: profile.py has no Home Assistant import.
"""

from __future__ import annotations

import csv
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from custom_components.miele_mobile_control import profile, washer

S = 1_700_000_000.0  # the moment the washer starts running (Status 5)
FIXTURE_CSV = Path(__file__).resolve().parent / "fixtures" / "socket_power_first_wash.csv"


def record(**overrides) -> washer.WashRecord:
    """A frozen wash record: Cottons 20 °C, 1600 rpm, soiling 2."""
    values = dict(
        state=washer.WASH_RUNNING,
        program_id=1,
        program_name="Cottons",
        temperature=20,
        eco=False,
        spin_rpm=1600,
        soiling=2,
        extras={"quick": False, "gentle": False, "extra_quiet": False},
        extras_offered=("quick", "gentle", "extra_quiet"),
        started=S - 600,
        finished=None,
        duration=None,
        twindos=[1, 0],
        detergent=True,
    )
    values.update(overrides)
    return washer.WashRecord(**values)


def run_wash(samples, *, end, end_status=7, rec=None, energy=None, waiting_from=None):
    """Drive a recorder: optional waiting (4), running from S, close at end.

    *samples* are (time, watts or None); *energy* (time, kWh) pairs.
    Events are applied in time order; the status changes at S and end.
    """
    rec = rec or record()
    recorder = profile.PowerRecorder()
    events = [(t, 1, "power", v) for t, v in samples]
    events += [(t, 1, "energy", v) for t, v in (energy or [])]
    if waiting_from is not None:
        events.append((waiting_from, 2, "status", 4))
    events.append((S, 2, "status", 5))
    events.append((end, 0, "status", end_status))
    result = None
    for t, _, kind, value in sorted(events, key=lambda e: (e[0], e[1])):
        if kind == "power":
            recorder.power(t, value)
        elif kind == "energy":
            recorder.energy(t, value)
        else:
            out = recorder.status(value, now=t, record=rec)
            if out is not None:
                result = out
    return recorder, result


# ---------- WITH the plug: per-minute averaging ----------


def test_time_weighted_mean_on_irregular_samples():
    """Several samples in a minute, none in another, one unavailable gap.

    minute 0: 100 W for 20 s (held from before the start), 200 W 30 s,
              50 W 10 s -> 8500 Ws / 60 s = 141.667 W, 2.3611 Wh
    minute 1: 50 W held the whole minute -> 50 W, 0.8333 Wh
    minute 2: 50 W 10 s, unavailable 20 s, 300 W 30 s -> 9500 Ws over 40 s
              = 237.5 W, 237.5 / 60 = 3.9583 Wh
    """
    _, run = run_wash(
        [(S - 30, 100.0), (S + 20, 200.0), (S + 50, 50.0), (S + 130, None),
         (S + 150, 300.0)],
        end=S + 180,
    )
    assert run.minutes == 3
    assert run.wh == pytest.approx([8500 / 3600, 50 / 60, 237.5 / 60], abs=1e-4)
    assert run.missing == 0
    assert run.kwh == pytest.approx((8500 / 3600 + 50 / 60 + 237.5 / 60) / 1000, abs=1e-6)
    assert run.from_meter is False
    assert run.finished is True


def test_hold_last_value_across_minutes_without_samples():
    _, run = run_wash([(S - 5, 60.0)], end=S + 240)
    assert run.wh == pytest.approx([1.0, 1.0, 1.0, 1.0])
    assert run.missing == 0


def test_unavailable_minute_is_missing_not_zero():
    _, run = run_wash([(S, 120.0), (S + 60, None), (S + 130, 120.0)], end=S + 180)
    assert run.wh[0] == pytest.approx(2.0)
    assert run.wh[1] is None
    # minute 2: 10 s unavailable, 50 s at 120 W -> mean 120 W over the covered part
    assert run.wh[2] == pytest.approx(2.0)
    assert run.missing == 1


def test_no_sample_ever_seen_is_missing():
    _, run = run_wash([(S + 60, 60.0)], end=S + 120)
    assert run.wh[0] is None
    assert run.wh[1] == pytest.approx(1.0)


def test_waiting_to_start_is_excluded_minute_zero_at_real_start():
    _, run = run_wash(
        [(S - 600, 4.0), (S - 300, 3.0), (S, 600.0)],
        waiting_from=S - 600,
        end=S + 120,
    )
    assert run.started == S
    assert run.minutes == 2
    assert run.wh == pytest.approx([10.0, 10.0])


def test_waiting_only_records_nothing():
    recorder = profile.PowerRecorder()
    recorder.power(S, 4.0)
    assert recorder.status(4, now=S, record=record()) is None
    assert recorder.recording is None
    assert recorder.status(1, now=S + 600, record=record()) is None


def test_paused_keeps_the_clock_running():
    recorder = profile.PowerRecorder()
    rec = record()
    recorder.power(S, 60.0)
    recorder.status(5, now=S, record=rec)
    recorder.status(6, now=S + 60, record=rec)
    recorder.power(S + 60, 6.0)
    recorder.status(5, now=S + 120, record=rec)
    recorder.power(S + 120, 60.0)
    run = recorder.status(7, now=S + 180, record=rec)
    assert run.minutes == 3
    assert run.wh == pytest.approx([1.0, 0.1, 1.0])


def test_finished_is_storable_interrupted_is_not():
    _, done = run_wash([(S, 60.0)], end=S + 600, end_status=7)
    _, stopped = run_wash([(S, 60.0)], end=S + 600, end_status=1)
    assert done.finished and done.storable
    assert not stopped.finished and not stopped.storable


def test_too_many_missing_minutes_not_storable():
    # 1 of 10 minutes missing is within the share, 2 of 10 is not.
    _, one = run_wash([(S, 60.0), (S + 60, None), (S + 120, 60.0)], end=S + 600)
    _, two = run_wash([(S, 60.0), (S + 60, None), (S + 180, 60.0)], end=S + 600)
    assert one.missing == 1 and one.storable
    assert two.missing == 2 and not two.storable
    assert profile.MAX_MISSING_SHARE == pytest.approx(0.10)


def test_record_without_details_not_storable():
    bare = record(temperature=None, spin_rpm=None, extras={}, extras_offered=())
    _, run = run_wash([(S, 60.0)], end=S + 600, rec=bare)
    assert run.key is None
    assert not run.storable


def test_energy_sensor_total_used_when_configured():
    _, run = run_wash(
        [(S, 60.0)], end=S + 600, energy=[(S - 10, 2.500), (S + 590, 2.512)]
    )
    assert run.from_meter is True
    assert run.kwh == pytest.approx(0.012)


def test_integrated_total_without_energy_sensor():
    _, run = run_wash([(S, 60.0)], end=S + 600)
    assert run.from_meter is False
    assert run.kwh == pytest.approx(0.010)


def test_energy_meter_reset_falls_back_to_integrated():
    _, run = run_wash(
        [(S, 60.0)], end=S + 600, energy=[(S - 10, 2.500), (S + 300, 0.001)]
    )
    assert run.from_meter is False
    assert run.kwh == pytest.approx(0.010)


def test_energy_meter_unavailable_at_end_falls_back():
    _, run = run_wash(
        [(S, 60.0)], end=S + 600, energy=[(S - 10, 2.500), (S + 300, None)]
    )
    assert run.from_meter is False
    assert run.kwh == pytest.approx(0.010)


def test_snapshot_of_a_running_wash():
    recorder = profile.PowerRecorder()
    recorder.power(S, 60.0)
    recorder.status(5, now=S, record=record())
    snap = recorder.snapshot(S + 120)
    assert snap.minutes == 2
    assert snap.wh == pytest.approx([1.0, 1.0])
    assert snap.finished is False


# ---------- restart mid-wash ----------


def test_restart_mid_wash_loses_only_the_downtime():
    """Saved at +90 s, Home Assistant back at +300 s, wash ends at +360 s."""
    recorder = profile.PowerRecorder()
    rec = record()
    recorder.power(S, 60.0)
    recorder.status(5, now=S, record=rec)
    saved = recorder.to_dict(now=S + 90)

    restored = profile.PowerRecorder.from_dict(saved)
    assert restored.recording is not None
    restored.power(S + 300, 120.0)
    run = restored.status(7, now=S + 360, record=rec)
    assert run.minutes == 6
    assert run.wh[0] == pytest.approx(1.0)
    assert run.wh[1] == pytest.approx(1.0)  # 30 s covered at 60 W
    assert run.wh[2:5] == [None, None, None]
    assert run.wh[5] == pytest.approx(2.0)
    assert run.missing == 3
    assert run.key == profile.profile_key(rec)


def test_restore_of_garbage_recorder_is_empty():
    for data in (None, "x", 3, {"recording": "bad"}, {"recording": {"start": "x"}}):
        restored = profile.PowerRecorder.from_dict(data)
        assert restored.recording is None


# ---------- the matrix key ----------


def test_key_format():
    rec = record(extras={"quick": False, "gentle": True, "extra_quiet": True})
    assert profile.profile_key(rec) == "Cottons|20|eco=0|1600|soil=2|extra_quiet+gentle"
    assert profile.profile_key(record()) == "Cottons|20|eco=0|1600|soil=2|none"
    assert profile.profile_key(record(soiling=None, eco=True)) == (
        "Cottons|20|eco=1|1600|soil=-|none"
    )


def test_each_extra_makes_its_own_key_twindos_does_not():
    base = profile.profile_key(record())
    with_quick = profile.profile_key(
        record(extras={"quick": True, "gentle": False, "extra_quiet": False})
    )
    other_liquid = profile.profile_key(record(twindos=[0, 2], detergent=True))
    no_liquid = profile.profile_key(record(twindos=[0, 0], detergent=False))
    assert with_quick != base
    assert other_liquid == base == no_liquid


def test_programs_sharing_a_name_get_distinct_keys():
    a = profile.profile_key(record(program_id=24, program_name="Denim"))
    b = profile.profile_key(record(program_id=123, program_name="Denim"))
    assert a != b
    assert a.startswith("Denim (24)|")


def test_key_needs_the_details():
    assert profile.profile_key(record(temperature=None)) is None
    assert profile.profile_key(record(spin_rpm=None)) is None
    assert profile.profile_key(record(program_id=None)) is None
    assert profile.profile_key(None) is None


def test_context_key_matches_the_record_key():
    ctx = washer.ProgramContext(
        prog_id=1,
        temperature=washer.Choice(mask=1, current=20, options=[]),
        spin_rpm=washer.Choice(mask=1, current=1600, options=[]),
        soiling=washer.Choice(mask=1, current=2, options=[]),
        extras={
            "gentle": washer.Extra(mask=1, on=True),
            "quick": washer.Extra(mask=1, on=False),
            "soak": washer.Extra(mask=0, on=True),  # not offered: ignored
        },
    )
    assert profile.context_key(ctx) == "Cottons|20|eco=0|1600|soil=2|gentle"
    assert profile.context_key(None) is None


# ---------- the matrix ----------


def make_run(key, started, wh, kwh=None):
    return profile.MeasuredRun(
        key=key,
        started=started,
        minutes=len(wh),
        wh=list(wh),
        kwh=kwh if kwh is not None else sum(w or 0 for w in wh) / 1000,
        missing=sum(1 for w in wh if w is None),
        from_meter=False,
        finished=True,
    )


def test_matrix_keeps_the_most_recent_runs():
    matrix = profile.ProfileMatrix()
    for i in range(profile.MAX_RUNS + 2):
        assert matrix.add(make_run("k", S + i, [1.0]))
    runs = matrix.runs["k"]
    assert len(runs) == profile.MAX_RUNS == 5
    assert [r.started for r in runs] == [S + i for i in range(2, 7)]


def test_matrix_refuses_unstorable_runs():
    matrix = profile.ProfileMatrix()
    bad = make_run("k", S, [1.0])
    bad.finished = False
    assert not matrix.add(bad)
    assert matrix.runs == {}


def test_profile_is_the_mean_over_runs_of_different_length():
    matrix = profile.ProfileMatrix()
    matrix.add(make_run("k", S, [1.0, 2.0, 3.0], kwh=0.006))
    matrix.add(make_run("k", S + 10, [3.0, 4.0, 5.0, 8.0], kwh=0.015))
    matrix.add(make_run("k", S + 20, [2.0, 6.0, 4.0, 4.0, 9.0], kwh=0.027))
    p = matrix.profile("k")
    # median length 4; minute 3 averages the two runs that have it.
    assert p["minutes"] == 4
    assert p["wh"] == pytest.approx([2.0, 4.0, 4.0, 6.0])
    assert p["kwh"] == pytest.approx(0.016)
    assert p["runs"] == 3
    assert p["last_run"] == datetime.fromtimestamp(S + 20, timezone.utc).isoformat(
        timespec="seconds"
    )
    assert matrix.profile("unknown") is None


def test_profile_skips_a_missing_minute_of_one_run():
    matrix = profile.ProfileMatrix()
    # Stored directly: one missing minute of 2 is above the share, but a
    # long run may hold a few; the profile averages only measured values.
    matrix.runs["k"] = [make_run("k", S, [1.0, 2.0]), make_run("k", S + 1, [3.0, None])]
    assert matrix.profile("k")["wh"] == pytest.approx([2.0, 2.0])


def test_forget():
    matrix = profile.ProfileMatrix()
    matrix.add(make_run("k", S, [1.0]))
    assert matrix.forget("k") is True
    assert matrix.forget("k") is False
    assert matrix.profile("k") is None


def test_profiles_for_templates():
    matrix = profile.ProfileMatrix()
    matrix.add(make_run("a", S, [1.0]))
    matrix.add(make_run("b", S, [2.0, 2.0]))
    all_profiles = matrix.profiles()
    assert set(all_profiles) == {"a", "b"}
    assert all_profiles["b"]["minutes"] == 2


# ---------- storage ----------


def test_storage_round_trip():
    data = profile.ProfileData()
    data.matrix.add(make_run("k", S, [1.0, None, 2.0]))
    rec = data.recorder("fab1")
    rec.power(S, 60.0)
    rec.status(5, now=S, record=record())
    data.last_attempt["fab1"] = make_run("k", S, [1.0])
    stored = data.to_dict(now=S + 60)

    back = profile.ProfileData.from_dict(stored)
    assert back.matrix.profile("k") == data.matrix.profile("k")
    assert back.recorder("fab1").recording is not None
    assert back.recorder("fab1").recording.key == profile.profile_key(record())
    assert back.last_attempt["fab1"].wh == [1.0]


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "garbage",
        [],
        {},
        {"matrix": "bad"},
        {"matrix": {"k": "bad"}},
        {"matrix": {"k": [{"wh": "x"}]}},
        {"recorders": {"fab": 5}, "last_attempt": {"fab": None}},
    ],
)
def test_corrupt_storage_loads_as_empty(stored):
    data = profile.ProfileData.from_dict(stored)
    assert data.matrix.profiles() == {}


# ---------- a real recording of the owner's plug ----------

STATUS = {"off": 1, "on": 2, "programmed": 3, "waiting_to_start": 4,
          "running": 5, "paused": 6, "finished": 7, "failure": 8}


def _fixture_rows():
    with FIXTURE_CSV.open() as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["t"] = datetime.fromisoformat(row["time"]).timestamp()
    return rows


def _number(value):
    return None if value in ("unavailable", "unknown") else float(value)


def test_real_wash_from_the_plug_fixture():
    """Cottons 20 °C, 1600 rpm, no extras: waits from 12:01, runs 12:15.

    Waiting rows are excluded; minute 0 is the first running row. The
    fixture is the full wash, with two Home Assistant restarts in it.
    """
    # Empty rows: the capture itself got no answer (Home Assistant restarting);
    # nothing reached the recorder then either.
    rows = [r for r in _fixture_rows() if r["socket_energy_kwh"] != ""]
    rec = record(extras={"quick": False, "gentle": False, "extra_quiet": False})
    recorder = profile.PowerRecorder()
    run = None
    for row in rows:
        recorder.power(row["t"], _number(row["socket_power_w"]))
        recorder.energy(row["t"], _number(row["socket_energy_kwh"]))
        if row["wsi863_status"] not in STATUS:  # washer unreadable: no poll result
            continue
        out = recorder.status(STATUS[row["wsi863_status"]], now=row["t"], record=rec)
        if out is not None:
            run = out
    running = [r for r in rows if r["wsi863_status"] in ("running", "paused")]
    first = running[0]
    assert first["time"].endswith("12:15:34")
    if run is None:  # the wash is still running at the end of the fixture
        end = rows[-1]["t"] + 60
        run = recorder.snapshot(end)
    else:
        end = run.started + run.minutes * 60
    assert run.started == first["t"]
    assert run.minutes == math.ceil((end - first["t"]) / 60)
    # the whole wash: the washer reported finished (Status 7) at 15:57
    assert run.finished
    assert run.minutes == 223
    assert run.kwh == pytest.approx(0.778, abs=0.002)
    assert run.missing == 0
    # minute 0: 16.506 W the whole minute (next sample 62 s later)
    assert run.wh[0] == pytest.approx(16.506 / 60, abs=1e-4)
    # minute 1: 2 s of 16.506 W, then 58 s of 3.994 W
    assert run.wh[1] == pytest.approx((2 * 16.506 + 58 * 3.994) / 3600, abs=1e-4)
    # the waiting power (about 3-4 W) never enters minute 0
    assert run.wh[0] > 10 / 60
    assert run.key == "Cottons|20|eco=0|1600|soil=2|none"
    if run.finished:
        assert run.from_meter
        assert run.kwh == pytest.approx(
            float(rows[-1]["socket_energy_kwh"]) - float(first["socket_energy_kwh"]),
            abs=0.002,
        )


# ---------- WITHOUT the plug ----------


def test_without_plug_no_profile_entities(caplog):
    caplog.set_level(logging.DEBUG)
    for options in ({}, {"plugs": {}}, {"plugs": "bad"}, {"plugs": {"fab1": {}}},
                    {"plugs": {"fab1": {"power_sensor": ""}}}):
        plugs = profile.plugs_from_options(options)
        assert "fab1" not in plugs
        assert profile.profile_entity_keys(plugs, "fab1") == ()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_adding_the_plug_later_makes_the_entities_appear():
    options = profile.options_with_plug({}, "fab1", "sensor.washer_plug_power", None)
    assert options == {"plugs": {"fab1": {"power_sensor": "sensor.washer_plug_power"}}}
    plugs = profile.plugs_from_options(options)
    assert plugs["fab1"].power_sensor == "sensor.washer_plug_power"
    assert plugs["fab1"].energy_sensor is None
    assert profile.profile_entity_keys(plugs, "fab1") == profile.PROFILE_ENTITY_KEYS
    assert profile.profile_entity_keys(plugs, "fab2") == ()
    # Config entry options are a read-only mapping in Home Assistant.
    from types import MappingProxyType

    assert "fab1" in profile.plugs_from_options(MappingProxyType(options))


def test_energy_sensor_alone_is_no_plug():
    options = profile.options_with_plug({}, "fab1", None, "sensor.washer_plug_energy")
    assert profile.profile_entity_keys(profile.plugs_from_options(options), "fab1") == ()


def test_removing_the_plug_clears_its_options():
    options = profile.options_with_plug(
        {"plugs": {"fab1": {"power_sensor": "sensor.p"}, "fab2": {"power_sensor": "sensor.q"}}},
        "fab1", None, None,
    )
    assert options == {"plugs": {"fab2": {"power_sensor": "sensor.q"}}}


def test_without_plug_the_wash_record_is_unchanged():
    """The tracker alone (no recorder) still freezes and closes the record."""
    tracker = washer.WashTracker()
    tracker.update(4, now=S - 600, state={"ProgramID": 1})
    tracker.update(5, now=S)
    rec = tracker.update(7, now=S + 600)
    assert rec.state == washer.WASH_FINISHED
    assert rec.duration == 10  # the ten minutes on the washer's timer are not washing
    assert not hasattr(rec, "wh")


def test_profile_entities_and_action_are_translated():
    import json

    component = Path(__file__).resolve().parents[1] / "custom_components" / "miele_mobile_control"
    strings = json.loads((component / "strings.json").read_text())
    for key in profile.PROFILE_ENTITY_KEYS:
        assert strings["entity"]["sensor"][key]["name"]
    assert strings["services"]["forget_profile"]["fields"]["key"]["name"]
    assert set(strings["options"]["step"]["plug"]["data"]) == {
        profile.CONF_POWER_SENSOR, profile.CONF_ENERGY_SENSOR
    }
    services = (component / "services.yaml").read_text()
    assert "\nforget_profile:\n  target:\n    entity:\n" in services


def test_wash_already_running_at_setup_is_skipped():
    """A wash whose start the recorder never saw (installed, or the plug
    chosen, in the middle of a wash) is not measured: half a wash stored as
    a whole one would be a wrong profile. The next wash is measured."""
    recorder = profile.PowerRecorder()
    recorder.power(S, 500.0)
    recorder.join(5)
    assert recorder.status(5, now=S, record=record()) is None
    assert recorder.recording is None
    assert recorder.status(5, now=S + 600, record=record()) is None
    assert recorder.status(7, now=S + 1200, record=record()) is None
    assert recorder.recording is None
    # the next wash, seen from its start
    recorder.status(3, now=S + 5000, record=None)
    recorder.status(5, now=S + 6000, record=record())
    assert recorder.recording is not None
    run = recorder.status(7, now=S + 6120, record=record())
    assert run is not None and run.minutes == 2


def test_join_does_not_skip_a_restored_recording():
    """A restart mid-wash with a saved recording goes on measuring."""
    recorder = profile.PowerRecorder()
    recorder.power(S, 60.0)
    recorder.status(5, now=S, record=record())
    restored = profile.PowerRecorder.from_dict(recorder.to_dict(now=S + 60))
    restored.join(5)
    assert restored.blind is False
    assert restored.status(5, now=S + 120, record=record()) is None
    assert restored.recording is not None


def test_power_value_parsing():
    assert profile.parse_power("12.5", "W") == 12.5
    assert profile.parse_power("1.5", "kW") == 1500.0
    assert profile.parse_power("unavailable", "W") is None
    assert profile.parse_power("unknown", "W") is None
    assert profile.parse_power("nan", "W") is None
    assert profile.parse_energy("1500", "Wh") == 1.5
    assert profile.parse_energy("2.5", "kWh") == 2.5
    assert profile.parse_energy("x", "kWh") is None
