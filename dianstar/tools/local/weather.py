# 天气工具：解析城市与日期，通过 Open-Meteo 查询气温、天气现象、降水和风速。

import datetime as dt
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_CONDITIONS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snowfall", 73: "Moderate snowfall", 75: "Heavy snowfall",
    77: "Snow grains", 80: "Slight rain showers", 81: "Moderate rain showers",
    82: "Violent rain showers", 85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail",
    97: "Heavy thunderstorm", 99: "Thunderstorm with heavy hail",
}
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get a city's weather forecast: temperature in Celsius, daily weather condition, precipitation and wind when available. Today's current temperature, condition and wind may also be available. Daily condition means the most severe condition that day, not the dominant or all-day condition; do not say 'mainly' from this field. Precipitation probability is not a guarantee of rain or no rain. Daily high and low do not reveal when they occur; do not infer morning, evening or day-night patterns from them. If date is missing, use today. Ask for a city if missing; never guess a city. Date must be today, tomorrow, or YYYY-MM-DD within the next 7 days.",
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
class WeatherUnavailable(Exception):
    """The requested forecast cannot be returned reliably."""


class WeatherDateInvalid(WeatherUnavailable):
    """The date supplied for a weather lookup is unsupported."""


class WeatherLocationAmbiguous(WeatherUnavailable):
    """The city name matches multiple plausible places."""


class WeatherLocationNotFound(WeatherUnavailable):
    """No place matched the supplied city name."""


class WeatherLocationMismatch(WeatherUnavailable):
    """The geocoder resolved a different Chinese city."""


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


def _optional_number(values: dict, key: str) -> float | None:
    """Read an optional single-day metric without turning missing data into a failure."""
    try:
        number = float(values[key][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_condition(code: object) -> str | None:
    try:
        return WEATHER_CONDITIONS.get(int(code)) if code is not None else None
    except (TypeError, ValueError):
        return None


def _geocoding_candidates(location: str) -> list[str]:
    """Prefer a full Chinese city name before a bare name that may match towns."""
    parts = [part.strip() for part in location.strip().replace("，", ",").split(",")]
    query = ", ".join(parts)
    city = parts[0]
    suffix = f", {', '.join(parts[1:])}" if len(parts) > 1 else ""
    city_variant = None
    if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", city):
        city_variant = city[:-1] if city.endswith("市") else city + "市"
    candidates = [query]
    if city_variant:
        if not suffix and not city.endswith("市"):
            candidates.insert(0, city_variant)
        else:
            candidates.append(city_variant + suffix)
    return candidates


async def get_weather(location: str, date: str) -> dict[str, object]:
    """Return available weather data for a named city's local date."""
    if not isinstance(location, str) or not 2 <= len(location.strip()) <= 100:
        raise WeatherLocationNotFound("Provide a city name, optionally with a region or country.")
    if not isinstance(date, str) or (date not in {"today", "tomorrow"} and len(date) != 10):
        raise WeatherDateInvalid("Provide today, tomorrow, or a date in YYYY-MM-DD format.")
    if date not in {"today", "tomorrow"}:
        try:
            requested_day = dt.date.fromisoformat(date)
        except ValueError as error:
            raise WeatherDateInvalid("Provide a valid date in YYYY-MM-DD format.") from error

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
        raise WeatherLocationNotFound("City not found; use a city with a region or country.")
    if re.fullmatch(r"[\u4e00-\u9fff]+", city_name):
        matches = [item for item in results if isinstance(item, dict)
                   and isinstance(item.get("name"), str)
                   and item["name"].removesuffix("地区").removesuffix("市") == city_name]
        if not matches:
            raise WeatherLocationMismatch("Geocoder returned a different city.")
        results = matches
    place = results[0]
    if not isinstance(place, dict):
        raise WeatherUnavailable("Weather service returned an invalid location.")
    resolved_name = place.get("name")
    if not isinstance(resolved_name, str):
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
            raise WeatherLocationAmbiguous("City name is ambiguous; add a region or country.")
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
        raise WeatherDateInvalid("Date must be today or within the next 7 local days.")
    date = requested_day.isoformat()

    forecast_params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_sum,precipitation_probability_max,wind_speed_10m_max",
        "timezone": place["timezone"],
        "start_date": date,
        "end_date": date,
        "temperature_unit": "celsius",
    }
    if requested_day == today:
        forecast_params["current"] = "temperature_2m,weather_code,wind_speed_10m"
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
        "country_code": place.get("country_code"),
        "date": date,
        "temperature_max_c": maximum,
        "temperature_min_c": minimum,
        "source": "Open-Meteo forecast",
    }
    daily_condition = _optional_condition(_optional_number(daily, "weather_code"))
    if daily_condition is not None:
        result["daily_most_severe_condition"] = daily_condition
    for api_key, result_key in (
        ("precipitation_sum", "precipitation_sum_mm"),
        ("precipitation_probability_max", "precipitation_probability_max_pct"),
        ("wind_speed_10m_max", "wind_speed_max_kmh"),
    ):
        value = _optional_number(daily, api_key)
        if value is not None:
            result[result_key] = value
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
        current_condition = _optional_condition(current.get("weather_code"))
        if current_condition is not None:
            result["current_condition"] = current_condition
        try:
            current_wind = float(current["wind_speed_10m"])
        except (KeyError, TypeError, ValueError):
            current_wind = None
        if current_wind is not None and math.isfinite(current_wind):
            result["wind_speed_current_kmh"] = current_wind
    return result
