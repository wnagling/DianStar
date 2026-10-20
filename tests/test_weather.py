# 测试天气工具的城市查找、当地日期判断和预报气温提取。

import datetime as dt
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from agent_try.tools.weather import FORECAST_URL, GEOCODING_URL, WeatherUnavailable, get_weather


PLACE = {
    "name": "北京", "admin1": "北京", "country": "中国",
    "country_code": "CN",
    "latitude": 39.9, "longitude": 116.4, "timezone": "Asia/Shanghai",
    "population": 18_000_000,
}


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    @patch("agent_try.tools.weather._get_json", new_callable=AsyncMock)
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

    @patch("agent_try.tools.weather._get_json", new_callable=AsyncMock)
    async def test_city_fallback_does_not_choose_a_prefix_match(self, get_json):
        get_json.side_effect = [
            {},
            {"results": [{**PLACE, "name": "乌鲁木齐南", "admin1": "新疆"}]},
        ]
        with self.assertRaisesRegex(WeatherUnavailable, "City not found"):
            await get_weather("乌鲁木齐市", "today")
        self.assertEqual(get_json.await_count, 2)

    @patch("agent_try.tools.weather._get_json", new_callable=AsyncMock)
    async def test_autonomous_region_qualifier_is_shortened_for_lookup(self, get_json):
        today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        get_json.side_effect = [
            {},
            {"results": [{**PLACE, "name": "乌鲁木齐", "admin1": "新疆"}]},
            {"daily": {"time": [today], "temperature_2m_max": [16], "temperature_2m_min": [5]}},
        ]
        result = await get_weather("乌鲁木齐市，新疆维吾尔自治区，中国", "today")
        self.assertIn("乌鲁木齐", result["location"])
        self.assertEqual(get_json.await_args_list[1].args[1]["name"], "乌鲁木齐, 新疆, 中国")

    @patch("agent_try.tools.weather._get_json", new_callable=AsyncMock)
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

    @patch("agent_try.tools.weather._get_json", new_callable=AsyncMock)
    async def test_today_uses_place_timezone_and_extracts_daily_high_low(self, get_json):
        today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        get_json.side_effect = [
            {"results": [PLACE]},
            {
                "daily": {"time": [today], "temperature_2m_max": [21.4], "temperature_2m_min": [9.2]},
                "current": {"time": f"{today}T14:15", "temperature_2m": 18.3},
            },
        ]
        result = await get_weather("北京", "today")
        self.assertEqual(result["date"], today)
        self.assertEqual(result["country_code"], "CN")
        self.assertEqual(result["temperature_max_c"], 21.4)
        self.assertEqual(result["temperature_min_c"], 9.2)
        self.assertEqual(result["temperature_current_c"], 18.3)
        self.assertEqual(result["current_time"], f"{today}T14:15")
        self.assertEqual(get_json.await_args_list[0].args[0], GEOCODING_URL)
        self.assertEqual(get_json.await_args_list[1].args[0], FORECAST_URL)
        self.assertEqual(get_json.await_args_list[1].args[1]["timezone"], "Asia/Shanghai")
        self.assertEqual(get_json.await_args_list[1].args[1]["current"], "temperature_2m")

    @patch("agent_try.tools.weather._get_json", new_callable=AsyncMock)
    async def test_future_date_has_no_current_temperature(self, get_json):
        tomorrow = (dt.datetime.now(ZoneInfo("Asia/Shanghai")).date() + dt.timedelta(days=1)).isoformat()
        get_json.side_effect = [
            {"results": [PLACE]},
            {"daily": {"time": [tomorrow], "temperature_2m_max": [22], "temperature_2m_min": [10]}},
        ]
        result = await get_weather("北京", "tomorrow")
        self.assertNotIn("temperature_current_c", result)
        self.assertNotIn("current", get_json.await_args_list[1].args[1])

    @patch("agent_try.tools.weather._get_json", new_callable=AsyncMock)
    async def test_date_outside_window_stops_before_forecast(self, get_json):
        get_json.return_value = {"results": [PLACE]}
        future = (dt.datetime.now(ZoneInfo("Asia/Shanghai")).date() + dt.timedelta(days=8)).isoformat()
        with self.assertRaisesRegex(WeatherUnavailable, "next 7"):
            await get_weather("北京", future)
        get_json.assert_awaited_once()

    @patch("agent_try.tools.weather._get_json", new_callable=AsyncMock)
    async def test_ambiguous_city_requests_qualification(self, get_json):
        get_json.return_value = {"results": [PLACE, {**PLACE, "country": "Other", "population": 17_000_000}]}
        with self.assertRaisesRegex(WeatherUnavailable, "ambiguous"):
            await get_weather("北京", "today")
        get_json.assert_awaited_once()

    @patch("agent_try.tools.weather._get_json", new_callable=AsyncMock)
    async def test_major_city_wins_over_small_same_named_places(self, get_json):
        today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        get_json.side_effect = [
            {"results": [PLACE, {**PLACE, "admin1": "Other", "population": None}]},
            {"daily": {"time": [today], "temperature_2m_max": [21], "temperature_2m_min": [9]}},
        ]
        result = await get_weather("北京", "today")
        self.assertIn("中国", result["location"])
