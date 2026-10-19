"""Look up a city's daily forecast through Open-Meteo."""

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


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

    query = location.strip()
    places = await _get_json(GEOCODING_URL, {
        "name": query, "count": 5, "language": "zh", "format": "json"
    })
    if not places.get("results"):
        city, separator, qualifier = query.partition(",")
        if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", city) and not city.endswith("市"):
            retry_query = f"{city}市{separator}{qualifier}" if separator else f"{city}市"
            places = await _get_json(GEOCODING_URL, {
                "name": retry_query, "count": 5, "language": "zh", "format": "json"
            })
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

    forecast = await _get_json(FORECAST_URL, {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": place["timezone"],
        "start_date": date,
        "end_date": date,
        "temperature_unit": "celsius",
    })
    try:
        daily = forecast["daily"]
        if daily["time"] != [date]:
            raise ValueError("Forecast date mismatch")
        maximum = float(daily["temperature_2m_max"][0])
        minimum = float(daily["temperature_2m_min"][0])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise WeatherUnavailable("Forecast temperatures are unavailable for that date.") from error
    return {
        "location": ", ".join(filter(None, (place.get("name"), place.get("admin1"), place.get("country")))),
        "date": date,
        "temperature_max_c": maximum,
        "temperature_min_c": minimum,
        "source": "Open-Meteo forecast",
    }
