"""Options flow for the Grocy integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import OptionsFlowWithReload, ConfigFlowResult

from .const import CONF_GROCY_URL, CONF_API_KEY, STEP_OPTIONS_DATA_SCHEMA

_LOGGER = logging.getLogger(__name__)


async def validate_grocy_connection(url: str, api_key: str) -> None:
    """Validate the Grocy connection by calling the system info endpoint.

    Raises ValueError with an error key on failure.
    """
    clean_url = url.rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{clean_url}/api/system/info",
                headers={"GROCY-API-KEY": api_key},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    raise ValueError("invalid_auth")
                if resp.status != 200:
                    raise ValueError("cannot_connect")
    except aiohttp.ClientError as err:
        _LOGGER.debug("Grocy connection error: %s", err)
        raise ValueError("cannot_connect") from err


class GrocyOptionsFlow(OptionsFlowWithReload):
    """Handle an options flow for Grocy."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
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
                    data={
                        CONF_GROCY_URL: user_input[CONF_GROCY_URL].rstrip("/"),
                        CONF_API_KEY: user_input[CONF_API_KEY],
                    }
                )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                STEP_OPTIONS_DATA_SCHEMA, self.config_entry.options
            ),
            errors=errors,
        )
