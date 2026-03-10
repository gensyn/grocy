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

# Mapping from Grocy quantity unit long names (German and English) to
# their common abbreviations.  Both singular and plural forms are included.
_UNIT_ABBREVIATIONS: dict[str, str] = {
    # Mass – German
    "Gramm": "g",
    "Kilogramm": "kg",
    "Milligramm": "mg",
    # Mass – English
    "gram": "g",
    "grams": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "milligram": "mg",
    "milligrams": "mg",
    "ounce": "oz",
    "ounces": "oz",
    "pound": "lb",
    "pounds": "lb",
    # Volume – German
    "Liter": "l",
    "Milliliter": "ml",
    "Deziliter": "dl",
    "Zentiliter": "cl",
    "Esslöffel": "EL",
    "Teelöffel": "TL",
    "Tasse": "Tasse",
    # Volume – English
    "liter": "l",
    "litre": "l",
    "liters": "l",
    "litres": "l",
    "milliliter": "ml",
    "millilitre": "ml",
    "milliliters": "ml",
    "millilitres": "ml",
    "deciliter": "dl",
    "decilitre": "dl",
    "centiliter": "cl",
    "centilitre": "cl",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "cup": "cup",
    "cups": "cup",
    "fluid ounce": "fl oz",
    "fluid ounces": "fl oz",
    # Piece / count – German
    "Stück": "Stk",
    "Stücke": "Stk",
    "Scheibe": "Scheibe",
    "Scheiben": "Scheiben",
    "Packung": "Pkg",
    "Packungen": "Pkg",
    "Dose": "Dose",
    "Dosen": "Dosen",
    "Flasche": "Fl",
    "Flaschen": "Fl",
    "Bund": "Bd",
    "Zehe": "Zehe",
    "Zehen": "Zehen",
    "Prise": "Pr",
    "Prisen": "Pr",
    # Piece / count – English
    "piece": "pc",
    "pieces": "pc",
    "slice": "slice",
    "slices": "slices",
    "clove": "clove",
    "cloves": "cloves",
    "bunch": "bunch",
    "can": "can",
    "cans": "cans",
    "bottle": "btl",
    "bottles": "btl",
    "package": "pkg",
    "packages": "pkg",
    "pinch": "pinch",
    "pinches": "pinch",
}


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

    async def async_get_products(self) -> list[dict]:
        """Get all products from the Grocy instance."""
        return await self._api_get("objects/products")

    async def async_get_stock(self) -> list[dict]:
        """Get the current stock overview from the Grocy instance.

        Returns a list of stock entries.  Each entry contains at least
        ``product_id`` and ``amount``.  Products with no stock entries at all
        will be absent from this list.
        """
        return await self._api_get("stock")

    async def async_get_quantity_unit(self, qu_id: int | str) -> dict:
        """Get a single quantity unit object by its ID."""
        return await self._api_get(f"objects/quantity_units/{qu_id}")

    async def async_get_recipe_ingredients(
        self, recipe_id: str | int
    ) -> list[dict]:
        """Get ingredients for a recipe with resolved product names and quantity units.

        Grocy stores recipe position amounts in the product's stock quantity unit
        (e.g. 20 g) while qu_id carries the recipe display unit (e.g. Esslöffel).
        When the two differ, the factor from quantity_unit_conversions is used to
        convert back to the recipe display amount (e.g. 20 g / 10 = 2 EL).

        Each returned dict contains:
        - product_name: str
        - amount: display amount (converted to recipe QU when possible)
        - unit_name: str – the resolved quantity unit abbreviation (may be empty)
        - skip: bool – True for ingredients in the 'Gewürze' product group
        """
        positions = await self._api_get(
            "objects/recipes_pos",
            params={"query[]": f"recipe_id={recipe_id}"},
        )

        # Caches to avoid redundant API calls within the same request
        qu_cache: dict[int | str, str] = {}
        pg_cache: dict[int | str, str] = {}
        # conv_cache key: (from_qu_id, to_qu_id, product_id) → factor | None
        conv_cache: dict[tuple, float | None] = {}

        ingredients = []
        for pos in positions:
            product_name = str(pos.get("product_id", "Unknown"))
            skip = False
            qu_id_stock: int | str | None = None

            # Resolve product name, stock QU, and product group
            product_id = pos.get("product_id")
            if product_id is not None:
                try:
                    product = await self._api_get(f"objects/products/{product_id}")
                    product_name = product.get("name", str(product_id))
                    qu_id_stock = product.get("qu_id_stock")

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

            # Resolve quantity unit name (recipe display QU)
            qu_id = pos.get("qu_id")
            unit_name = ""
            if qu_id is not None:
                if qu_id not in qu_cache:
                    try:
                        qu = await self._api_get(f"objects/quantity_units/{qu_id}")
                        raw_name = qu.get("name", "")
                        qu_cache[qu_id] = _UNIT_ABBREVIATIONS.get(raw_name, raw_name)
                    except Exception:  # noqa: BLE001
                        LOGGER.debug("Could not fetch quantity unit %s", qu_id)
                        qu_cache[qu_id] = ""
                unit_name = qu_cache.get(qu_id, "")

            # Convert amount from stock QU back to recipe display QU when needed.
            # Grocy stores amount in the product's stock QU; qu_id is the display QU.
            raw_amount = pos.get("amount", "")
            display_amount: float | str = raw_amount
            if (
                qu_id is not None
                and qu_id_stock is not None
                and qu_id != qu_id_stock
                and raw_amount != ""
            ):
                factor = await self._get_qu_conversion_factor(
                    qu_id, qu_id_stock, product_id, conv_cache
                )
                if factor is None:
                    # Try reverse direction: from_qu=stock, to_qu=recipe
                    factor_rev = await self._get_qu_conversion_factor(
                        qu_id_stock, qu_id, product_id, conv_cache
                    )
                    if factor_rev is not None and factor_rev != 0:
                        try:
                            display_amount = float(raw_amount) * factor_rev
                        except (ValueError, TypeError):
                            pass
                elif factor is not None and factor != 0:
                    try:
                        display_amount = float(raw_amount) / factor
                    except (ValueError, TypeError):
                        pass

            # Format numeric amounts: drop the decimal when it is .0
            if isinstance(display_amount, float) and display_amount == int(display_amount):
                display_amount = int(display_amount)

            ingredients.append(
                {
                    "product_name": product_name,
                    "amount": display_amount,
                    "unit_name": unit_name,
                    "skip": skip,
                }
            )
        return ingredients

    async def _get_qu_conversion_factor(
        self,
        from_qu_id: int | str,
        to_qu_id: int | str,
        product_id: int | str | None,
        cache: dict[tuple, float | None],
    ) -> float | None:
        """Return the Grocy QU conversion factor for from_qu → to_qu.

        Grocy semantics: 1 unit of from_qu = factor units of to_qu.
        Product-specific conversions are tried first; generic ones are used as
        a fallback.  Returns None when no matching conversion is found.
        """
        cache_key = (from_qu_id, to_qu_id, product_id)
        if cache_key in cache:
            return cache[cache_key]

        pids = [product_id, None] if product_id is not None else [None]
        for pid in pids:
            query_filters = [f"from_qu_id={from_qu_id}", f"to_qu_id={to_qu_id}"]
            if pid is not None:
                query_filters.append(f"product_id={pid}")
            try:
                conversions = await self._api_get(
                    "objects/quantity_unit_conversions",
                    params={"query[]": query_filters},
                )
                if conversions:
                    factor = float(conversions[0].get("factor", 1))
                    cache[cache_key] = factor
                    return factor
            except Exception:  # noqa: BLE001
                LOGGER.debug(
                    "Could not fetch QU conversion %s→%s (product %s)",
                    from_qu_id,
                    to_qu_id,
                    pid,
                )

        cache[cache_key] = None
        return None

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
