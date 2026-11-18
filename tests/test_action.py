"""Pure tests for the set_program action's decision logic.

The ordering lives in const.plan_set_program / build_changes /
extras_change / verify_written, all Home Assistant free, so the whole
"what is validated when and what gets written" contract is testable here:

* an unknown or unoffered program is refused before anything is written;
* same program + an option the program does not offer: refused, nothing
  written (select_first is False and validation happens inside the plan);
* different program + a bad option: the plan defers validation, and when
  validation then fails the caller's message says the program WAS changed;
* verify_written catches an extra (and an eco variant) that did not stick.
"""

from __future__ import annotations

import pytest

from custom_components.miele_mobile_control import const, washer


def make_context(
    prog_id: int = 1,
    *,
    temp_current: int = 40,
    temp_info: int = 0,
    temp_options: tuple = ((20, 0), (40, 0), (40, 1)),
    spin_current: int = 1400,
    spin_options: tuple = (400, 800, 1400),
    soiling_current: int = 2,
    soiling_options: tuple = (1, 2, 3),
    extras: dict | None = None,
) -> washer.ProgramContext:
    return washer.ProgramContext(
        prog_id=prog_id,
        temperature=washer.Choice(
            mask=1, current=temp_current, options=list(temp_options), info=temp_info
        ),
        spin_rpm=washer.Choice(mask=1, current=spin_current, options=list(spin_options)),
        soiling=washer.Choice(
            mask=1, current=soiling_current, options=list(soiling_options)
        ),
        extras=extras if extras is not None else {},
    )


def extra(mask: int = 1, on: bool = False) -> washer.Extra:
    return washer.Extra(mask=mask, on=on)


# ---------- program resolution happens before any write ----------


def test_unknown_program_name_refused():
    with pytest.raises(ValueError, match="unknown program"):
        const.plan_set_program(
            program_value="Nonsense", program_ids=[1, 3], context=make_context()
        )


def test_known_name_not_offered_by_machine_refused():
    # "Minimum iron" is a real program, but this machine does not offer it.
    with pytest.raises(ValueError, match="Minimum iron"):
        const.plan_set_program(
            program_value="Minimum iron", program_ids=[1], context=make_context()
        )


def test_number_not_in_program_list_refused_and_names_offerings():
    with pytest.raises(ValueError, match="Cottons"):
        const.plan_set_program(
            program_value=3, program_ids=[1], context=make_context()
        )


# ---------- same program: everything validated before any write ----------


def test_same_program_valid_request_skips_select():
    pid, changes, select_first = const.plan_set_program(
        program_value="Cottons",
        program_ids=[1, 3],
        context=make_context(),
        temperature=40,
        eco=True,
        spin_rpm=800,
        soiling=3,
    )
    assert pid == 1
    assert select_first is False
    assert changes == {
        "temperature": 40,
        "eco": True,
        "spin_rpm": 800,
        "soiling": 3,
    }


def test_same_program_bad_temperature_refused_before_any_write():
    with pytest.raises(washer.NotOfferedError, match="95"):
        const.plan_set_program(
            program_value=1,
            program_ids=[1],
            context=make_context(),
            temperature=95,
        )


def test_same_program_bad_spin_refused_before_any_write():
    with pytest.raises(washer.NotOfferedError, match="rpm"):
        const.plan_set_program(
            program_value=1,
            program_ids=[1],
            context=make_context(spin_options=(400, 800)),
            spin_rpm=1400,
        )


def test_same_program_extra_not_offered_refused_before_any_write():
    context = make_context(extras={"soak": extra(mask=0)})
    with pytest.raises(washer.NotOfferedError, match="soak"):
        const.plan_set_program(
            program_value=1,
            program_ids=[1],
            context=context,
            extras=["soak"],
        )


def test_same_program_no_options_means_no_changes():
    pid, changes, select_first = const.plan_set_program(
        program_value="Cottons", program_ids=[1, 3], context=make_context()
    )
    assert (pid, changes, select_first) == (1, {}, False)


# ---------- different program: validation deferred until after the select ----------


