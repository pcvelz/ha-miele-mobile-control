"""TwinDos laundry liquid tests against the captured WSI863 leaves.

The three patterns (off [0, 0], whites [1, 0], colours [0, 2]) were measured
on one WSI863 with UltraPhase in both containers, program Cottons; the
captured Cottons context shows colours with both containers changeable
(mask 9).
"""

from __future__ import annotations

import copy

import pytest

from custom_components.miele_mobile_control import const, washer
from custom_components.miele_mobile_control.dop2 import decode


def ctx_fields(fixtures, values=None, masks=None):
    """Decoded Cottons 2/1574, optionally with other container values/masks."""
    fields = copy.deepcopy(decode(fixtures["cottons_selected_2_1574_0_1"])[1])
    containers = fields[3][1][15][1][1][1]
    for i, c in enumerate(containers):
        if values is not None:
            c[3][1] = values[i]
        if masks is not None:
            c[1][1] = masks[i]
    return fields


def sel_fields(fixtures):
    return copy.deepcopy(decode(fixtures["on_no_program_2_1577_0_0"])[1])


def written(built):
    return built[6][1][13][1][1][1]


# ---------- parsing and pattern names ----------


def test_captured_cottons_context_shows_colours_both_changeable(fixtures):
    context = washer.parse_context(ctx_fields(fixtures))
    assert washer.dosing_values(context.dosing) == [0, 2]
    assert [c.mask for c in context.dosing.containers] == [9, 9]
    assert washer.twindos_writable(context.dosing)
    assert washer.twindos_mode([0, 2]) == "colours"
    # The three "no ..." flags are not offered on this machine.
    assert not context.dosing.no_laundry_detergent.offered
    assert not context.dosing.no_fabric_conditioner.offered
    assert not context.dosing.no_additive.offered


def test_measured_patterns():
    assert washer.TWINDOS_PATTERNS == {
        "off": (0, 0), "whites": (1, 0), "colours": (0, 2),
    }
    assert washer.twindos_mode([0, 0]) == "off"
    assert washer.twindos_mode([1, 0]) == "whites"
    assert washer.twindos_mode([0, 2]) == "colours"
    for odd in ([1, 2], [2, 0], [0, 1], [0], [], [0, 0, 0]):
        assert washer.twindos_mode(odd) == washer.TWINDOS_UNKNOWN


def test_detergent_from_mode_never_guesses():
    assert washer.detergent_from_mode("off") is False
    assert washer.detergent_from_mode("whites") is True
    assert washer.detergent_from_mode("colours") is True
    assert washer.detergent_from_mode("unknown") is None
    assert washer.detergent_from_mode(None) is None


def test_missing_dosing_block_keeps_the_context(fixtures):
    fields = ctx_fields(fixtures)
    del fields[3][1][15]
    context = washer.parse_context(fields)
    assert context.prog_id == 1
    assert context.dosing is None
    assert not washer.twindos_writable(None)


def test_not_writable_unless_exactly_two_changeable(fixtures):
    one_locked = washer.parse_context(ctx_fields(fixtures, masks=[9, 0])).dosing
    assert not washer.twindos_writable(one_locked)
    single = washer.parse_context(ctx_fields(fixtures)).dosing
    single.containers = single.containers[:1]
    assert not washer.twindos_writable(single)


# ---------- building the write ----------


@pytest.mark.parametrize("name", ["off", "whites", "colours"])
def test_twindos_writes_the_whole_pattern(fixtures, name):
    built = washer.build_parameters(
        sel_fields(fixtures), ctx_fields(fixtures), twindos=name
    )
    assert written(built) == list(washer.TWINDOS_PATTERNS[name])
    assert built[3][1] == 2  # parametrization, never a start request


def test_detergent_off_writes_off(fixtures):
    built = washer.build_parameters(
        sel_fields(fixtures), ctx_fields(fixtures), detergent=False
    )
    assert written(built) == [0, 0]


def test_detergent_on_restores_last_known_pattern(fixtures):
    built = washer.build_parameters(
        sel_fields(fixtures),
        ctx_fields(fixtures, values=[0, 0]),
        detergent=True,
        dosing_defaults=[1, 0],
    )
    assert written(built) == [1, 0]


@pytest.mark.parametrize("last", [None, [], [0, 0], [1, 2], [5]])
def test_detergent_on_refuses_without_known_pattern(fixtures, last):
    with pytest.raises(washer.NotOfferedError, match="cannot be switched on"):
        washer.build_parameters(
            sel_fields(fixtures),
            ctx_fields(fixtures, values=[0, 0]),
            detergent=True,
            dosing_defaults=last,
        )


@pytest.mark.parametrize(
    "kwargs", [{"detergent": False}, {"detergent": True, "dosing_defaults": [1, 0]},
               {"twindos": "off"}, {"twindos": "whites"}],
)
def test_refused_unless_both_containers_changeable(fixtures, kwargs):
    for masks in ([9, 0], [0, 9], [0, 0]):
        with pytest.raises(washer.NotOfferedError, match="two changeable"):
            washer.build_parameters(
                sel_fields(fixtures), ctx_fields(fixtures, masks=masks), **kwargs
            )
        with pytest.raises(washer.NotOfferedError):
            washer.validate_parameters(
                washer.parse_context(ctx_fields(fixtures, masks=masks)),
                **{k: v for k, v in kwargs.items() if k != "dosing_defaults"},
            )


