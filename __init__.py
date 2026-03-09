"""The Grocy integration."""

from __future__ import annotations

import logging
import random
import uuid
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    SERVICE_PLAN_MEAL,
    SERVICE_PLAN_MEAL_SCHEMA,
    CONF_DATE,
    CONF_MEAL_TYPE,
    CONF_CALENDAR,
    CONF_TODO_LIST,
    CONF_NOTIFY,
    CONF_BLACKLIST,
)
from .sensor import GrocySensor

_PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


def _strip_meal_prefix(name: str, meal_type: str) -> str:
    """Strip the leading '[meal_type]' prefix from a recipe name.

    Any whitespace between the prefix and the recipe name is also removed,
    so both '[Dinner] Pasta' and '[Dinner]Pasta' return 'Pasta'.
    """
    prefix = f"[{meal_type}]"
    if name.startswith(prefix):
        return name[len(prefix):].strip()
    return name


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Grocy integration and register the plan_meal service."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("sessions", {})

    async def async_plan_meal(service_call: ServiceCall) -> None:
        """Execute the grocy.plan_meal service."""
        entity_id = service_call.data[CONF_ENTITY_ID]
        meal_date = service_call.data[CONF_DATE]
        meal_type = service_call.data[CONF_MEAL_TYPE]
        calendar = service_call.data[CONF_CALENDAR]
        todo_list = service_call.data[CONF_TODO_LIST]
        notify = service_call.data[CONF_NOTIFY]
        blacklist = service_call.data[CONF_BLACKLIST]

        # Retrieve the sensor entity for this Grocy instance
        sensor = _get_sensor(hass, entity_id)

        # Fetch all recipes from Grocy
        try:
            all_recipes = await sensor.async_get_recipes()
        except HomeAssistantError as err:
            raise ServiceValidationError(
                f"Failed to load recipes from Grocy: {err}",
                translation_domain=DOMAIN,
                translation_key="grocy_api_error",
            ) from err

        # Filter recipes by meal type prefix "[meal_type]"
        prefix = f"[{meal_type}]"
        typed_recipes = [
            r for r in all_recipes if r.get("name", "").startswith(prefix)
        ]

        if not typed_recipes:
            raise ServiceValidationError(
                f"No recipes found for meal type '{meal_type}'.",
                translation_domain=DOMAIN,
                translation_key="no_recipes_found",
            )

        # Determine which recipe display names are blacklisted from the calendar.
        # Calendar entries are stored with the stripped name, so compare accordingly.
        blacklisted_names: set[str] = set()
        if blacklist > 0:
            try:
                blacklisted_names = await _get_blacklisted_recipe_names(
                    hass, calendar, blacklist
                )
            except (ServiceValidationError, HomeAssistantError) as err:
                _LOGGER.warning(
                    "Could not fetch calendar events for blacklist check: %s", err
                )

        # Filter out blacklisted recipes (compare using stripped display names)
        available = [
            r for r in typed_recipes
            if _strip_meal_prefix(r.get("name", ""), meal_type) not in blacklisted_names
        ]

        if not available:
            raise ServiceValidationError(
                f"All recipes for meal type '{meal_type}' are currently blacklisted.",
                translation_domain=DOMAIN,
                translation_key="all_recipes_blacklisted",
            )

        # Create a planning session to track state across notification responses
        session_id = uuid.uuid4().hex[:8]
        session: dict[str, Any] = {
            "sensor": sensor,
            "recipe": None,
            "meal_date": meal_date,
            "meal_type": meal_type,
            "calendar": calendar,
            "todo_list": todo_list,
            "notify": notify,
            "available": available,
            "dismissed": set(),
            "unsub": None,
        }
        hass.data[DOMAIN]["sessions"][session_id] = session

        # Pick a random recipe and send it as a suggestion
        session["recipe"] = random.choice(available)
        await _send_suggestion(hass, session_id)

        # Register event listener for Android companion app notification actions.
        # A broad Exception catch is intentional here: if _handle_add or _handle_next
        # raise an unexpected error, we want it logged with full traceback rather than
        # silently swallowed by HA's event bus error handler.
        async def _action_listener(event: Any) -> None:
            try:
                await _handle_notification_action(hass, session_id, event)
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error handling notification action for session %s",
                    session_id,
                )

        session["unsub"] = hass.bus.async_listen(
            "mobile_app_notification_action",
            _action_listener,
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAN_MEAL,
        async_plan_meal,
        schema=SERVICE_PLAN_MEAL_SCHEMA,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Grocy from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


def _get_sensor(hass: HomeAssistant, entity_id: str) -> GrocySensor:
    """Return the GrocySensor entity for the given entity_id.

    Raises ServiceValidationError if the entity cannot be found.
    """
    try:
        entity = hass.data["entity_components"][Platform.SENSOR].get_entity(
            entity_id
        )
    except (KeyError, AttributeError):
        entity = None

    if entity is None:
        raise ServiceValidationError(
            f"Could not find entity '{entity_id}'. "
            "Make sure the Grocy integration is fully loaded.",
            translation_domain=DOMAIN,
            translation_key="entity_not_found",
        )
    return entity


async def _get_blacklisted_recipe_names(
    hass: HomeAssistant, calendar: str, blacklist_days: int
) -> set[str]:
    """Return the set of event summaries from the calendar for the last blacklist_days.

    Calendar entries are stored with the stripped recipe display name (meal type
    prefix already removed), so callers should compare against stripped recipe names.
    """
    now = dt_util.now()
    start_dt = now - timedelta(days=blacklist_days)
    try:
        response = await hass.services.async_call(
            "calendar",
            "get_events",
            {
                CONF_ENTITY_ID: calendar,
                "start_date_time": start_dt.isoformat(),
                "end_date_time": now.isoformat(),
            },
            blocking=True,
            return_response=True,
        )
    except (ServiceValidationError, HomeAssistantError) as err:
        _LOGGER.warning("Failed to get calendar events for blacklist: %s", err)
        return set()

    blacklisted: set[str] = set()
    if response:
        events = response.get(calendar, {}).get("events", [])
        for event in events:
            summary = event.get("summary", "")
            if summary:
                blacklisted.add(summary)
    return blacklisted


async def _send_suggestion(hass: HomeAssistant, session_id: str) -> None:
    """Send a meal suggestion notification to the configured notify service."""
    session = hass.data[DOMAIN]["sessions"].get(session_id)
    if not session:
        return

    recipe = session["recipe"]
    meal_type = session["meal_type"]
    notify = session["notify"]

    # Strip [meal_type] prefix for display
    display_name = _strip_meal_prefix(recipe.get("name", "Unknown recipe"), meal_type)

    # Accept both "notify.service_name" and plain "service_name" formats
    notify_service = notify.split(".", 1)[-1] if "." in notify else notify

    try:
        await hass.services.async_call(
            "notify",
            notify_service,
            {
                "title": "Meal Suggestion",
                "message": display_name,
                "data": {
                    "tag": f"grocy_meal_{session_id}",
                    "actions": [
                        {
                            "action": f"GROCY_ADD_{session_id}",
                            "title": "Add",
                        },
                        {
                            "action": f"GROCY_NEXT_{session_id}",
                            "title": "Next",
                        },
                        {
                            "action": f"GROCY_CANCEL_{session_id}",
                            "title": "Cancel",
                        },
                    ],
                },
            },
            blocking=False,
        )
    except (ServiceValidationError, HomeAssistantError) as err:
        _LOGGER.error("Failed to send meal suggestion notification: %s", err)
        _cleanup_session(hass, session_id)


async def _handle_notification_action(
    hass: HomeAssistant, session_id: str, event: Any
) -> None:
    """Handle incoming notification action events for a planning session."""
    action = event.data.get("action", "")

    if action == f"GROCY_ADD_{session_id}":
        await _handle_add(hass, session_id)
    elif action == f"GROCY_NEXT_{session_id}":
        await _handle_next(hass, session_id)
    elif action == f"GROCY_CANCEL_{session_id}":
        _cleanup_session(hass, session_id)


async def _handle_add(hass: HomeAssistant, session_id: str) -> None:
    """Accept the suggestion: add recipe to calendar and ingredients to todo list."""
    session = hass.data[DOMAIN]["sessions"].get(session_id)
    if not session:
        _LOGGER.warning(
            "Received 'Add' action but session %s no longer exists", session_id
        )
        return

    recipe = session["recipe"]
    meal_type = session["meal_type"]
    calendar = session["calendar"]
    todo_list = session["todo_list"]
    meal_date = session["meal_date"]
    sensor: GrocySensor = session["sensor"]

    # Use the stripped display name (without [meal_type] prefix) for the calendar entry
    display_name = _strip_meal_prefix(recipe.get("name", ""), meal_type)

    # Add the recipe to the calendar as an all-day event.
    # end_date must be the day AFTER start_date (iCal exclusive-end convention).
    end_date = meal_date + timedelta(days=1)
    try:
        await hass.services.async_call(
            "calendar",
            "create_event",
            {
                CONF_ENTITY_ID: calendar,
                "summary": display_name,
                "start_date": str(meal_date),
                "end_date": str(end_date),
            },
            blocking=True,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Failed to add recipe '%s' to calendar '%s'", display_name, calendar
        )

    # Add each ingredient as a separate item in the todo list, skipping "Gewürze"
    recipe_id = recipe.get("id")
    if recipe_id is not None:
        try:
            ingredients = await sensor.async_get_recipe_ingredients(recipe_id)
            for ingredient in ingredients:
                if ingredient.get("skip"):
                    continue
                product_name = ingredient["product_name"]
                amount = ingredient.get("amount", "")
                unit_name = ingredient.get("unit_name", "")
                # Format amount as a clean number (strip unnecessary trailing zeros)
                if amount:
                    try:
                        amount_str = f"{float(amount):g}"
                    except (ValueError, TypeError):
                        amount_str = str(amount)
                    item_name = (
                        f"{amount_str} {unit_name} {product_name}"
                        if unit_name.strip()
                        else f"{amount_str} {product_name}"
                    )
                else:
                    item_name = product_name
                await hass.services.async_call(
                    "todo",
                    "add_item",
                    {
                        CONF_ENTITY_ID: todo_list,
                        "item": item_name,
                    },
                    blocking=True,
                )
        except (ServiceValidationError, HomeAssistantError) as err:
            _LOGGER.error("Failed to add ingredients to todo list: %s", err)
    else:
        _LOGGER.error(
            "Recipe '%s' has no id field; cannot fetch ingredients", recipe.get("name")
        )

    _cleanup_session(hass, session_id)


async def _handle_next(hass: HomeAssistant, session_id: str) -> None:
    """Skip the current suggestion and send a new one from the remaining pool."""
    session = hass.data[DOMAIN]["sessions"].get(session_id)
    if not session:
        return

    # Record the skipped recipe so it is not suggested again
    current_id = session["recipe"].get("id")
    if current_id is not None:
        session["dismissed"].add(current_id)

    remaining = [
        r for r in session["available"] if r.get("id") not in session["dismissed"]
    ]

    if not remaining:
        _LOGGER.info(
            "No more recipes available for session %s after skipping", session_id
        )
        _cleanup_session(hass, session_id)
        return

    # Pick a new recipe and resend
    session["recipe"] = random.choice(remaining)
    await _send_suggestion(hass, session_id)


def _cleanup_session(hass: HomeAssistant, session_id: str) -> None:
    """Remove a planning session and unsubscribe its event listener."""
    session = hass.data[DOMAIN]["sessions"].pop(session_id, None)
    if session and session.get("unsub"):
        session["unsub"]()

