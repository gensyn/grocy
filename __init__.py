"""The Grocy integration."""

from __future__ import annotations

import logging
import random
import uuid
from datetime import date, datetime, timedelta
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
            "blacklist": blacklist,
            "all_typed": typed_recipes,
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
    meal_date: date = session["meal_date"]

    # Strip [meal_type] prefix for display
    display_name = _strip_meal_prefix(recipe.get("name", "Unknown recipe"), meal_type)

    # Format date in a human-readable way (e.g. "10 March 2026")
    date_str = f"{meal_date.day} {meal_date.strftime('%B %Y')}"

    # Accept both "notify.service_name" and plain "service_name" formats
    notify_service = notify.split(".", 1)[-1] if "." in notify else notify

    try:
        await hass.services.async_call(
            "notify",
            notify_service,
            {
                "title": "Meal Suggestion",
                "message": f"{display_name} ({date_str})",
                "data": {
                    "tag": f"grocy_meal_{session_id}",
                    "actions": [
                        {
                            "action": f"GROCY_ADD_{session_id}",
                            "title": "Ok",
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
    """Accept the suggestion: add recipe to calendar and ingredients to todo list.

    After writing, advance the session to the next free calendar day so the
    user is prompted for consecutive days without having to re-invoke the service.
    """
    session = hass.data[DOMAIN]["sessions"].get(session_id)
    if not session:
        _LOGGER.warning(
            "Received 'Ok' action but session %s no longer exists", session_id
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

    # Add each ingredient to the todo list, skipping "Gewürze".
    # Item format: "{product} {amount} {unit}" (e.g. "Mehl 200 g").
    # If an item for the same product+unit already exists, its amount is summed.
    recipe_id = recipe.get("id")
    if recipe_id is not None:
        try:
            ingredients = await sensor.async_get_recipe_ingredients(recipe_id)

            # Step 1: Accumulate ingredients within this recipe by (product, unit)
            # to handle the (unlikely but possible) case of duplicate entries.
            accumulated: dict[tuple[str, str], Any] = {}
            order: list[tuple[str, str]] = []
            for ingredient in ingredients:
                if ingredient.get("skip"):
                    continue
                product_name = ingredient["product_name"]
                unit_name = ingredient.get("unit_name", "")
                amount = ingredient.get("amount", "")
                key = (product_name, unit_name)
                if key not in accumulated:
                    accumulated[key] = amount
                    order.append(key)
                else:
                    try:
                        accumulated[key] = float(accumulated[key]) + float(amount)
                    except (ValueError, TypeError):
                        pass  # Keep existing value when amounts are not numeric

            # Step 2: Fetch current todo list items so we can merge when possible.
            existing_items: list[dict] = []
            try:
                response = await hass.services.async_call(
                    "todo",
                    "get_items",
                    {CONF_ENTITY_ID: todo_list},
                    blocking=True,
                    return_response=True,
                )
                existing_items = (response or {}).get(todo_list, {}).get("items", [])
            except (ServiceValidationError, HomeAssistantError):
                _LOGGER.warning(
                    "Could not fetch existing todo items from '%s'; "
                    "all ingredients will be added as new items",
                    todo_list,
                )

            # Step 3: For each accumulated ingredient, update an existing item
            # when one with the same product+unit is already present; otherwise add.
            # Item format: "{product} {amount} {unit}" or "{product} {amount}"
            for key in order:
                product_name, unit_name = key
                amount = accumulated[key]

                # Format the amount for display.
                amount_str = ""
                if amount:
                    try:
                        amount_str = f"{float(amount):g}"
                    except (ValueError, TypeError):
                        amount_str = str(amount)

                # Build the canonical item name.
                if amount_str and unit_name.strip():
                    item_name = f"{product_name} {amount_str} {unit_name}"
                elif amount_str:
                    item_name = f"{product_name} {amount_str}"
                else:
                    item_name = product_name

                # Try to find a matching item in the existing list.
                # Format is "{product} {amount} {unit}" so we look for an item that
                # starts with "{product} ", has a parseable float as the next token,
                # and whose trailing unit matches.
                matched_summary: str | None = None
                matched_old_amount: float | None = None
                product_prefix = f"{product_name} "
                for existing in existing_items:
                    summary = existing.get("summary", "")
                    if not summary.startswith(product_prefix):
                        continue
                    rest = summary[len(product_prefix):].strip()
                    parts = rest.split(None, 1)
                    if not parts:
                        continue
                    try:
                        existing_amount = float(parts[0])
                    except ValueError:
                        continue
                    existing_unit = parts[1].strip() if len(parts) > 1 else ""
                    if existing_unit == unit_name.strip():
                        matched_old_amount = existing_amount
                        matched_summary = summary
                        break

                if matched_summary is not None and matched_old_amount is not None:
                    # Merge the amounts and update the existing item.
                    try:
                        combined = matched_old_amount + float(amount_str)
                        combined_str = (
                            str(int(combined))
                            if combined.is_integer()
                            else f"{combined:g}"
                        )
                        if unit_name.strip():
                            new_item_name = f"{product_name} {combined_str} {unit_name}"
                        else:
                            new_item_name = f"{product_name} {combined_str}"
                    except (ValueError, TypeError):
                        new_item_name = item_name  # Fallback: use new value as-is
                    await hass.services.async_call(
                        "todo",
                        "update_item",
                        {
                            CONF_ENTITY_ID: todo_list,
                            "item": matched_summary,
                            "rename": new_item_name,
                        },
                        blocking=True,
                    )
                else:
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

    # Advance to the next free calendar day and continue suggesting.
    await _advance_to_next_free_day(hass, session_id)


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


async def _find_next_free_date(
    hass: HomeAssistant,
    calendar: str,
    from_date: date,
    max_days: int = 30,
) -> date | None:
    """Return the first date after from_date with no events in the calendar.

    Looks up to max_days days ahead.  Returns None if every candidate date
    already has at least one event or the calendar query fails.
    """
    search_start = datetime.combine(from_date + timedelta(days=1), datetime.min.time())
    search_end = datetime.combine(
        from_date + timedelta(days=max_days + 1), datetime.min.time()
    )
    try:
        response = await hass.services.async_call(
            "calendar",
            "get_events",
            {
                CONF_ENTITY_ID: calendar,
                "start_date_time": search_start.isoformat(),
                "end_date_time": search_end.isoformat(),
            },
            blocking=True,
            return_response=True,
        )
    except (ServiceValidationError, HomeAssistantError):
        _LOGGER.exception(
            "Failed to query calendar '%s' for free dates", calendar
        )
        return None

    # Collect the set of dates that are already occupied.
    busy_dates: set[date] = set()
    events = (response or {}).get(calendar, {}).get("events", [])
    for event in events:
        start_raw = event.get("start", "")
        try:
            busy_dates.add(date.fromisoformat(str(start_raw)[:10]))
        except (ValueError, TypeError):
            pass

    # Walk forward to find the first free date.
    candidate = from_date + timedelta(days=1)
    for _ in range(max_days):
        if candidate not in busy_dates:
            return candidate
        candidate += timedelta(days=1)
    return None


async def _advance_to_next_free_day(hass: HomeAssistant, session_id: str) -> None:
    """Move the session to the next free calendar date and send a new suggestion.

    Dismissed recipes are reset so that all recipes in the meal type pool are
    viable again (only the blacklist filter is applied for the new day).
    If no free date is found within 30 days, the session is cleaned up.
    """
    session = hass.data[DOMAIN]["sessions"].get(session_id)
    if not session:
        return

    calendar = session["calendar"]
    meal_date: date = session["meal_date"]
    meal_type: str = session["meal_type"]
    blacklist: int = session["blacklist"]
    all_typed: list[dict] = session["all_typed"]

    next_date = await _find_next_free_date(hass, calendar, meal_date)
    if next_date is None:
        _LOGGER.info(
            "No free calendar date found within 30 days; ending session %s", session_id
        )
        _cleanup_session(hass, session_id)
        return

    # Refresh blacklist relative to today for the new day.
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

    available = [
        r for r in all_typed
        if _strip_meal_prefix(r.get("name", ""), meal_type) not in blacklisted_names
    ]

    if not available:
        _LOGGER.info(
            "All recipes blacklisted for next date; ending session %s", session_id
        )
        _cleanup_session(hass, session_id)
        return

    # Update the session for the new day: reset dismissed, update date and pool.
    session["meal_date"] = next_date
    session["dismissed"] = set()
    session["available"] = available
    session["recipe"] = random.choice(available)
    await _send_suggestion(hass, session_id)


def _cleanup_session(hass: HomeAssistant, session_id: str) -> None:
    """Remove a planning session and unsubscribe its event listener."""
    session = hass.data[DOMAIN]["sessions"].pop(session_id, None)
    if session and session.get("unsub"):
        session["unsub"]()

