"""Checks for the weather completion contract and loop stopping rules."""

import unittest

from agent_try.loop import Observation, ToolAction, run_loop
from agent_try.weather_contract import verify_weather, weather_goal


class WeatherContractTests(unittest.TestCase):
    def test_date_only_follow_up_is_a_weather_goal(self):
        history = [
            {"role": "user", "content": "潮州天气多少度？"},
            {"role": "assistant", "content": "潮州今天气温 25℃。你想查询哪天的天气？"},
        ]
        self.assertTrue(weather_goal("明天", history))

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
