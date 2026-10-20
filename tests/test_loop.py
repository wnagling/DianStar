# 测试天气回答的完成条件，以及任务循环的停止规则。

import unittest

from agent_try.loop import Observation, ToolAction, run_loop
from agent_try.tools.weather_contract import (
    same_weather_city, verify_weather, weather_date_unspecified, weather_goal,
)


class WeatherContractTests(unittest.TestCase):
    def test_revised_weather_lookup_keeps_city_and_explicit_country(self):
        original = "乌鲁木齐市，新疆维吾尔自治区，中国"
        self.assertTrue(same_weather_city(original, "乌鲁木齐, 新疆, 中国"))
        self.assertFalse(same_weather_city(original, "北京, 新疆, 中国"))
        self.assertFalse(same_weather_city(original, "乌鲁木齐, 新疆, 美国"))

    def test_date_only_follow_up_is_a_weather_goal(self):
        history = [
            {"role": "user", "content": "潮州天气多少度？"},
            {"role": "assistant", "content": "潮州今天气温 25℃。你想查询哪天的天气？"},
        ]
        self.assertTrue(weather_goal("明天", history))

    def test_country_reply_after_clarification_keeps_weather_goal_and_date(self):
        history = [
            {"role": "user", "content": "La Paz 今天多少度？"},
            {"role": "assistant", "content": "“La Paz”可能对应多个地点。你要查哪个国家或地区的“La Paz”？"},
        ]
        self.assertTrue(weather_goal("玻利维亚", history))
        self.assertFalse(weather_date_unspecified("玻利维亚", history))

    def test_wrong_date_does_not_complete_even_with_matching_temperatures(self):
        observation = Observation(
            ToolAction("get_weather", {"location": "潮州", "date": "today"}),
            {"location": "潮州", "date": "2026-10-20",
             "temperature_current_c": 25.0,
             "temperature_max_c": 30.0, "temperature_min_c": 20.0},
        )
        verdict = verify_weather(
            "潮州明天当前 25℃，最高 30℃，最低 20℃。", [observation],
            question="潮州明天气温？", date_unspecified=False,
        )
        self.assertEqual(verdict.status, "retry")
        self.assertIn("重新查询", verdict.feedback)
        self.assertTrue(verdict.retry_tool)

    def test_swapped_high_and_low_are_rejected(self):
        observation = Observation(
            ToolAction("get_weather", {"location": "潮州", "date": "tomorrow"}),
            {"location": "潮州", "date": "2026-10-21",
             "temperature_max_c": 30.0, "temperature_min_c": 20.0},
        )
        verdict = verify_weather(
            "潮州明天最高 20℃，最低 30℃。", [observation],
            question="潮州明天气温？", date_unspecified=False,
        )
        self.assertEqual(verdict.status, "retry")

    def test_english_weather_answer_must_stay_in_english(self):
        observation = Observation(
            ToolAction("get_weather", {"location": "Beijing", "date": "today"}),
            {"location": "北京, 中国", "date": "2026-10-20",
             "temperature_current_c": 8.8,
             "temperature_max_c": 13.2, "temperature_min_c": 7.5},
        )
        question = "what's the temeprature of beijing"
        self.assertTrue(weather_goal(question, []))
        chinese = verify_weather(
            "北京今天最高气温 13.2°C，最低气温 7.5°C。当前气温 8.8°C。",
            [observation], question=question, date_unspecified=True,
        )
        self.assertEqual(chinese.status, "retry")
        english = verify_weather(
            "Beijing today: current temperature 8.8°C, high 13.2°C, low 7.5°C.",
            [observation], question=question, date_unspecified=True,
        )
        self.assertEqual(english.status, "completed")
        self.assertIn("Which date", english.answer)


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_final_answer_hits_bound_without_completion(self):
        async def think(_observations, _feedback):
            return "未经查询的天气回答"

        async def act(_action):
            self.fail("No tool was requested")

        def verify(_answer, _observations):
            from agent_try.loop import Verdict
            return Verdict("retry", "先查天气")

        with self.assertRaisesRegex(ValueError, "loop limit"):
            await run_loop(think, act, verify, max_steps=2)
