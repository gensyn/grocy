"""Constants for the Grocy integration."""

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.const import CONF_NAME, CONF_ENTITY_ID

DOMAIN = "grocy"

CONF_GROCY_URL = "grocy_url"
CONF_API_KEY = "api_key"
CONF_MEAL_TYPE = "meal_type"
CONF_CALENDAR = "calendar"
CONF_TODO_LIST = "todo_list"
CONF_NOTIFY = "notify"
CONF_BLACKLIST = "blacklist"
CONF_DATE = "date"

SERVICE_PLAN_MEAL = "plan_meal"

SERVICE_PLAN_MEAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id,
        vol.Required(CONF_DATE): cv.date,
        vol.Required(CONF_MEAL_TYPE): str,
        vol.Required(CONF_CALENDAR): cv.entity_id,
        vol.Required(CONF_TODO_LIST): cv.entity_id,
        vol.Required(CONF_NOTIFY): str,
        vol.Required(CONF_BLACKLIST): vol.All(int, vol.Range(min=0)),
    }
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_GROCY_URL): str,
        vol.Required(CONF_API_KEY): str,
    }
)

STEP_OPTIONS_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GROCY_URL): str,
        vol.Required(CONF_API_KEY): str,
    }
)
