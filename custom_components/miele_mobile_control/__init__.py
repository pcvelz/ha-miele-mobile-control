"""The Miele Mobile Control integration.

From Home Assistant, do what the Miele app does for a washing machine
while Mobile Start is armed: choose program, temperature, spin speed and
extras, and start. Talks to the same cloud endpoints as the app and writes
the appliance's DOP2 program-selection object (2/1577).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import probatio
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_EMAIL,
    CONF_PASSWORD,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from . import const
from .api import MieleCloud
from .const import (
    ATTR_DETERGENT,
    ATTR_ECO,
    ATTR_EXTRAS,
    ATTR_PROGRAM,
    ATTR_SOILING,
    ATTR_SPIN_SPEED,
    ATTR_START,
    ATTR_START_AT,
    ATTR_TEMPERATURE,
    ATTR_TWINDOS,
    CONF_COUNTRY,
    DEFAULT_COUNTRY,
    DOMAIN,
    PLATFORMS,
    SERVICE_SET_PROGRAM,
)
from .coordinator import MieleConfigEntry, MieleCoordinator
from .power import PowerProfiles
from .profile import plugs_from_options
from .washer import validate_parameters

_LOGGER = logging.getLogger(__name__)

# A target in the action call: the washer's device, or an entity of it.
# Home Assistant merges the target keys into the service data before the
# schema runs, so device_id/entity_id/area_id arrive in call.data.
SET_PROGRAM_SCHEMA = probatio.Schema(
    {
        probatio.Optional(ATTR_DEVICE_ID): probatio.All(
            probatio.EnsureList(), [cv.string], probatio.Length(min=1)
        ),
        probatio.Optional(ATTR_ENTITY_ID): probatio.All(
            probatio.EnsureList(), [cv.string], probatio.Length(min=1)
        ),
        # Accepted so the handler can refuse it with a clear message instead
        # of the schema's opaque "extra keys not allowed".
        probatio.Optional(ATTR_AREA_ID): probatio.Any(cv.string, [cv.string]),
        probatio.Required(ATTR_PROGRAM): probatio.Any(
            cv.positive_int, cv.string
        ),
        probatio.Optional(ATTR_TEMPERATURE): probatio.All(
            probatio.Coerce(int), probatio.Range(min=0, max=95)
        ),
        probatio.Optional(ATTR_ECO): bool,
        probatio.Optional(ATTR_SPIN_SPEED): probatio.All(
            probatio.Coerce(int), probatio.Range(min=0, max=1800)
        ),
        probatio.Optional(ATTR_SOILING): probatio.All(
            probatio.Coerce(int), probatio.Range(min=1, max=3)
        ),
        probatio.Optional(ATTR_EXTRAS): probatio.All(
            probatio.EnsureList(), [cv.string]
        ),
        probatio.Optional(ATTR_DETERGENT): bool,
        probatio.Optional(ATTR_TWINDOS): const.twindos_value,
        probatio.Optional(ATTR_START, default=False): bool,
        # Arms the washer's own timer for this moment (a delayed start).
        probatio.Optional(ATTR_START_AT): cv.datetime,
    }
)

ATTR_KEY = "key"
SERVICE_FORGET_PROFILE = "forget_profile"

FORGET_PROFILE_SCHEMA = probatio.Schema(
    {
        probatio.Optional(ATTR_DEVICE_ID): probatio.All(
            probatio.EnsureList(), [cv.string], probatio.Length(min=1)
        ),
        probatio.Optional(ATTR_ENTITY_ID): probatio.All(
            probatio.EnsureList(), [cv.string], probatio.Length(min=1)
        ),
        probatio.Optional(ATTR_AREA_ID): probatio.Any(cv.string, [cv.string]),
        probatio.Optional(ATTR_KEY): cv.string,
    }
)

# How long to wait for the appliance to publish the newly selected program.
PROGRAM_SELECT_TIMEOUT = 10.0
PROGRAM_SELECT_POLL = 1.0


async def async_setup_entry(hass: HomeAssistant, entry: MieleConfigEntry) -> bool:
    """Set up the account and its washing machines."""
    api = MieleCloud(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
    )
    coordinator = MieleCoordinator(hass, entry, api)
    coordinator.async_restore_wash_records()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # Measured power profiles only exist for a washer with a plug in the
    # options; without one nothing is tracked, stored or created.
    plugs = plugs_from_options(entry.options)
    if plugs:
        coordinator.profiles = PowerProfiles(hass, entry, coordinator, plugs)
        await coordinator.profiles.async_setup()
    options_seen = dict(entry.options)

    async def _async_options_updated(
        hass: HomeAssistant, updated: MieleConfigEntry
    ) -> None:
        # Reload on an options change only (a reauth reloads by itself).
        if dict(updated.options) != options_seen:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_setup_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MieleConfigEntry) -> bool:
    """Unload the account."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    profiles = getattr(entry.runtime_data, "profiles", None)
    if unloaded and profiles is not None:
        await profiles.async_unload()
    return unloaded


