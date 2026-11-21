"""Delayed start: the washer's own timer, armed from Home Assistant.

The app writes the delay (whole minutes) and its mode into 2/1577, then
sends user request 8 on 2/1583 instead of request 1.
"""

from __future__ import annotations

import copy

import pytest

from custom_components.miele_mobile_control.dop2 import decode
from custom_components.miele_mobile_control import washer


def ctx_fields(fixtures):
    return copy.deepcopy(decode(fixtures["cottons_selected_2_1574_0_1"])[1])


def sel_fields(fixtures):
    return copy.deepcopy(decode(fixtures["on_no_program_2_1577_0_0"])[1])


def test_delay_minutes_counts_as_the_app():
    assert washer.delay_minutes(1000 + 90 * 60, 1000) == 91
    assert washer.delay_minutes(1000 + 30, 1000) == 1
    assert washer.delay_minutes(1000, 1000) == 0
    assert washer.delay_minutes(900, 1000) == 0


def test_build_writes_minutes_and_mode(fixtures):
    built = washer.build_delayed_start(sel_fields(fixtures), ctx_fields(fixtures), 45)
    para = built[6][1]
    assert built[3][1] == 2  # parametrization
    assert para[1][1] == 45
    assert para[2][1] == 1  # the minutes mode this washer supports


def test_build_keeps_the_other_values(fixtures):
    plain = washer.build_parameters(sel_fields(fixtures), ctx_fields(fixtures))
    built = washer.build_delayed_start(sel_fields(fixtures), ctx_fields(fixtures), 45)
    for member in plain[6][1]:
        if member not in (1, 2):
            assert built[6][1][member] == plain[6][1][member]


def test_build_refuses_without_minutes_mode(fixtures):
    ctx = ctx_fields(fixtures)
    ctx[3][1][2][1][2][1] = 0  # supported bits
    ctx[3][1][2][1][3][1] = 0  # current
    with pytest.raises(washer.NotOfferedError, match="delayed start"):
        washer.build_delayed_start(sel_fields(fixtures), ctx, 45)


def test_build_refuses_when_not_changeable(fixtures):
    ctx = ctx_fields(fixtures)
    ctx[3][1][1][1][1][1] = 0  # request mask of delayed_start
    with pytest.raises(washer.NotOfferedError):
        washer.build_delayed_start(sel_fields(fixtures), ctx, 45)


def test_build_refuses_a_passed_moment(fixtures):
    with pytest.raises(washer.NotOfferedError, match="future"):
        washer.build_delayed_start(sel_fields(fixtures), ctx_fields(fixtures), 0)


def test_session_writes_delay_then_request_8(fixtures):
    from test_washer import FakeCloud, _run

    cloud = FakeCloud(fixtures, delay=0.0)
    session = washer.WasherSession(cloud.read_leaf, cloud.write_leaf)
    _run(session.start(delay_minutes=45))
    assert [(u, a) for u, a, _ in cloud.writes] == [(2, 1577), (2, 1583)]
    sel = decode(cloud.writes[0][2])[1]
    assert sel[6][1][1][1] == 45
    assert sel[6][1][2][1] == 1
    assert decode(cloud.writes[1][2])[1][1][1] == washer.USER_REQUEST_START_DELAYED


def test_session_plain_start_is_unchanged(fixtures):
    from test_washer import FakeCloud, _run

    cloud = FakeCloud(fixtures, delay=0.0)
    session = washer.WasherSession(cloud.read_leaf, cloud.write_leaf)
    _run(session.start())
    assert [(u, a) for u, a, _ in cloud.writes] == [(2, 1583)]
    assert decode(cloud.writes[0][2])[1][1][1] == washer.USER_REQUEST_START


def test_session_refused_delay_writes_nothing(fixtures):
    from test_washer import FakeCloud, _run

    cloud = FakeCloud(fixtures, delay=0.0)
    session = washer.WasherSession(cloud.read_leaf, cloud.write_leaf)
    with pytest.raises(washer.NotOfferedError):
        _run(session.start(delay_minutes=0))
    assert cloud.writes == []
