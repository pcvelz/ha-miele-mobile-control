"""Codec tests: every captured leaf must decode and re-encode byte for byte."""

from __future__ import annotations

import pytest

from custom_components.miele_mobile_control.dop2 import Dop2DecodeError, decode, encode


def test_round_trip_all_fixtures(fixtures):
    for name, buf in fixtures.items():
        header, fields = decode(buf)
        assert encode(header, fields) == buf, f"{name} did not round-trip"


def test_decode_headers(fixtures):
    header, _ = decode(fixtures["cottons_selected_2_1574_0_1"])
    assert header["unit"] == 2
    assert header["attr"] == 1574


def test_decode_rejects_garbage(fixtures):
    with pytest.raises(Dop2DecodeError):
        decode(b"\x00\x01\x02")
    buf = fixtures["on_no_program_2_1583_0_0"]
    with pytest.raises(Dop2DecodeError):
        decode(buf + b"\x00")


def test_nested_struct_and_array_values(fixtures):
    _, fields = decode(fixtures["on_no_program_2_1577_0_0"])
    para = fields[6][1]
    assert para[3][1] == 0  # temperature member, E2 (°C)
    assert 14 in para  # extras member is a struct keyed by extra id
    assert para[14][1][8][1] is False  # extra_quiet off
