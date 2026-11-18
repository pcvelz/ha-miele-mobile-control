"""Session-level safety tests: confirmed-or-raise writes and a gated start.

Pure Python (asyncio only); the fake cloud serves the captured WSI863
leaves like tests/test_washer.py does, plus an aiohttp stub for the api
layer's auth-retry behaviour.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from custom_components.miele_mobile_control.dop2 import decode, encode
from custom_components.miele_mobile_control import api, washer


WSI863_PROGRAM_IDS = [
    146, 123, 190, 27, 23, 9, 8, 4, 3, 1, 69, 37, 122, 29, 31, 39, 22,
    129, 53, 95,
]

ARMED_PROGRAMMED = {"Status": 3, "RemoteEnable": [15, 0, 1]}
ARMED_ON = {"Status": 2, "RemoteEnable": [15, 0, 1]}


class FakeCloud:
    """Minimal async stand-in: serves the captured leaves, records writes."""

    def __init__(self, fixtures, delay=0.02) -> None:
        self.delay = delay
        self.usr = decode(fixtures["on_no_program_2_1583_0_0"])[1]
        self.sel = decode(fixtures["on_no_program_2_1577_0_0"])[1]
        self.usr_header = {"unit": 2, "attr": 1583, "idx1": 0, "idx2": 0}
        self.sel_header = {"unit": 2, "attr": 1577, "idx1": 0, "idx2": 0}
        self.writes = []  # (unit, attr, leaf bytes), in order

    async def read_leaf(self, unit, attr, idx1=0, idx2=0):
        await asyncio.sleep(self.delay)
        if (unit, attr) == (2, 1583):
            return encode(self.usr_header, self.usr)
        if (unit, attr) == (2, 1577):
            return encode(self.sel_header, self.sel)
        return None

    async def write_leaf(self, unit, attr, leaf):
        await asyncio.sleep(self.delay)
        self.writes.append((unit, attr, leaf))


def _run(coro):
    return asyncio.run(coro)


async def _armed_programmed():
    return ARMED_PROGRAMMED


# ---------- unconfirmed writes raise, they are never silent ----------


def test_unconfirmed_write_raises(fixtures):
    cloud = FakeCloud(fixtures)
    session = washer.WasherSession(
        cloud.read_leaf, cloud.write_leaf, confirm_timeout=0.1
    )

    async def never():
        return False

    with pytest.raises(washer.WriteNotConfirmedError, match="did not confirm"):
        _run(session.select_program(1, confirm=never))

    assert len(cloud.writes) == 1


def test_start_not_confirmed_raises_and_cools_down(fixtures):
    """A start the washer does not confirm (no Status 4/5) must fail loudly,
    and the cooldown must block an immediate second attempt."""
    cloud = FakeCloud(fixtures)

    async def never():
        return False

    async def scenario():
        session = washer.WasherSession(
            cloud.read_leaf, cloud.write_leaf, confirm_timeout=0.1
        )
        with pytest.raises(washer.WriteNotConfirmedError):
            await session.start(confirm=never, state_check=_armed_programmed)
        # one write went out; the cooldown now refuses a second start
        with pytest.raises(washer.StartRefusedError, match="moment ago"):
            await session.start(confirm=never, state_check=_armed_programmed)

    _run(scenario())
    assert len(cloud.writes) == 1


# ---------- start gating ----------


def test_start_refused_without_program(fixtures):
    """state_check saying "on, no program" refuses before anything is read
    or written."""
    cloud = FakeCloud(fixtures)
    reads = []

    async def read_spy(unit, attr, idx1=0, idx2=0):
        reads.append((unit, attr))
        return await cloud.read_leaf(unit, attr, idx1, idx2)

    session = washer.WasherSession(read_spy, cloud.write_leaf)

    async def on_no_program():
        return ARMED_ON

    with pytest.raises(washer.StartRefusedError, match="program is selected"):
        _run(session.start(confirm=None, state_check=on_no_program))

    assert cloud.writes == []
    assert reads == []  # refused before the leaf read


def test_start_runs_state_check_before_write(fixtures):
    """state_check runs under the lock, immediately before the write, and
    a passing check lets the start through."""
    cloud = FakeCloud(fixtures)
    order = []

    async def state_check():
        order.append("state_check")
        return ARMED_PROGRAMMED

    original_write = cloud.write_leaf

    async def write_spy(unit, attr, leaf):
        order.append("write")
        await original_write(unit, attr, leaf)

    async def confirm():
        order.append("confirm")
        return True

    session = washer.WasherSession(cloud.read_leaf, write_spy)
    _run(session.start(confirm=confirm, state_check=state_check))

    assert order == ["state_check", "write", "confirm"]
    assert len(cloud.writes) == 1
    _unit, _attr, leaf = cloud.writes[-1]
    assert decode(leaf)[1][1][1] == 1


def test_second_start_within_cooldown_refused_and_nothing_written(fixtures):
    cloud = FakeCloud(fixtures)
    session = washer.WasherSession(cloud.read_leaf, cloud.write_leaf)

    async def confirm():
        return True

    async def scenario():
        await session.start(confirm=confirm, state_check=_armed_programmed)
        with pytest.raises(washer.StartRefusedError, match="moment ago"):
            await session.start(confirm=confirm, state_check=_armed_programmed)

    _run(scenario())
    assert len(cloud.writes) == 1


# ---------- locked extras ----------


def _locked_context(fixtures):
    """Cottons context with 'quick' shown but locked (mask without bit 0)."""
    context = washer.parse_context(
        decode(fixtures["cottons_selected_2_1574_0_1"])[1]
    )
    quick = context.extras["quick"]
    context.extras["quick"] = washer.Extra(mask=quick.mask & ~1, on=quick.on)
    return context


def test_locked_extra_refused_when_changed(fixtures):
    context = _locked_context(fixtures)
    with pytest.raises(washer.NotOfferedError, match="locked"):
        washer.validate_parameters(context, extras={"quick": True})


def test_locked_extra_untouched_when_not_mentioned(fixtures):
    """Not mentioning a locked extra must pass, and build_parameters must
    leave it at the appliance value."""
    context = _locked_context(fixtures)
    washer.validate_parameters(context, spin_rpm=800)

    _, sel = decode(fixtures["on_no_program_2_1577_0_0"])
    _, ctx_fields = decode(fixtures["cottons_selected_2_1574_0_1"])
    built = washer.build_parameters(sel, ctx_fields, spin_rpm=800)
    para = built[6][1]
    quick_member = washer.EXTRAS["quick"]
    # copied from the appliance value, not forced to anything
    assert para[14][1][quick_member][1] == context.extras["quick"].on


def test_locked_extra_unchanged_value_allowed(fixtures):
    """Writing the value the extra already has is not a change: allowed."""
    context = _locked_context(fixtures)
    washer.validate_parameters(
        context, extras={"quick": context.extras["quick"].on}
    )


# ---------- program_id against the machine's own list ----------


def test_program_id_name_resolves_against_machine_list():
    assert washer.program_id("Denim", WSI863_PROGRAM_IDS) == 123
    assert washer.program_id("denim", WSI863_PROGRAM_IDS) == 123
    assert washer.program_id("QuickPowerWash", WSI863_PROGRAM_IDS) == 146


def test_program_id_number_not_offered_raises():
    with pytest.raises(ValueError, match="not offered.*Denim \\(123\\)"):
        washer.program_id(24, WSI863_PROGRAM_IDS)
    with pytest.raises(ValueError, match="not offered"):
        washer.program_id("24", WSI863_PROGRAM_IDS)


def test_program_id_without_list_keeps_old_behaviour():
    assert washer.program_id("Denim") == 24  # first table entry
    assert washer.program_id(9999) == 9999


def test_program_label_disambiguates_shared_names():
    assert washer.program_label(123, WSI863_PROGRAM_IDS) == "Denim"
    both = [24, 123]
    assert washer.program_label(24, both) == "Denim (24)"
    assert washer.program_label(123, both) == "Denim (123)"


# ---------- api: a 401 on the start write is never retried ----------


class _FakeResponse:
    def __init__(self, status, payload=b"{}") -> None:
        self.status = status
        self._payload = payload

    async def read(self):
        return self._payload

    async def json(self):
        return json.loads(self._payload)


class _FakeRequestContext:
    def __init__(self, response) -> None:
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers login, then serves a scripted status per request."""

    def __init__(self, put_statuses) -> None:
        self.calls = []  # (method, url, authorization header)
        self._put_statuses = list(put_statuses)
        self._logins = 0

    def post(self, url, **kwargs):
        auth = kwargs.get("headers", {}).get("Authorization")
        self.calls.append(("POST", url, auth))
        self._logins += 1
        payload = json.dumps(
            {
                "appToken": f"tok{self._logins}",
                "appTokenValidity": 9999999999,
            }
        ).encode()
        return _FakeRequestContext(_FakeResponse(200, payload))

    def request(self, method, url, **kwargs):
        auth = kwargs.get("headers", {}).get("Authorization")
        self.calls.append((method, url, auth))
        if method == "PUT":
            status = self._put_statuses.pop(0)
        else:
            status = 200
        return _FakeRequestContext(_FakeResponse(status, b"{}"))


