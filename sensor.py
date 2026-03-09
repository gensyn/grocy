"""Platform for sensor integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN, CONF_GROCY_URL, CONF_API_KEY

LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor platform from a config entry."""
    data = entry.data
    options = entry.options
    async_add_entities(
        [
            GrocySensor(
                data[CONF_NAME],
                options[CONF_GROCY_URL],
                options[CONF_API_KEY],
                entry.entry_id,
                hass,
            )
        ]
    )


class GrocySensor(SensorEntity):
    """Representation of a Grocy sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry_name: str,
        grocy_url: str,
        api_key: str,
        entry_id: str,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the Grocy sensor."""
        self.entry_name = entry_name
        self.grocy_url = grocy_url
        self.api_key = api_key
        self.entry_id = entry_id

        device_id = f"{DOMAIN}_{self.entry_id}"
        self._attr_name = None
        self._attr_unique_id = f"{self.entry_id}_grocy"
        self.entity_id = generate_entity_id(
            "sensor.grocy_{}", slugify(entry_name), hass=hass
        )
        self._attr_native_value = "connected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            manufacturer="Gensyn",
            model="Grocy",
            name=entry_name,
            configuration_url=grocy_url,
        )

    async def async_get_recipes(self) -> list[dict]:
        """Get all recipes from the Grocy instance."""
        return await self._api_get("objects/recipes")

    async def async_get_recipe_ingredients(
        self, recipe_id: str | int
    ) -> list[dict]:
        """Get ingredients for a recipe with resolved product names and quantity units.

        Each returned dict contains:
        - product_name: str
        - amount: the raw amount value from Grocy (float or empty string)
        - unit_name: str – the resolved quantity unit name (may be empty)
        - skip: bool – True for ingredients in the 'Gewürze' product group
        """
        positions = await self._api_get(
            "objects/recipes_pos",
            params={"query[]": f"recipe_id={recipe_id}"},
        )

        # Caches to avoid redundant API calls within the same request
        qu_cache: dict[int | str, str] = {}
        pg_cache: dict[int | str, str] = {}

        ingredients = []
        for pos in positions:
            product_name = str(pos.get("product_id", "Unknown"))
            skip = False

            # Resolve product name and check product group
            product_id = pos.get("product_id")
            if product_id is not None:
                try:
                    product = await self._api_get(f"objects/products/{product_id}")
                    product_name = product.get("name", str(product_id))

                    product_group_id = product.get("product_group_id")
                    if product_group_id is not None:
                        if product_group_id not in pg_cache:
                            try:
                                pg = await self._api_get(
                                    f"objects/product_groups/{product_group_id}"
                                )
                                pg_cache[product_group_id] = pg.get("name", "")
                            except Exception:  # noqa: BLE001
                                LOGGER.debug(
                                    "Could not fetch product group %s", product_group_id
                                )
                                pg_cache[product_group_id] = ""
                        if pg_cache.get(product_group_id) == "Gewürze":
                            skip = True
                except Exception:  # noqa: BLE001
                    LOGGER.debug("Could not fetch product %s", product_id)

            # Resolve quantity unit name
            qu_id = pos.get("qu_id")
            unit_name = ""
            if qu_id is not None:
                if qu_id not in qu_cache:
                    try:
                        qu = await self._api_get(f"objects/quantity_units/{qu_id}")
                        qu_cache[qu_id] = qu.get("name", "")
                    except Exception:  # noqa: BLE001
                        LOGGER.debug("Could not fetch quantity unit %s", qu_id)
                        qu_cache[qu_id] = ""
                unit_name = qu_cache.get(qu_id, "")

            ingredients.append(
                {
                    "product_name": product_name,
                    "amount": pos.get("amount", ""),
                    "unit_name": unit_name,
                    "skip": skip,
                }
            )
        return ingredients

    async def _api_get(
        self, endpoint: str, params: dict | None = None
    ) -> Any:
        """Make an authenticated GET request to the Grocy API."""
        url = f"{self.grocy_url}/api/{endpoint}"
        headers = {"GROCY-API-KEY": self.api_key}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except aiohttp.ClientResponseError as err:
            LOGGER.error(
                "Grocy API %s returned status %s: %s", endpoint, err.status, err
            )
            raise HomeAssistantError(
                f"Grocy API error for {endpoint}: {err}"
            ) from err
        except aiohttp.ClientError as err:
            LOGGER.error("Error calling Grocy API %s: %s", endpoint, err)
            raise HomeAssistantError(
                f"Error calling Grocy API: {err}"
            ) from err
