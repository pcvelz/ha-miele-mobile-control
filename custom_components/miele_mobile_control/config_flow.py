"""Config flow for Miele Mobile Control.

Logs in with the Miele account the way the app does, to prove the
credentials before creating the entry. The unique id is the lowercased
e-mail address; a 401 later triggers the reauth flow.
"""

from __future__ import annotations

from typing import Any, override

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import MieleApiError, MieleAuthError, MieleCloud, MieleConnectionError
from .const import CONF_COUNTRY, DEFAULT_COUNTRY, DOMAIN


def _country_default(hass: HomeAssistant) -> str:
    return (hass.config.country or DEFAULT_COUNTRY).upper()


def _schema(hass: HomeAssistant, default_country: str | None = None) -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_EMAIL): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL)
            ),
            probatio.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            probatio.Optional(
                CONF_COUNTRY, default=default_country or _country_default(hass)
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        }
    )


def _reauth_schema() -> probatio.Schema:
    """Reauth asks for the password only: the account is fixed (it is the
    entry's unique id), its e-mail is shown in the form description."""
    return probatio.Schema(
        {
            probatio.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
        }
    )


class MieleMobileControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def _async_try_login(self, user_input: dict[str, Any]) -> None:
        cloud = MieleCloud(
            async_get_clientsession(self.hass),
            user_input[CONF_EMAIL],
            user_input[CONF_PASSWORD],
            user_input.get(CONF_COUNTRY),
        )
        await cloud.async_login()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._async_try_login(user_input)
            except MieleAuthError:
                errors["base"] = "invalid_auth"
            except MieleConnectionError:
                errors["base"] = "cannot_connect"
            except MieleApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_COUNTRY: user_input.get(
                            CONF_COUNTRY, _country_default(self.hass)
                        ),
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _schema(self.hass), user_input
            ),
            errors=errors,
        )

    @override
    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    @override
    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            # Verify against the account this entry belongs to — never
            # against a typed e-mail — and keep the stored country.
            attempt = {
                CONF_EMAIL: reauth_entry.data[CONF_EMAIL],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_COUNTRY: reauth_entry.data.get(CONF_COUNTRY),
            }
            try:
                await self._async_try_login(attempt)
            except MieleAuthError:
                errors["base"] = "invalid_auth"
            except MieleConnectionError:
                errors["base"] = "cannot_connect"
            except MieleApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                self._abort_if_unique_id_mismatch()
                # Reloads the entry so the running client picks up the new
                # password instead of retrying with the old one.
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema(),
            errors=errors,
            description_placeholders={CONF_EMAIL: reauth_entry.data[CONF_EMAIL]},
        )
