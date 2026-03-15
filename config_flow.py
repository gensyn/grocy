"""Config flow for the Grocy integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import callback

from .const import DOMAIN, CONF_GROCY_URL, CONF_API_KEY, STEP_USER_DATA_SCHEMA
from .options_flow import GrocyOptionsFlow, validate_grocy_connection

_LOGGER = logging.getLogger(__name__)


class GrocyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Grocy."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await validate_grocy_connection(
                    user_input[CONF_GROCY_URL], user_input[CONF_API_KEY]
                )
            except ValueError as err:
                errors["base"] = str(err)
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_NAME: user_input[CONF_NAME]},
                    options={
                        CONF_GROCY_URL: user_input[CONF_GROCY_URL].rstrip("/"),
                        CONF_API_KEY: user_input[CONF_API_KEY],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> GrocyOptionsFlow:
        """Create the options flow."""
        return GrocyOptionsFlow()