@callback
def _async_setup_services(hass: HomeAssistant) -> None:
    """Register the set_program and forget_profile actions once."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_PROGRAM):
        return

    async def _async_set_program(call: ServiceCall) -> None:
        device_ids = _async_target_device_ids(hass, call)
        coordinator, fab, name = _coordinator_for_device(hass, device_ids[0])
        await _async_run_set_program(coordinator, fab, name, call.data)

    hass.services.async_register(
        DOMAIN, SERVICE_SET_PROGRAM, _async_set_program, schema=SET_PROGRAM_SCHEMA
    )

    async def _async_forget_profile(call: ServiceCall) -> None:
        device_ids = _async_target_device_ids(hass, call)
        coordinator, fab, name = _coordinator_for_device(hass, device_ids[0])
        profiles = coordinator.profiles
        if profiles is None or fab not in profiles.plugs:
            raise ServiceValidationError(
                f"{name} has no measuring plug configured; there are no profiles"
            )
        key = call.data.get(ATTR_KEY) or profiles.current_key(fab)
        if not key:
            raise ServiceValidationError(
                f"{name} shows no program combination; give the key to forget"
            )
        if not profiles.async_forget(key):
            raise ServiceValidationError(f"no stored profile for {key!r}")

    hass.services.async_register(
        DOMAIN, SERVICE_FORGET_PROFILE, _async_forget_profile,
        schema=FORGET_PROFILE_SCHEMA,
    )


def _async_target_device_ids(hass: HomeAssistant, call: ServiceCall) -> list[str]:
    """Resolve the action target to exactly one device id.

    Accepts the washer's device or an entity of it; anything else (an area,
    several devices, a foreign entity) is refused with a clear message.
    """
    if call.data.get(ATTR_AREA_ID):
        raise ServiceValidationError(
            "choose the washing machine's device or one of its entities, "
            "not an area"
        )
    device_ids = list(call.data.get(ATTR_DEVICE_ID) or [])
    entity_registry = er.async_get(hass)
    for entity_id in call.data.get(ATTR_ENTITY_ID) or []:
        entity = entity_registry.async_get(entity_id)
        if entity is None or entity.device_id is None:
            raise ServiceValidationError(
                f"entity {entity_id} does not belong to a device"
            )
        device_ids.append(entity.device_id)
    device_ids = list(dict.fromkeys(device_ids))
    if len(device_ids) != 1:
        raise ServiceValidationError(
            f"choose exactly one washing machine device, got {len(device_ids)}"
        )
    return device_ids


@callback
def _coordinator_for_device(
    hass: HomeAssistant, device_id: str
) -> tuple[MieleCoordinator, str, str]:
    """Resolve a device to (coordinator, fabrication number, device name)."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"unknown device {device_id}")
    name = device.name_by_user or device.name or "washer"
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        coordinator: MieleCoordinator = entry.runtime_data
        for identifier in device.identifiers:
            if identifier[0] == DOMAIN:
                return coordinator, identifier[1], name
    raise ServiceValidationError(f"{name} is not a Miele Mobile Control device")


async def _async_run_set_program(
    coordinator: MieleCoordinator,
    fab: str,
    name: str,
    data: dict[str, Any],
) -> None:
    """Select the program, write the parameters, optionally start.

    The order is fixed so a refused request never leaves the washer
    half-changed: the program is resolved before anything is written; when
    the requested program is already selected, every option is validated
    against the current context before any write; when it is a different
    program, the options can only be validated after the program change and
    a failure then says the program was changed.
    """
    washer = coordinator.data_for(fab)
    if washer is None or not washer.reachable:
        raise HomeAssistantError(f"{name} is not reachable")
    if not washer.armed or washer.status not in (2, 3):
        raise HomeAssistantError(
            "Switch the washer on and arm Mobile Start at the machine, "
            "then try again"
        )

    # Read the program context straight from the washer: a coordinator
    # refresh is debounced and can still hold the context of a previous
    # program.
    context = await coordinator.async_read_context(fab)
    if context is None:
        raise HomeAssistantError(
            f"could not read the current program of {name}; nothing was changed"
        )

    try:
        pid, changes, select_first = const.plan_set_program(
            program_value=data[ATTR_PROGRAM],
            program_ids=washer.program_ids,
            context=context,
            temperature=data.get(ATTR_TEMPERATURE),
            eco=data.get(ATTR_ECO, False),
            spin_rpm=data.get(ATTR_SPIN_SPEED),
            soiling=data.get(ATTR_SOILING),
            extras=data.get(ATTR_EXTRAS),
            detergent=data.get(ATTR_DETERGENT),
            twindos=data.get(ATTR_TWINDOS),
        )
    except ValueError as err:
        raise HomeAssistantError(str(err)) from err

    if select_first:
        await coordinator.async_select_program(fab, pid)
        context = await _async_wait_for_program(
            coordinator, fab, pid, data[ATTR_PROGRAM]
        )
        # The options can only be validated now, against the new program.
        # A failure here means the program WAS changed.
        try:
            if data.get(ATTR_EXTRAS) is not None:
                changes["extras"] = const.extras_change(data[ATTR_EXTRAS], context)
            validate_parameters(context, **changes)
        except ValueError as err:
            raise HomeAssistantError(
                const.options_rejected_after_program_change(pid, err)
            ) from err

    if changes:
        await coordinator.async_set_parameters(fab, **changes)

    # Verify through a fresh 2/1574 read straight from the washer.
    verify = await coordinator.async_read_context(fab)
    if verify is None or verify.prog_id != pid:
        raise HomeAssistantError(f"{name} dropped the program")
    problems = const.verify_written(verify, changes)
    if problems:
        raise HomeAssistantError("; ".join(problems))

    start_at = data.get(ATTR_START_AT)
    if start_at is not None:
        await coordinator.async_start(fab, at=dt_util.as_utc(start_at))
    elif data.get(ATTR_START):
        await coordinator.async_start(fab)


async def _async_wait_for_program(
    coordinator: MieleCoordinator, fab: str, pid: int, program_value: Any
) -> Any:
    """Poll 2/1574 until it shows the program (the app does the same)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PROGRAM_SELECT_TIMEOUT
    while True:
        context = await coordinator.async_read_context(fab)
        if context is not None and context.prog_id == pid:
            return context
        if loop.time() >= deadline:
            raise HomeAssistantError(
                f"the appliance did not take program {program_value!r}"
            )
        await asyncio.sleep(PROGRAM_SELECT_POLL)
