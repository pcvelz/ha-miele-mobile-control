"""DataUpdateCoordinator for Miele Mobile Control.

Polls /State every 30 s. Only while Mobile Start is armed and the appliance
is on (Status 2/3/4) does it also read the program context 2/1574, and the
program list 2/1584 once per armed session. DOP2 reads are never attempted
while the appliance is off (Status 1): they 404, and a 404 means "no data".

All writes go through a per-appliance WasherSession (see washer.py) that
serializes read-modify-write so back-to-back changes cannot lose each other.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MieleApiError, MieleAuthError, MieleCloud, MieleConnectionError
from .const import DOMAIN, UPDATE_INTERVAL
from .dop2 import Dop2DecodeError, decode
from .washer import (
    ACTIVE_STATES,
    LEAF_PROGRAM_CONTEXT,
    LEAF_PROGRAM_LIST,
    StartRefusedError,
    WasherSession,
    WriteNotConfirmedError,
    mobile_start_armed,
    parse_context,
    parse_program_list,
    start_allowed,
)

_LOGGER = logging.getLogger(__name__)

type MieleConfigEntry = ConfigEntry[MieleCoordinator]


@dataclass
class WasherData:
    """Everything the entities need about one washing machine.

    *reachable* is False when the last /State poll of this washer failed; the
    entry then carries the last known data so entities keep working (and can
    show the washer as unavailable).
    """

    fab: str
    tech_type: str
    state: dict[str, Any]
    context: Any = None  # ProgramContext | None
    program_ids: list[int] = field(default_factory=list)
    reachable: bool = True

    @property
    def armed(self) -> bool:
        return mobile_start_armed(self.state)

    @property
    def status(self) -> int:
        return int(self.state.get("Status", 0) or 0)


class MieleCoordinator(DataUpdateCoordinator[list[WasherData]]):
    """Fetches state and, while armed, the DOP2 program context."""

    config_entry: MieleConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: MieleConfigEntry, api: MieleCloud
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{entry.domain}_{entry.entry_id}",
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.api = api
        # Program list cache, keyed by fab; kept only for the armed session.
        self._program_lists: dict[str, list[int]] = {}
        # Serialized write sessions per appliance.
        self._sessions: dict[str, WasherSession] = {}

    # ---------- lookup ----------

    def data_for(self, fab: str) -> WasherData | None:
        for washer in self.data or []:
            if washer.fab == fab:
                return washer
        return None

    def _session_for(self, fab: str) -> WasherSession:
        session = self._sessions.get(fab)
        if session is None:

            async def read_leaf(
                unit: int, attr: int, idx1: int, idx2: int
            ) -> bytes | None:
                return await self.api.async_read_leaf(
                    fab, unit, attr, idx1=idx1, idx2=idx2
                )

            async def write_leaf(unit: int, attr: int, leaf: bytes) -> None:
                await self.api.async_write_leaf(fab, unit, attr, leaf)

            session = WasherSession(read_leaf, write_leaf)
            self._sessions[fab] = session
        return session

    def _washer_or_raise(self, fab: str) -> WasherData:
        washer = self.data_for(fab)
        if washer is None or not washer.reachable:
            raise HomeAssistantError("the appliance is not reachable")
        return washer

    # ---------- polling ----------

    async def _async_update_data(self) -> list[WasherData]:
        try:
            devices = await self.api.async_get_devices()
        except MieleAuthError as err:
            raise ConfigEntryAuthFailed(
                "the Miele cloud refused the account credentials"
            ) from err
        except (MieleApiError, MieleConnectionError) as err:
            raise UpdateFailed(f"cannot list devices: {err}") from err

        previous = {w.fab: w for w in (self.data or [])}
        washers: list[WasherData] = []
        for device in devices:
            fab = device["fab"]
            try:
                state = await self.api.async_get_state(fab)
            except MieleAuthError as err:
                raise ConfigEntryAuthFailed(
                    "the Miele cloud refused the account credentials"
                ) from err
            except (MieleApiError, MieleConnectionError) as err:
                _LOGGER.debug("state of %s failed: %s", fab, err)
                stale = previous.get(fab)
                if stale is not None:
                    washers.append(
                        WasherData(
                            fab=stale.fab,
                            tech_type=stale.tech_type,
                            state=stale.state,
                            context=stale.context,
                            program_ids=stale.program_ids,
                            reachable=False,
                        )
                    )
                continue

            armed = mobile_start_armed(state)
            status = int(state.get("Status", 0) or 0)
            context = None
            try:
                if armed and status in ACTIVE_STATES:
                    context = await self._async_read_context(fab)
                    if fab not in self._program_lists:
                        program_ids = await self._async_read_program_list(fab)
                        if program_ids is not None:
                            self._program_lists[fab] = program_ids
            except MieleAuthError as err:
                # Auth failures of the DOP2 reads must surface as
                # ConfigEntryAuthFailed like the /State ones, otherwise the
                # entry is stuck on a generic update failure (and no reauth).
                raise ConfigEntryAuthFailed(
                    "the Miele cloud refused the account credentials"
                ) from err
            if not armed:
                self._program_lists.pop(fab, None)

            washers.append(
                WasherData(
                    fab=fab,
                    tech_type=device["tech_type"] or fab,
                    state=state,
                    context=context,
                    program_ids=self._program_lists.get(fab, []),
                )
            )
        return washers

    async def _async_read_context(self, fab: str) -> Any:
        """2/1574 at 0/1, parsed; 404 or a decode hiccup means no data."""
        try:
            raw = await self.api.async_read_leaf(
                fab, *LEAF_PROGRAM_CONTEXT, idx1=0, idx2=1
            )
        except MieleAuthError:
            raise
        except (MieleApiError, MieleConnectionError) as err:
            _LOGGER.debug("context of %s failed: %s", fab, err)
            return None
        if raw is None:
            return None
        try:
            return parse_context(decode(raw)[1])
        except (Dop2DecodeError, KeyError, IndexError, TypeError) as err:
            _LOGGER.debug("context of %s did not parse: %s", fab, err)
            return None

    async def _async_read_program_list(self, fab: str) -> list[int] | None:
        """2/1584 at 1/1 -> program ids; 404 means no data."""
        try:
            raw = await self.api.async_read_leaf(
                fab, *LEAF_PROGRAM_LIST, idx1=1, idx2=1
            )
        except MieleAuthError:
            raise
        except (MieleApiError, MieleConnectionError) as err:
            _LOGGER.debug("program list of %s failed: %s", fab, err)
            return None
        if raw is None:
            return None
        try:
            return parse_program_list(decode(raw)[1])
        except (Dop2DecodeError, KeyError, IndexError, TypeError) as err:
            _LOGGER.debug("program list of %s did not parse: %s", fab, err)
            return None

    # ---------- write confirmation ----------

    async def _async_read_parsed_context(self, fab: str) -> Any:
        """Freshly read and parsed 2/1574, or None on any hiccup."""
        try:
            raw = await self.api.async_read_leaf(
                fab, *LEAF_PROGRAM_CONTEXT, idx1=0, idx2=1
            )
            if raw is None:
                return None
            return parse_context(decode(raw)[1])
        except (MieleApiError, MieleConnectionError, Dop2DecodeError, KeyError,
                IndexError, TypeError):
            return None

    async def async_read_context(self, fab: str) -> Any:
        """The program context straight from the washer, not from the last poll.

        The set_program action needs this: a coordinator refresh is debounced,
        so right after a write the polled data can still be the old one.
        """
        return await self._async_read_parsed_context(fab)

    def _confirm_program(self, fab: str, prog_id: int):
        async def confirm() -> bool:
            ctx = await self._async_read_parsed_context(fab)
            return ctx is not None and ctx.prog_id == prog_id

        return confirm

    def _confirm_parameters(self, fab: str, changes: dict[str, Any]):
        async def confirm() -> bool:
            ctx = await self._async_read_parsed_context(fab)
            if ctx is None:
                return False
            temperature = changes.get("temperature")
            if temperature is not None and ctx.temperature.current != temperature:
                return False
            spin_rpm = changes.get("spin_rpm")
            if spin_rpm is not None and ctx.spin_rpm.current != spin_rpm:
                return False
            soiling = changes.get("soiling")
            if soiling is not None and ctx.soiling.current != soiling:
                return False
            for name, on in (changes.get("extras") or {}).items():
                extra = ctx.extras.get(name)
                if extra is None or extra.on != on:
                    return False
            return True

        return confirm

    def _confirm_started(self, fab: str):
        async def confirm() -> bool:
            try:
                state = await self.api.async_get_state(fab)
            except (MieleApiError, MieleConnectionError):
                return False
            return int(state.get("Status", 0) or 0) in (4, 5)

        return confirm

    async def _run_write(self, fab: str, coro: Awaitable[None]) -> None:
        """Run one session operation and translate its errors for the UI."""
        try:
            await coro
        except WriteNotConfirmedError as err:
            raise HomeAssistantError(str(err)) from err
        except MieleAuthError as err:
            raise HomeAssistantError(
                "the Miele cloud refused the account: re-authenticate the "
                "integration and try again"
            ) from err
        except (MieleApiError, MieleConnectionError) as err:
            raise HomeAssistantError(f"the appliance refused the change: {err}") from err
        except (Dop2DecodeError, ValueError) as err:
            raise HomeAssistantError(f"the appliance answer was unexpected: {err}") from err
        await self.async_request_refresh()

    # ---------- operations used by entities and the service ----------

    async def async_select_program(self, fab: str, prog_id: int) -> None:
        """Write 2/1577 type 0 with the chosen program."""
        self._armed_or_raise_guard(fab)
        await self._run_write(
            fab,
            self._session_for(fab).select_program(
                prog_id, confirm=self._confirm_program(fab, prog_id)
            ),
        )
        _LOGGER.debug("%s: program %d selected", fab, prog_id)

    def _armed_or_raise_guard(self, fab: str) -> WasherData:
        """Entities only call while available, but re-check before writing."""
        washer = self._washer_or_raise(fab)
        if not washer.armed or washer.status not in (2, 3):
            raise HomeAssistantError(
                "Switch the washer on and arm Mobile Start at the machine, "
                "then try again",
                translation_domain=DOMAIN,
                translation_key="mobile_start_not_armed",
            )
        return washer

    async def async_set_parameters(
        self,
        fab: str,
        *,
        temperature: int | None = None,
        eco: bool = False,
        spin_rpm: int | None = None,
        soiling: int | None = None,
        extras: dict[str, bool] | None = None,
    ) -> None:
        """Apply one change set via the serialized session write."""
        self._armed_or_raise_guard(fab)
        changes: dict[str, Any] = {
            "temperature": temperature,
            "eco": eco,
            "spin_rpm": spin_rpm,
            "soiling": soiling,
            "extras": extras,
        }
        await self._run_write(
            fab,
            self._session_for(fab).set_parameters(
                confirm=self._confirm_parameters(fab, changes), **changes
            ),
        )
        _LOGGER.debug("%s: parameters written", fab)

    async def async_start(self, fab: str) -> None:
        """Send Mobile Start; every outcome is reported honestly.

        The guard on the polled state is only a fast pre-check with a clear
        message; the session then reads /State FRESH from the cloud under
        the appliance lock (right after a program select the poll can still
        say "on"), re-checks program + Mobile Start there, refuses a second
        start within the cooldown, and raises when the washer does not
        confirm (Status 4/5).
        """
        washer = self._washer_or_raise(fab)
        if not start_allowed(washer.state):
            raise HomeAssistantError(
                "start is only offered while a program is selected (the "
                "washer shows Status 3) and Mobile Start is armed"
            )

        async def fresh_state() -> dict[str, Any]:
            return await self.api.async_get_state(fab)

        try:
            await self._session_for(fab).start(
                confirm=self._confirm_started(fab), state_check=fresh_state
            )
        except StartRefusedError as err:
            raise HomeAssistantError(str(err)) from err
        except WriteNotConfirmedError as err:
            raise HomeAssistantError(str(err)) from err
        except MieleAuthError as err:
            raise HomeAssistantError(
                "the Miele cloud refused the account: re-authenticate the "
                "integration and try again"
            ) from err
        except (MieleApiError, MieleConnectionError) as err:
            # The fresh-state check, the leaf read or the PUT failed; the
            # start was never confirmed, so say exactly that.
            raise HomeAssistantError(
                "the start could not be delivered to the washer and likely "
                "did not begin; check the machine before trying again"
            ) from err
        except (Dop2DecodeError, ValueError) as err:
            raise HomeAssistantError(
                f"the appliance answer was unexpected: {err}"
            ) from err
        _LOGGER.debug("%s: start requested", fab)
        await self.async_request_refresh()
