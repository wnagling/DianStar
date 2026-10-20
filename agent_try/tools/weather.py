"""Look up a city's daily forecast through Open-Meteo."""

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get a city's temperature in Celsius. For today, returns current temperature and the day's high and low; future dates return the high and low. If date is missing, use today. Ask for a city if missing; never guess a city. Date must be today, tomorrow, or YYYY-MM-DD within the next 7 days.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City, optionally followed by a region or country"},
                "date": {"type": "string", "description": "today, tomorrow, or YYYY-MM-DD"},
            },
            "required": ["location", "date"],
            "additionalProperties": False,
        },
    },
}
ADMIN_ALIASES = {
    "新疆维吾尔自治区": "新疆",
    "内蒙古自治区": "内蒙古",
    "广西壮族自治区": "广西",
    "宁夏回族自治区": "宁夏",
    "西藏自治区": "西藏",
}


class WeatherUnavailable(Exception):
    """The requested forecast cannot be returned reliably."""


async def _get_json(url: str, params: dict[str, object]) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise WeatherUnavailable("Weather service is unavailable; try again later.") from error
    if not isinstance(data, dict) or data.get("error"):
        raise WeatherUnavailable("Weather service returned an error.")
    return data


def _geocoding_candidates(location: str) -> list[str]:
    """Try common Chinese city and province forms without dropping the region."""
    query = location.strip().replace("，", ",")
    city, comma, rest = query.partition(",")
    city = city.strip()
    suffix = f",{rest}" if comma else ""
    first_qualifier, next_comma, remaining = rest.partition(",")
    qualifier = first_qualifier.strip()
    shorter = ADMIN_ALIASES.get(qualifier, qualifier)
    if shorter == qualifier and qualifier.endswith(("省", "市")):
        shorter = qualifier[:-1]
    normalized_suffix = f", {shorter}" if comma else ""
    if next_comma:
        normalized_suffix += f", {remaining.strip()}"
    city_variant = None
    if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", city):
        city_variant = city[:-1] if city.endswith("市") else city + "市"
    candidates = [query]
    if city_variant:
        candidates.append(city_variant + normalized_suffix)
        candidates.append(city_variant + suffix)
    candidates.append(city + normalized_suffix)
    return list(dict.fromkeys(candidates))


async def get_weather(location: str, date: str) -> dict[str, object]:
    """Return the local day's forecast high and low in Celsius for a named city."""
    if not isinstance(location, str) or not 2 <= len(location.strip()) <= 100:
        raise WeatherUnavailable("Provide a city name, optionally with a region or country.")
    if not isinstance(date, str) or (date not in {"today", "tomorrow"} and len(date) != 10):
        raise WeatherUnavailable("Provide today, tomorrow, or a date in YYYY-MM-DD format.")
    if date not in {"today", "tomorrow"}:
        try:
            requested_day = dt.date.fromisoformat(date)
        except ValueError as error:
            raise WeatherUnavailable("Provide a valid date in YYYY-MM-DD format.") from error

    places = {}
    candidates = _geocoding_candidates(location)
    city_name = location.strip().replace("，", ",").partition(",")[0].strip().removesuffix("市")
    for query in candidates:
        places = await _get_json(GEOCODING_URL, {
            "name": query, "count": 5, "language": "zh", "format": "json"
        })
        if query != candidates[0] and places.get("results"):
            # Prefix search can match nearby names; a fallback must match the city itself.
            exact = [item for item in places["results"] if isinstance(item, dict)
                     and str(item.get("name", "")).removesuffix("市") == city_name]
            places = {"results": exact}
        if places.get("results"):
            break
    results = places.get("results") or []
    if not isinstance(results, list) or not results:
        raise WeatherUnavailable("City not found; use a city with a region or country.")
    place = results[0]
    if not isinstance(place, dict):
        raise WeatherUnavailable("Weather service returned an invalid location.")
    same_named = [place] + [
        other for other in results[1:]
        if isinstance(other, dict) and other.get("name") == place.get("name")
    ]
    if any(
        (other.get("admin1"), other.get("country")) != (place.get("admin1"), place.get("country"))
        for other in same_named[1:]
    ):
        by_population = sorted(same_named, key=lambda item: item.get("population") or 0, reverse=True)
        largest = by_population[0].get("population") or 0
        second = by_population[1].get("population") or 0
        if largest < 100_000 or largest < 10 * second:
            raise WeatherUnavailable("City name is ambiguous; add a region or country.")
        place = by_population[0]
    try:
        today = dt.datetime.now(ZoneInfo(place["timezone"])).date()
        latitude = float(place["latitude"])
        longitude = float(place["longitude"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise WeatherUnavailable("Weather service returned an invalid location.") from error
    if date in {"today", "tomorrow"}:
        requested_day = today + dt.timedelta(days=int(date == "tomorrow"))
    if not today <= requested_day <= today + dt.timedelta(days=7):
        raise WeatherUnavailable("Date must be today or within the next 7 local days.")
    date = requested_day.isoformat()

    forecast_params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": place["timezone"],
        "start_date": date,
        "end_date": date,
        "temperature_unit": "celsius",
    }
    if requested_day == today:
        forecast_params["current"] = "temperature_2m"
    forecast = await _get_json(FORECAST_URL, forecast_params)
    try:
        daily = forecast["daily"]
        if daily["time"] != [date]:
            raise ValueError("Forecast date mismatch")
        maximum = float(daily["temperature_2m_max"][0])
        minimum = float(daily["temperature_2m_min"][0])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise WeatherUnavailable("Forecast temperatures are unavailable for that date.") from error
    result = {
        "location": ", ".join(filter(None, (place.get("name"), place.get("admin1"), place.get("country")))),
        "date": date,
        "temperature_max_c": maximum,
        "temperature_min_c": minimum,
        "source": "Open-Meteo forecast",
    }
    if requested_day == today:
        current = forecast.get("current") or {}
        try:
            current_time = current["time"]
            if not isinstance(current_time, str) or current_time[:10] != date:
                raise ValueError("Current time does not match local date")
            result["temperature_current_c"] = float(current["temperature_2m"])
            result["current_time"] = current_time
        except (KeyError, TypeError, ValueError):
            result["temperature_current_c"] = None
            result["current_time"] = None
    return result
