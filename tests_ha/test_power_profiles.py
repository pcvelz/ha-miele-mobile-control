"""Measured power profiles inside a real Home Assistant (phacc).

The owner: "in configuration of the integration we can select a plug to
measure the usage from. We should have a TDD for both the scenario with the
plug and without the plug. Ideally we should focus on the scenario with the
power measuring plug."
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.miele_mobile_control.const import DOMAIN

from conftest import FAB, STATE_ARMED_COTTONS, FakeCloud

POWER = "sensor.plug_power"
ENERGY = "sensor.plug_energy"
PROFILE_ENTITIES = (
    "sensor.wsi863_wash_energy",
    "sensor.wsi863_power_profile",
    "sensor.wsi863_power_profiles",
    "sensor.wsi863_power_plug",
)


def _entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="owner@example.invalid",
        data={"email": "owner@example.invalid", "password": "x", "country": "NL"},
        options=options or {},
    )


def _plug_options() -> dict:
    return {"plugs": {FAB: {"power_sensor": POWER, "energy_sensor": ENERGY}}}


def _set_plug(hass: HomeAssistant, watts: str, kwh: str) -> None:
    hass.states.async_set(POWER, watts, {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(ENERGY, kwh, {"unit_of_measurement": "kWh", "device_class": "energy"})


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def _poll(hass: HomeAssistant, entry: MockConfigEntry, status: int) -> None:
    FakeCloud.state = {**STATE_ARMED_COTTONS, "Status": status}
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


# ---------- WITHOUT the plug ----------


async def test_without_plug_nothing_new(hass: HomeAssistant, hass_storage, caplog):
    entry = _entry()
    await _setup(hass, entry)
    assert hass.states.get("sensor.wsi863_status") is not None
    for entity_id in PROFILE_ENTITIES:
        assert hass.states.get(entity_id) is None
    assert entry.runtime_data.profiles is None
    await _poll(hass, entry, 4)
    await _poll(hass, entry, 5)
    await _poll(hass, entry, 7)
    assert hass.states.get("sensor.wsi863_wash_state").state == "finished"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not [k for k in hass_storage if k.startswith(DOMAIN)]
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


# ---------- WITH the plug ----------


async def test_with_plug_a_wash_is_measured_stored_and_persisted(
    hass: HomeAssistant, hass_storage, freezer
):
    _set_plug(hass, "3", "2.500")
    entry = _entry(_plug_options())
    await _setup(hass, entry)
    for entity_id in PROFILE_ENTITIES:
        assert hass.states.get(entity_id) is not None, entity_id
    selected = hass.states.get("sensor.wsi863_power_profile")
    assert selected.state == "0"  # never measured
    key = selected.attributes["key"]
    assert key and key.startswith("Cottons|")
    assert hass.states.get("sensor.wsi863_power_plug").state == POWER

    await _poll(hass, entry, 4)  # waiting to start: not measured
    freezer.tick(timedelta(minutes=10))
    _set_plug(hass, "600", "2.500")
    await _poll(hass, entry, 5)  # minute 0 starts here
    for _ in range(5):
        freezer.tick(timedelta(minutes=1))
        await _poll(hass, entry, 5)

    # Reload mid-wash (as a restart would): the partial recording is saved.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert any(k.startswith(DOMAIN) for k in hass_storage)

    for _ in range(5):
        freezer.tick(timedelta(minutes=1))
        _set_plug(hass, "600", "2.500")
        await _poll(hass, entry, 5)
    running = hass.states.get("sensor.wsi863_wash_energy")
    assert running.attributes["measuring"] is True

    _set_plug(hass, "600", "2.600")
    await _poll(hass, entry, 7)

    energy = hass.states.get("sensor.wsi863_wash_energy")
    assert float(energy.state) == pytest.approx(0.1)
    assert energy.attributes["from_energy_sensor"] is True
    assert energy.attributes["minutes"] == 10
    assert energy.attributes["missing_minutes"] <= 1
    assert energy.attributes["stored"] is True
    assert energy.attributes["key"] == key

    assert hass.states.get("sensor.wsi863_power_profiles").state == "1"
    profile = hass.states.get("sensor.wsi863_power_profile")
    assert profile.state == "1"
    assert profile.attributes["key"] == key
    assert profile.attributes["minutes"] == 10
    assert profile.attributes["wh"][0] == pytest.approx(10.0)

    # forget_profile without a key removes the key shown now.
    await hass.services.async_call(
        DOMAIN, "forget_profile",
        {"entity_id": "sensor.wsi863_power_profile"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.wsi863_power_profiles").state == "0"


# ---------- the options flow ----------


async def test_options_flow_adds_and_removes_the_plug(hass: HomeAssistant):
    _set_plug(hass, "3", "2.5")
    entry = _entry()
    await _setup(hass, entry)
    assert hass.states.get("sensor.wsi863_power_profile") is None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "plug"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"power_sensor": POWER}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {"plugs": {FAB: {"power_sensor": POWER}}}
    assert hass.states.get("sensor.wsi863_power_profile") is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options == {"plugs": {}}
    registry = er.async_get(hass)
    # The entities are gone from the running set (their registry entries
    # stay, as for any removed entity, until the user deletes them).
    state = hass.states.get("sensor.wsi863_power_profile")
    assert state is None or state.state == "unavailable"
    assert registry.async_get("sensor.wsi863_status") is not None