def test_start_write_401_is_not_retried():
    cloud = api.MieleCloud(FakeSession([401, 204]), "e@example.com", "pw")
    with pytest.raises(api.MieleAuthError):
        _run(cloud.async_write_leaf("fab1", 2, 1583, b"\x00\x01"))
    puts = [c for c in cloud._session.calls if c[0] == "PUT"]
    assert len(puts) == 1  # exactly one, no retry after the 401
    assert [c[0] for c in cloud._session.calls].count("POST") == 1


def test_other_write_401_retried_once():
    cloud = api.MieleCloud(FakeSession([401, 204]), "e@example.com", "pw")
    _run(cloud.async_write_leaf("fab1", 2, 1577, b"\x00\x01"))
    puts = [c for c in cloud._session.calls if c[0] == "PUT"]
    assert len(puts) == 2


def test_retry_after_401_uses_fresh_token():
    cloud = api.MieleCloud(FakeSession([401, 204]), "e@example.com", "pw")
    _run(cloud.async_write_leaf("fab1", 2, 1577, b"\x00\x01"))
    puts = [c for c in cloud._session.calls if c[0] == "PUT"]
    # first try with the expired token, retry with the re-login's token
    assert [auth for _m, _u, auth in puts] == ["appToken tok1", "appToken tok2"]
