"""Async client for the Miele app cloud ("DOP2 tunnel").

Same endpoints the Miele app uses after pairing:
  POST /V3/Login?org=<CC>            email + password -> appToken
  GET  /V2/Devices                   wifi modules and their appliances
  GET  /V2/Devices/{fab}/State       appliance state (JSON)
  GET/PUT /V2/Devices/{fab}/DOP2/... binary DOP2 leaves

The token is reused until one day before its validity, then refreshed; a 401
gets one re-login and retry. Secrets are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from .const import DEFAULT_COUNTRY

_LOGGER = logging.getLogger(__name__)

REST_BASE = "https://rest-eu.domestic.miele-iot.com"
JSON_ACCEPT = "application/vnd.miele.v1+json"
DOP2_ACCEPT = "application/vnd.miele.v1+dop2"
DOP2_CONTENT = "application/vnd.miele.v1+dop2"
LOGIN_CONTENT = "application/json; charset=utf-8"

# Renew the token once it is closer than this to its expiry.
TOKEN_RENEW_MARGIN = 24 * 60 * 60

DEVICE_TYPE_WASHING_MACHINE = 1

_REQUEST_TIMEOUT = ClientTimeout(total=25)


class MieleApiError(Exception):
    """The cloud or the appliance refused a request."""


class MieleAuthError(MieleApiError):
    """The account credentials were refused."""


class MieleConnectionError(MieleApiError):
    """The cloud could not be reached."""


class MieleCloud:
    """One Miele account session."""

    def __init__(
        self,
        session: ClientSession,
        email: str,
        password: str,
        country: str | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._country = (country or DEFAULT_COUNTRY).upper()
        self._token: str | None = None
        self._token_valid_until: float = 0.0
        self._login_lock = asyncio.Lock()

    # ---------- auth ----------

    @property
    def email(self) -> str:
        return self._email

    @property
    def country(self) -> str:
        return self._country

    async def async_login(self) -> None:
        """Log in and cache the appToken (also called for renewal)."""
        async with self._login_lock:
            url = f"{REST_BASE}/V3/Login?org={self._country}"
            try:
                async with self._session.post(
                    url,
                    json={"email": self._email, "password": self._password},
                    headers={
                        "Content-Type": LOGIN_CONTENT,
                        "Accept": "application/json",
                        "Accept-Language": f"en-{self._country}",
                    },
                    timeout=_REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status in (401, 403):
                        raise MieleAuthError("the Miele cloud refused the account")
                    if resp.status != 200:
                        raise MieleApiError(f"login answered HTTP {resp.status}")
                    data = await resp.json()
            except MieleApiError:
                raise
            except TimeoutError as err:
                raise MieleConnectionError("login timed out") from err
            except Exception as err:
                raise MieleConnectionError(f"login failed: {err}") from err
            try:
                self._token = data["appToken"]
                self._token_valid_until = float(data["appTokenValidity"])
            except (KeyError, TypeError, ValueError) as err:
                raise MieleApiError("login answer has no appToken") from err
            _LOGGER.debug(
                "login ok, token valid %s", time.ctime(self._token_valid_until)
            )

    def _token_due(self) -> bool:
        return (
            self._token is None
            or time.time() >= self._token_valid_until - TOKEN_RENEW_MARGIN
        )

    async def _async_ensure_token(self) -> str:
        if self._token_due():
            await self.async_login()
        assert self._token is not None
        return self._token

    # ---------- requests ----------

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        accept: str,
        body: bytes | None = None,
        content_type: str | None = None,
        retry_auth: bool = True,
    ) -> tuple[int, bytes]:
        token = await self._async_ensure_token()
        headers = {
            "Authorization": f"appToken {token}",
            "Accept-Language": f"en-{self._country}",
            "Accept": accept,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            async with self._session.request(
                method,
                f"{REST_BASE}{path}",
                headers=headers,
                data=body,
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                payload = await resp.read()
                status = resp.status
        except TimeoutError as err:
            raise MieleConnectionError(f"{method} {path} timed out") from err
        except MieleConnectionError:
            raise
        except Exception as err:
            raise MieleConnectionError(f"{method} {path} failed: {err}") from err

        if status == 401:
            if retry_auth:
                _LOGGER.debug("token refused, re-login and retry once")
                self._token_valid_until = 0.0
                return await self._async_request(
                    method,
                    path,
                    accept=accept,
                    body=body,
                    content_type=content_type,
                    retry_auth=False,
                )
            raise MieleAuthError("the Miele cloud refused the account credentials")
        return status, payload

    # ---------- resources ----------

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Appliances of all wifi modules of the account.

        Each entry: {"fab": str, "tech_type": str|None, "state": dict}.
        """
        status, payload = await self._async_request(
            "GET", "/V2/Devices", accept=JSON_ACCEPT
        )
        if status != 200:
            raise MieleApiError(f"/V2/Devices answered HTTP {status}")
        try:
            modules = json.loads(payload)
        except ValueError as err:
            raise MieleApiError("/V2/Devices returned no JSON") from err
        devices: list[dict[str, Any]] = []
        for module in modules:
            for device in module.get("devices", []):
                ident = device.get("ident", {})
                if ident.get("DeviceType") != DEVICE_TYPE_WASHING_MACHINE:
                    continue
                label = ident.get("DeviceIdentLabel", {})
                devices.append(
                    {
                        "fab": str(device.get("mac", "")),
                        "tech_type": label.get("TechType"),
                        "state": device.get("state", {}),
                    }
                )
        return devices

    async def async_get_state(self, fab: str) -> dict[str, Any]:
        """GET /V2/Devices/{fab}/State."""
        status, payload = await self._async_request(
            "GET", f"/V2/Devices/{fab}/State", accept=JSON_ACCEPT
        )
        if status != 200:
            _LOGGER.debug("State of %s answered HTTP %s", fab, status)
            raise MieleApiError(f"State answered HTTP {status}")
        try:
            return json.loads(payload)
        except ValueError as err:
            raise MieleApiError("State returned no JSON") from err

    async def async_read_leaf(
        self, fab: str, unit: int, attr: int, idx1: int = 0, idx2: int = 0
    ) -> bytes | None:
        """Read a DOP2 leaf; a 404 (e.g. appliance off) is no data, not an error."""
        status, payload = await self._async_request(
            "GET",
            f"/V2/Devices/{fab}/DOP2/{unit}/{attr}?idx1={idx1}&idx2={idx2}",
            accept=DOP2_ACCEPT,
        )
        if status == 404:
            return None
        if status != 200:
            _LOGGER.debug(
                "DOP2 %s/%s of %s answered HTTP %s", unit, attr, fab, status
            )
            raise MieleApiError(f"DOP2 {unit}/{attr} answered HTTP {status}")
        return payload

    async def async_write_leaf(
        self, fab: str, unit: int, attr: int, leaf: bytes
    ) -> None:
        """PUT a DOP2 leaf, unpadded, no query string; expects 204."""
        # A PUT to the user-request leaf (2/1583) fires the washer's Mobile
        # Start, which is spent once per arming. Retrying it after a 401
        # would send the start a second time and can cost the owner a trip
        # to the machine, so the auth retry is disabled for that leaf only.
        # Other writes carry their full desired state, so re-sending one of
        # those after a re-login is idempotent and keeps the single retry.
        retry_auth = (unit, attr) != (2, 1583)
        status, _payload = await self._async_request(
            "PUT",
            f"/V2/Devices/{fab}/DOP2/{unit}/{attr}",
            accept=DOP2_ACCEPT,
            body=leaf,
            content_type=DOP2_CONTENT,
            retry_auth=retry_auth,
        )
        if status == 403:
            raise MieleApiError(
                "remote control is disabled at the machine: arm Mobile Start "
                "and try again"
            )
        if status != 204:
            _LOGGER.debug(
                "write DOP2 %s/%s of %s answered HTTP %s",
                unit,
                attr,
                fab,
                status,
            )
            raise MieleApiError(
                f"write DOP2 {unit}/{attr} answered HTTP {status}"
            )
