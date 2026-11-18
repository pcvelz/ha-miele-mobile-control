"""Washer logic tests against the captured WSI863 leaves."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.miele_mobile_control.dop2 import decode, encode
from custom_components.miele_mobile_control import washer


def ctx(fixtures):
    """Parsed Cottons context."""
    return washer.parse_context(decode(fixtures["cottons_selected_2_1574_0_1"])[1])


def test_program_list_ids(fixtures):
    _, fields = decode(fixtures["on_no_program_2_1584_1_1"])
    assert washer.parse_program_list(fields) == [
        146, 123, 190, 27, 23, 9, 8, 4, 3, 1, 69, 37, 122, 29, 31, 39, 22,
        129, 53, 95,
    ]


def test_cottons_context_values(fixtures):
    context = ctx(fixtures)
    assert context.prog_id == 1
    assert context.temperature.current == 40
    assert context.temperature.info == 0
    assert context.spin_rpm.current == 1600
    assert context.temperature.options == [
        (0, 2), (20, 0), (30, 0), (40, 1), (40, 0), (50, 0), (60, 1), (60, 0),
        (75, 0), (90, 0),
    ]
    assert context.spin_rpm.options == [0, 400, 600, 800, 1000, 1200, 1400, 1600]
    assert context.soiling.options == [1, 2, 3]


def test_cottons_offered_extras(fixtures):
    context = ctx(fixtures)
    offered = {name for name, e in context.extras.items() if e.offered}
    assert offered == {
        "quick", "single", "water_plus", "pre_wash", "soak", "rinse_hold",
        "extra_quiet", "steam_smoothing", "gentle", "allergo_wash",
        "intensive",
    }
    assert context.extras["rinsing_plus"].offered is False


def test_build_parameters_matches_accepted_write(fixtures):
    """60 °C, 1200 rpm, extra_quiet + gentle must equal the accepted PUT."""
    _, sel = decode(fixtures["on_no_program_2_1577_0_0"])
    _, cottons = decode(fixtures["cottons_selected_2_1574_0_1"])
    built = washer.build_parameters(
        sel,
        cottons,
        temperature=60,
        spin_rpm=1200,
        extras={"extra_quiet": True, "gentle": True},
    )
    header = {"unit": 2, "attr": 1577, "idx1": 0, "idx2": 0}
    assert encode(header, built) == fixtures[
        "write_cottons_60_1200_quiet_gentle_2_1577"
    ]


def test_build_parameters_defaults_only(fixtures):
    """No changes: copies the appliance values, parametrization type 2."""
    _, sel = decode(fixtures["on_no_program_2_1577_0_0"])
    _, cottons = decode(fixtures["cottons_selected_2_1574_0_1"])
    built = washer.build_parameters(sel, cottons)
    assert built[1][1] == 1
    assert built[3][1] == 2
    para = built[6][1]
    assert para[3][1] == 40  # temperature copied from 2/1574
    assert para[5][1] == 160  # spin 1600 rpm


def test_build_parameters_without_program_fails(fixtures):
    _, sel = decode(fixtures["on_no_program_2_1577_0_0"])
    _, empty = decode(fixtures["on_no_program_2_1574_0_1"])
    with pytest.raises(washer.NoProgramError):
        washer.build_parameters(sel, empty)


def test_validation_rejects_values_not_offered(fixtures):
    context = ctx(fixtures)
    with pytest.raises(washer.NotOfferedError):
        washer.validate_parameters(context, temperature=55)
    # Cottons offers 40 °C Eco, but no eco variant at 90 °C
    with pytest.raises(washer.NotOfferedError):
        washer.validate_parameters(context, temperature=90, eco=True)
    with pytest.raises(washer.NotOfferedError):
        washer.validate_parameters(context, spin_rpm=900)
    with pytest.raises(washer.NotOfferedError):
        washer.validate_parameters(context, soiling=5)
    with pytest.raises(washer.NotOfferedError):
        washer.validate_parameters(context, extras={"rinsing_plus": True})


def test_validation_accepts_offered_values(fixtures):
    context = ctx(fixtures)
    washer.validate_parameters(context, temperature=60)
    washer.validate_parameters(context, temperature=60, eco=True)
    washer.validate_parameters(context, temperature=0)  # cold
    washer.validate_parameters(context, spin_rpm=1200)
    washer.validate_parameters(context, soiling=3)
    washer.validate_parameters(context, extras={"extra_quiet": True})


def test_start_request_fields(fixtures):
    """The start write sets 2/1583 field 1 to 1."""
    _, fields = decode(fixtures["on_no_program_2_1583_0_0"])
    built = washer.build_start_request(fields)
    assert built[1][1] == 1
    header = {"unit": 2, "attr": 1583, "idx1": 0, "idx2": 0}
    leaf = encode(header, built)
    _, written = decode(leaf)
    assert written[1][1] == 1
    # and it round-trips to a well-formed leaf
    assert encode(decode(leaf)[0], written) == leaf


def test_program_names_and_fallback():
    assert washer.program_name(1) == "Cottons"
    assert washer.program_name(9999) == "Program 9999"
    assert washer.program_id("cottons") == 1
    assert washer.program_id("190") == 190
    assert washer.program_id(146) == 146
    with pytest.raises(ValueError):
        washer.program_id("no such program")


def test_labels():
    assert washer.temperature_label(0, 2) == "Cold"
    assert washer.temperature_label(20, 0) == "20 °C"
    assert washer.temperature_label(40, 1) == "40 °C Eco"
    assert washer.temperature_label(40, 0) == "40 °C"
    assert washer.spin_label(0) == "No spin"
    assert washer.spin_label(1400) == "1400 rpm"


def test_mobile_start_armed():
    assert washer.mobile_start_armed({"RemoteEnable": [15, 0, 1]}) is True
    assert washer.mobile_start_armed({"RemoteEnable": [15, 0, 0]}) is False
    assert washer.mobile_start_armed({"RemoteEnable": []}) is False


def test_start_allowed():
    assert washer.start_allowed({"Status": 3, "RemoteEnable": [15, 0, 1]})
    assert not washer.start_allowed({"Status": 2, "RemoteEnable": [15, 0, 1]})
    assert not washer.start_allowed({"Status": 3, "RemoteEnable": [15, 0, 0]})


# ---------- WasherSession: serialized writes against a fake cloud ----------


class FakeCloud:
    """In-memory async stand-in for the Miele cloud with one washer.

    Serves the captured leaves with an artificial network delay and applies a
    written 2/1577 to the 2/1574 it serves, like the washer does. Reads
    re-encode from the stored state, so the caller never mutates it.
    """

    def __init__(self, fixtures, delay=0.2):
        self.delay = delay
        self.ctx = decode(fixtures["cottons_selected_2_1574_0_1"])[1]
        self.sel = decode(fixtures["on_no_program_2_1577_0_0"])[1]
        self.sel_header = {"unit": 2, "attr": 1577, "idx1": 0, "idx2": 0}
        self.usr = decode(fixtures["on_no_program_2_1583_0_0"])[1]
        self.usr_header = {"unit": 2, "attr": 1583, "idx1": 0, "idx2": 0}
        self.ctx_header = {"unit": 2, "attr": 1574, "idx1": 0, "idx2": 1}
        self.writes = []  # (unit, attr, leaf bytes), in order

    async def read_leaf(self, unit, attr, idx1=0, idx2=0):
        await asyncio.sleep(self.delay)
        if (unit, attr) == (2, 1577):
            return encode(self.sel_header, self.sel)
        if (unit, attr) == (2, 1574):
            return encode(self.ctx_header, self.ctx)
        if (unit, attr) == (2, 1583):
            return encode(self.usr_header, self.usr)
        return None

    async def write_leaf(self, unit, attr, leaf):
        await asyncio.sleep(self.delay)
        self.writes.append((unit, attr, leaf))
        if (unit, attr) == (2, 1577):
            self._apply_selection(decode(leaf)[1])

    def _apply_selection(self, sel_fields):
        """Fold a written 2/1577 into the served 2/1574 current values."""
        para = sel_fields[6][1]
        ctx_para = self.ctx[3][1]
        ctx_para[3][1][2][1] = para[3][1]  # temperature
        ctx_para[3][1][3][1] = para[4][1]  # temperature info
        ctx_para[5][1][2][1] = para[5][1]  # spin speed
        ctx_para[6][1][2][1] = para[6][1]  # soiling
        for member, entry in para[14][1].items():
            if member in ctx_para[16][1]:
                ctx_para[16][1][member][1][2][1] = entry[1]


def _confirm_parameters(cloud, **changes):
    async def confirm():
        ctx = washer.parse_context(cloud.ctx)
        temperature = changes.get("temperature")
        if temperature is not None and ctx.temperature.current != temperature:
            return False
        spin_rpm = changes.get("spin_rpm")
        if spin_rpm is not None and ctx.spin_rpm.current != spin_rpm:
            return False
        for name, on in (changes.get("extras") or {}).items():
            extra = ctx.extras.get(name)
            if extra is None or extra.on != on:
                return False
        return True

    return confirm


def _run(coro):
    return asyncio.run(coro)


def test_overlapping_parameter_changes_both_apply(fixtures):
    """30 °C and 800 rpm, overlapping on different parameters: both survive.

    Without serialization both reads see the same starting state and the
    second write is built from it, so whichever lands last drops the other
    change; disjoint parameters make that loss visible.
    """
    cloud = FakeCloud(fixtures, delay=0.2)
    session = washer.WasherSession(
        cloud.read_leaf, cloud.write_leaf, confirm_timeout=5.0
    )

    async def change_1():
        await session.set_parameters(
            temperature=30, confirm=_confirm_parameters(cloud, temperature=30)
        )

    async def change_2():
        await session.set_parameters(
            spin_rpm=800, confirm=_confirm_parameters(cloud, spin_rpm=800)
        )

    async def scenario():
        await asyncio.gather(change_1(), change_2())

    _run(scenario())

    final = washer.parse_context(cloud.ctx)
    assert final.temperature.current == 30
    assert final.spin_rpm.current == 800
    # both writes went out, each reading the other's result
    assert len(cloud.writes) == 2


def test_session_writes_with_read_header(fixtures):
    """The written leaf carries the header the appliance answered with."""
    cloud = FakeCloud(fixtures, delay=0.05)
    cloud.sel_header = {"unit": 2, "attr": 1577, "idx1": 7, "idx2": 9}
    session = washer.WasherSession(cloud.read_leaf, cloud.write_leaf)

    _run(session.select_program(1))

    _unit, _attr, leaf = cloud.writes[-1]
    assert decode(leaf)[0] == {"unit": 2, "attr": 1577, "idx1": 7, "idx2": 9}


def test_session_start_uses_read_header(fixtures):
    cloud = FakeCloud(fixtures, delay=0.05)
    cloud.usr_header = {"unit": 2, "attr": 1583, "idx1": 3, "idx2": 4}
    session = washer.WasherSession(cloud.read_leaf, cloud.write_leaf)

    _run(session.start())

    _unit, _attr, leaf = cloud.writes[-1]
    header, fields = decode(leaf)
    assert header == {"unit": 2, "attr": 1583, "idx1": 3, "idx2": 4}
    assert fields[1][1] == 1
