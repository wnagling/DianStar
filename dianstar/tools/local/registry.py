"""Bind built-in schemas, handlers and required capabilities."""
import json

from dianstar.core.types import Observation, ToolAction
from dianstar.core.registry import ToolRegistration
from dianstar.tools.local.web import FETCH_TOOL, SEARCH_TOOL
from dianstar.tools.local.web_access import WEB_ACCESS_TOOL, WEB_ACCESS_STATUS_TOOL, manage_web_access
from dianstar.tools.local.date_calculator import DATE_TOOL, DateCalculationError, calculate_date
from dianstar.tools.local.number_calculator import NUMBER_TOOL, NumberCalculationError, calculate_number
from dianstar.tools.local.weather import WEATHER_TOOL, WeatherDateInvalid, WeatherLocationAmbiguous, WeatherLocationMismatch, WeatherLocationNotFound, WeatherUnavailable, get_weather
from dianstar.tools.local.weather_contract import weather_data


def update_web_tools(registry, handler, settings):
    for schema in (SEARCH_TOOL, FETCH_TOOL):
        name = schema["function"]["name"]
        if settings.provider == "disabled":
            registry.pop(name, None)
        else:
            registry[name] = ToolRegistration(schema,
                "web.search" if name == "search_web" else "web.read", handler)


def build_local_registry(web, evidence, env_file, search_settings):
    def observe_error(action: ToolAction, code: str) -> Observation:
        result = {"error": code, "requested_location": action.arguments["location"],
                  "requested_date": action.arguments["date"]}
        return Observation(action, result)

    async def act_weather(action: ToolAction) -> Observation:
        try:
            result = await get_weather(action.arguments["location"], action.arguments["date"])
        except WeatherLocationAmbiguous:
            return observe_error(action, "location_ambiguous")
        except WeatherDateInvalid:
            return observe_error(action, "invalid_date")
        except WeatherLocationMismatch:
            return observe_error(action, "location_mismatch")
        except WeatherLocationNotFound:
            return observe_error(action, "location_not_found")
        except WeatherUnavailable:
            return observe_error(action, "weather_unavailable")
        return Observation(action, json.loads(weather_data(result)))

    async def act_date(action: ToolAction) -> Observation:
        try:
            result = calculate_date(**action.arguments)
        except DateCalculationError as error:
            result = {"error": error.code, "message": str(error), "requested": action.arguments}
        return Observation(action, result)

    async def act_number(action: ToolAction) -> Observation:
        try:
            result = calculate_number(**action.arguments)
        except NumberCalculationError as error:
            result = {"error": error.code, "message": str(error), "requested": action.arguments}
        return Observation(action, result)

    async def act_web(action: ToolAction) -> Observation:
        result = await web.execute(action.name, action.arguments)
        result = evidence.observe(action.name, action.arguments, result)
        return Observation(action, result)

    async def act_web_access(action: ToolAction) -> Observation:
        # Defense in depth: this handler cannot forward a mutation argument.
        result = await manage_web_access("status", env_file=env_file)
        return Observation(action, result)

    # 同一份注册表绑定模型可见的工具定义和实际执行函数。
    tool_registry = {
        WEATHER_TOOL["function"]["name"]: ToolRegistration(WEATHER_TOOL, "weather.read", act_weather),
        DATE_TOOL["function"]["name"]: ToolRegistration(DATE_TOOL, "calculate", act_date),
        NUMBER_TOOL["function"]["name"]: ToolRegistration(NUMBER_TOOL, "calculate", act_number),
        WEB_ACCESS_TOOL["function"]["name"]: ToolRegistration(WEB_ACCESS_STATUS_TOOL, "permissions.read", act_web_access),
    }
    update_web_tools(tool_registry, act_web, search_settings)
    return tool_registry, act_web