def test_no_twindos_request_copies_the_washer_values(fixtures):
    built = washer.build_parameters(
        sel_fields(fixtures), ctx_fields(fixtures, values=[1, 0]), spin_rpm=1200
    )
    assert written(built) == [1, 0]


def test_refused_when_the_select_leaf_has_no_two_slots(fixtures):
    sel = sel_fields(fixtures)
    sel[6][1][13][1][1][1] = [0]
    with pytest.raises(washer.NotOfferedError):
        washer.build_parameters(sel, ctx_fields(fixtures), twindos="off")


def test_session_writes_off_without_start(fixtures):
    """The session write for TwinDos off is one 2/1577 parametrization."""
    from test_washer import FakeCloud, _run

    cloud = FakeCloud(fixtures, delay=0.0)
    session = washer.WasherSession(cloud.read_leaf, cloud.write_leaf)
    _run(session.set_parameters(detergent=False))
    assert len(cloud.writes) == 1
    unit, attr, leaf = cloud.writes[0]
    assert (unit, attr) == (2, 1577)
    fields = decode(leaf)[1]
    assert fields[3][1] == 2
    assert fields[6][1][13][1][1][1] == [0, 0]


# ---------- the set_program action ----------


def test_twindos_value_validator():
    assert const.twindos_value("Whites") == "whites"
    assert const.twindos_value("off") == "off"
    assert const.twindos_value(False) == "off"  # unquoted YAML off
    for bad in ("on", True, "bont", 0, None):
        with pytest.raises(ValueError):
            const.twindos_value(bad)


@pytest.mark.parametrize(
    "detergent,twindos",
    [(False, "whites"), (False, "colours"), (True, "off")],
)
def test_contradicting_detergent_and_twindos_refused(detergent, twindos):
    with pytest.raises(ValueError, match="contradicts"):
        const.build_changes(detergent=detergent, twindos=twindos)


def test_consistent_detergent_and_twindos_accepted():
    assert const.build_changes(detergent=True, twindos="whites") == {
        "twindos": "whites", "detergent": True,
    }
    assert const.build_changes(detergent=False, twindos="off") == {
        "twindos": "off", "detergent": False,
    }
    assert const.build_changes(twindos="colours") == {"twindos": "colours"}


def test_plan_same_program_validates_twindos_before_write(fixtures):
    context = washer.parse_context(ctx_fields(fixtures, masks=[9, 0]))
    with pytest.raises(washer.NotOfferedError):
        const.plan_set_program(
            program_value=1, program_ids=[1], context=context, twindos="off"
        )
    context = washer.parse_context(ctx_fields(fixtures))
    pid, changes, select_first = const.plan_set_program(
        program_value=1, program_ids=[1], context=context, twindos="off"
    )
    assert (pid, changes, select_first) == (1, {"twindos": "off"}, False)


def test_verify_written_twindos(fixtures):
    colours = washer.parse_context(ctx_fields(fixtures))
    assert const.verify_written(colours, {"twindos": "colours"}) == []
    assert const.verify_written(colours, {"detergent": True}) == []
    assert const.verify_written(colours, {"twindos": "off"})
    assert const.verify_written(colours, {"detergent": False})
    unknown = washer.parse_context(ctx_fields(fixtures, values=[1, 2]))
    # An unknown pattern confirms neither on nor off.
    assert const.verify_written(unknown, {"detergent": False})
    assert const.verify_written(unknown, {"detergent": True})


# ---------- the wash record ----------


def test_record_freezes_twindos(fixtures):
    tracker = washer.WashTracker()
    tracker.note_context(washer.parse_context(ctx_fields(fixtures, values=[1, 0])))
    record = tracker.update(5, now=1_700_000_000.0, state={"ProgramID": 1})
    assert record.twindos == [1, 0]
    assert record.twindos_mode == "whites"
    assert record.detergent is True
    again = washer.WashRecord.from_dict(record.to_dict())
    assert again.twindos == [1, 0] and again.detergent is True


def test_record_off_and_unknown(fixtures):
    for values, mode, detergent in (([0, 0], "off", False), ([2, 2], "unknown", None)):
        tracker = washer.WashTracker()
        tracker.note_context(washer.parse_context(ctx_fields(fixtures, values=values)))
        record = tracker.update(5, now=1.0, state={"ProgramID": 1})
        assert record.twindos_mode == mode
        assert record.detergent is detergent


def test_record_stored_by_0_1_1_still_loads():
    stored = {
        "state": "finished",
        "program_id": 1,
        "program_name": "Cottons",
        "temperature": 40,
        "eco": False,
        "spin_rpm": 1400,
        "soiling": 2,
        "extras": {"quick": False},
        "extras_offered": ["quick"],
        "started": 1_700_000_000.0,
        "finished": 1_700_007_200.0,
        "duration": 120,
    }
    record = washer.WashRecord.from_dict(stored)
    assert record is not None
    assert record.program_name == "Cottons"
    assert record.twindos == []
    assert record.twindos_mode == washer.TWINDOS_UNKNOWN
    assert record.detergent is None
