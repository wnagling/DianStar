# 测试天气工具的城市查找、当地日期判断和预报气温提取。

import datetime as dt
import json
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from dianstar.tools.weather import (
    FORECAST_URL, GEOCODING_URL, WeatherDateInvalid, WeatherLocationAmbiguous,
    WeatherLocationMismatch,
    WeatherLocationNotFound, WeatherUnavailable, get_weather,
)
from dianstar.tools.weather_contract import weather_data


PLACE = {
    "name": "北京", "admin1": "北京", "country": "中国",
    "country_code": "CN",
    "latitude": 39.9, "longitude": 116.4, "timezone": "Asia/Shanghai",
    "population": 18_000_000,
}


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_bare_luoyang_prefers_henan_city_over_same_named_towns(self, get_json):
        tomorrow = (dt.datetime.now(ZoneInfo("Asia/Shanghai")).date() + dt.timedelta(days=1)).isoformat()
        get_json.side_effect = [
            {"results": [{**PLACE, "name": "洛阳市", "admin1": "河南"}]},
            {"daily": {"time": [tomorrow], "temperature_2m_max": [20],
                       "temperature_2m_min": [10]}},
        ]
        result = await get_weather("洛阳", "tomorrow")
        self.assertEqual(get_json.await_args_list[0].args[1]["name"], "洛阳市")
        self.assertEqual(result["location"], "洛阳市, 河南, 中国")

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_city_suffix_is_removed_when_service_indexes_short_name(self, get_json):
        today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        get_json.side_effect = [
            {},
            {"results": [{**PLACE, "name": "乌鲁木齐", "admin1": "新疆"}]},
            {"daily": {"time": [today], "temperature_2m_max": [16], "temperature_2m_min": [5]}},
        ]
        result = await get_weather("乌鲁木齐市", "today")
        self.assertIn("乌鲁木齐", result["location"])
        self.assertEqual(get_json.await_args_list[1].args[1]["name"], "乌鲁木齐")

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_city_fallback_does_not_choose_a_prefix_match(self, get_json):
        get_json.side_effect = [
            {},
            {"results": [{**PLACE, "name": "乌鲁木齐南", "admin1": "新疆"}]},
        ]
        with self.assertRaisesRegex(WeatherLocationNotFound, "City not found"):
            await get_weather("乌鲁木齐市", "today")
        self.assertEqual(get_json.await_count, 2)

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_geocoder_must_return_requested_chinese_city(self, get_json):
        get_json.return_value = {"results": [{**PLACE, "name": "洛阳市", "admin1": "河南"}]}
        with self.assertRaises(WeatherLocationMismatch):
            await get_weather("北京", "today")
        get_json.assert_awaited_once()

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_exact_chinese_city_can_follow_wrong_geocoder_candidate(self, get_json):
        tomorrow = (dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
                    + dt.timedelta(days=1)).isoformat()
        get_json.side_effect = [
            {"results": [{**PLACE, "name": "洛阳市", "admin1": "河南"}, PLACE]},
            {"daily": {"time": [tomorrow], "temperature_2m_max": [20],
                       "temperature_2m_min": [10]}},
        ]
        result = await get_weather("北京", "tomorrow")
        self.assertEqual(result["location"], "北京, 北京, 中国")

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_tool_does_not_invent_a_shorter_region_name(self, get_json):
        get_json.side_effect = [{}, {}]
        with self.assertRaises(WeatherLocationNotFound):
            await get_weather("乌鲁木齐市，新疆维吾尔自治区，中国", "today")
        self.assertEqual(get_json.await_args_list[0].args[1]["name"], "乌鲁木齐市, 新疆维吾尔自治区, 中国")
        self.assertEqual(get_json.await_args_list[1].args[1]["name"], "乌鲁木齐, 新疆维吾尔自治区, 中国")

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_chinese_city_suffix_fallback(self, get_json):
        today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        get_json.side_effect = [
            {},
            {"results": [{**PLACE, "name": "潮州市", "admin1": "广东"}]},
            {"daily": {"time": [today], "temperature_2m_max": [27], "temperature_2m_min": [20]}},
        ]
        result = await get_weather("潮州, 广东, 中国", "today")
        self.assertIn("潮州市", result["location"])
        self.assertEqual(get_json.await_args_list[1].args[1]["name"], "潮州市, 广东, 中国")

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_today_uses_place_timezone_and_extracts_daily_high_low(self, get_json):
        today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        get_json.side_effect = [
            {"results": [PLACE]},
            {
                "daily": {"time": [today], "temperature_2m_max": [21.4], "temperature_2m_min": [9.2],
                          "weather_code": [61], "precipitation_sum": [2.3],
                          "precipitation_probability_max": [70], "wind_speed_10m_max": [18.0]},
                "current": {"time": f"{today}T14:15", "temperature_2m": 18.3,
                            "weather_code": 3, "wind_speed_10m": 8.5},
            },
        ]
        result = await get_weather("北京", "today")
        self.assertEqual(result["date"], today)
        self.assertEqual(result["country_code"], "CN")
        self.assertEqual(result["temperature_max_c"], 21.4)
        self.assertEqual(result["temperature_min_c"], 9.2)
        self.assertEqual(result["temperature_current_c"], 18.3)
        self.assertEqual(result["current_time"], f"{today}T14:15")
        self.assertEqual(result["daily_most_severe_condition"], "Slight rain")
        self.assertEqual(result["current_condition"], "Overcast")
        self.assertEqual(result["precipitation_sum_mm"], 2.3)
        self.assertEqual(result["precipitation_probability_max_pct"], 70)
        self.assertEqual(result["wind_speed_max_kmh"], 18.0)
        self.assertEqual(result["wind_speed_current_kmh"], 8.5)
        visible = json.loads(weather_data(result))
        self.assertEqual(visible["daily_most_severe_condition"], "Slight rain")
        self.assertEqual(visible["wind_speed_max_kmh"], 18.0)
        self.assertNotIn("source", visible)
        self.assertEqual(get_json.await_args_list[0].args[0], GEOCODING_URL)
        self.assertEqual(get_json.await_args_list[1].args[0], FORECAST_URL)
        self.assertEqual(get_json.await_args_list[1].args[1]["timezone"], "Asia/Shanghai")
        self.assertEqual(get_json.await_args_list[1].args[1]["current"], "temperature_2m,weather_code,wind_speed_10m")

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_future_date_has_no_current_temperature(self, get_json):
        tomorrow = (dt.datetime.now(ZoneInfo("Asia/Shanghai")).date() + dt.timedelta(days=1)).isoformat()
        get_json.side_effect = [
            {"results": [PLACE]},
            {"daily": {"time": [tomorrow], "temperature_2m_max": [22], "temperature_2m_min": [10],
                       "weather_code": [2], "wind_speed_10m_max": [12]}},
        ]
        result = await get_weather("北京", "tomorrow")
        self.assertNotIn("temperature_current_c", result)
        self.assertEqual(result["daily_most_severe_condition"], "Partly cloudy")
        self.assertEqual(result["wind_speed_max_kmh"], 12.0)
        self.assertNotIn("current", get_json.await_args_list[1].args[1])

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_date_outside_window_stops_before_forecast(self, get_json):
        get_json.return_value = {"results": [PLACE]}
        future = (dt.datetime.now(ZoneInfo("Asia/Shanghai")).date() + dt.timedelta(days=8)).isoformat()
        with self.assertRaisesRegex(WeatherDateInvalid, "next 7"):
            await get_weather("北京", future)
        get_json.assert_awaited_once()

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_ambiguous_city_requests_qualification(self, get_json):
        get_json.return_value = {"results": [PLACE, {**PLACE, "country": "Other", "population": 17_000_000}]}
        with self.assertRaisesRegex(WeatherLocationAmbiguous, "ambiguous"):
            await get_weather("北京", "today")
        get_json.assert_awaited_once()

    @patch("dianstar.tools.weather._get_json", new_callable=AsyncMock)
    async def test_major_city_wins_over_small_same_named_places(self, get_json):
        today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        get_json.side_effect = [
            {"results": [PLACE, {**PLACE, "admin1": "Other", "population": None}]},
            {"daily": {"time": [today], "temperature_2m_max": [21], "temperature_2m_min": [9]}},
        ]
        result = await get_weather("北京", "today")
        self.assertIn("中国", result["location"])