def test_different_program_defers_option_validation():
    # Temperature 95 °C is not offered, but the plan must NOT raise yet:
    # the program change goes first and only then can the options be
    # validated against the new program.
    pid, changes, select_first = const.plan_set_program(
        program_value="Minimum iron",
        program_ids=[1, 3],
        context=make_context(prog_id=1),
        temperature=95,
    )
    assert pid == 3
    assert select_first is True
    assert changes["temperature"] == 95


def test_different_program_bad_option_reports_program_changed():
    # Simulate what __init__ does after the washer took the new program:
    # validation fails, and the message must say the program WAS changed.
    pid, changes, _ = const.plan_set_program(
        program_value="Minimum iron",
        program_ids=[1, 3],
        context=make_context(prog_id=1),
        temperature=95,
    )
    new_context = make_context(prog_id=3, temp_options=((30, 0),))
    with pytest.raises(ValueError) as err:
        washer.validate_parameters(new_context, **changes)
    message = const.options_rejected_after_program_change(pid, err.value)
    assert "changed to Minimum iron" in message
    assert "options were not applied" in message


def test_different_program_valid_options_applied_after_select():
    pid, changes, select_first = const.plan_set_program(
        program_value=3,
        program_ids=[1, 3],
        context=make_context(prog_id=1),
        spin_rpm=800,
    )
    assert select_first is True
    new_context = make_context(prog_id=3)
    washer.validate_parameters(new_context, **changes)  # must not raise


# ---------- build_changes: mistakes checkable without the program ----------


def test_eco_without_temperature_refused():
    with pytest.raises(ValueError, match="eco needs a temperature"):
        const.build_changes(eco=True)


def test_unknown_extra_name_refused():
    with pytest.raises(ValueError, match="unknown extras: teleport"):
        const.build_changes(extras=["soak", "teleport"])


# ---------- extras_change: listed on, the rest the program knows off ----------


def test_extras_change_turns_unlisted_program_extras_off():
    context = make_context(
        extras={"soak": extra(on=True), "quick": extra(on=True)}
    )
    assert const.extras_change(["soak"], context) == {
        "soak": True,
        "quick": False,
    }


def test_extras_change_includes_requested_extra_program_does_not_know():
    # Kept in the write so validate_parameters can refuse it with a clear
    # message instead of it silently vanishing.
    context = make_context(extras={"soak": extra()})
    assert const.extras_change(["quick"], context) == {
        "quick": True,
        "soak": False,
    }


def test_extras_change_empty_list_switches_all_program_extras_off():
    context = make_context(extras={"soak": extra(on=True), "quick": extra()})
    assert const.extras_change([], context) == {"soak": False, "quick": False}


def test_extras_change_never_writes_off_locked_or_unoffered():
    # "off" is only written for extras the program offers AND allows to
    # change; a locked or unoffered extra is left untouched.
    context = make_context(
        extras={
            "soak": extra(mask=1, on=True),  # offered, writable
            "quick": extra(mask=2, on=True),  # offered, locked
            "gentle": extra(mask=0),  # not offered
        }
    )
    assert const.extras_change([], context) == {"soak": False}


# ---------- verify_written: catches extras and eco that did not stick ----------


def test_verify_written_all_changes_stuck():
    context = make_context(
        temp_info=1,
        spin_current=800,
        soiling_current=3,
        extras={"soak": extra(on=True), "quick": extra()},
    )
    changes = {
        "temperature": 40,
        "eco": True,
        "spin_rpm": 800,
        "soiling": 3,
        "extras": {"soak": True, "quick": False},
    }
    assert const.verify_written(context, changes) == []


def test_verify_written_catches_extra_that_did_not_stick():
    context = make_context(extras={"soak": extra(on=False)})
    problems = const.verify_written(
        context, {"extras": {"soak": True}}
    )
    assert len(problems) == 1
    assert "soak" in problems[0]


def test_verify_written_catches_kept_eco_variant():
    context = make_context(temp_current=40, temp_info=0)
    problems = const.verify_written(context, {"temperature": 40, "eco": True})
    assert any("non-eco" in p for p in problems)


def test_verify_written_catches_kept_temperature_spin_soiling():
    context = make_context()
    problems = const.verify_written(
        context,
        {"temperature": 20, "spin_rpm": 800, "soiling": 1},
    )
    assert len(problems) == 3
