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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Grocy integration and register the plan_meal service."""
    hass.data.setdefault(DOMAIN, {"sessions": {}})

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

        # Determine which recipe names are blacklisted from the calendar
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

        # Filter out blacklisted recipes
        available = [
            r for r in typed_recipes if r.get("name") not in blacklisted_names
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

        # Register event listener for Android companion app notification actions
        async def _action_listener(event: Any) -> None:
            await _handle_notification_action(hass, session_id, event)

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
    """Return a set of recipe names that appear in the calendar within the last blacklist_days."""
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
    notify = session["notify"]

    # Accept both "notify.service_name" and plain "service_name" formats
    notify_service = notify.split(".", 1)[-1] if "." in notify else notify

    try:
        await hass.services.async_call(
            "notify",
            notify_service,
            {
                "title": "Meal Suggestion",
                "message": recipe.get("name", "Unknown recipe"),
                "data": {
                    "tag": f"grocy_meal_{session_id}",
                    "actions": [
                        {
                            "action": f"GROCY_ADD_{session_id}",
                            "title": "Add",
                        },
                        {
                            "action": f"GROCY_DISMISS_{session_id}",
                            "title": "Dismiss",
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
    elif action == f"GROCY_DISMISS_{session_id}":
        await _handle_dismiss(hass, session_id)
    elif action == f"GROCY_CANCEL_{session_id}":
        _cleanup_session(hass, session_id)


async def _handle_add(hass: HomeAssistant, session_id: str) -> None:
    """Accept the suggestion: add recipe to calendar and ingredients to todo list."""
    session = hass.data[DOMAIN]["sessions"].get(session_id)
    if not session:
        return

    recipe = session["recipe"]
    calendar = session["calendar"]
    todo_list = session["todo_list"]
    meal_date = session["meal_date"]
    sensor: GrocySensor = session["sensor"]

    # Add the recipe to the calendar as an all-day event
    try:
        await hass.services.async_call(
            "calendar",
            "create_event",
            {
                CONF_ENTITY_ID: calendar,
                "summary": recipe.get("name", ""),
                "start_date": str(meal_date),
                "end_date": str(meal_date),
            },
            blocking=True,
        )
    except (ServiceValidationError, HomeAssistantError) as err:
        _LOGGER.error("Failed to add recipe to calendar: %s", err)

    # Add each ingredient as a separate item in the todo list
    try:
        ingredients = await sensor.async_get_recipe_ingredients(recipe["id"])
        for ingredient in ingredients:
            product_name = ingredient["product_name"]
            amount = ingredient.get("amount", "")
            item_name = f"{amount}x {product_name}" if amount else product_name
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

    _cleanup_session(hass, session_id)


async def _handle_dismiss(hass: HomeAssistant, session_id: str) -> None:
    """Dismiss the current suggestion and send a new one from the remaining pool."""
    session = hass.data[DOMAIN]["sessions"].get(session_id)
    if not session:
        return

    # Record the dismissed recipe so it is not suggested again
    session["dismissed"].add(session["recipe"]["id"])

    remaining = [
        r for r in session["available"] if r["id"] not in session["dismissed"]
    ]

    if not remaining:
        _LOGGER.info(
            "No more recipes available for session %s after dismissal", session_id
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
