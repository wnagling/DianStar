# 测试通用任务循环的停止规则。

import unittest

from dianstar.loop import Observation, ToolAction, run_loop

class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_final_answer_returns_without_answer_verifier(self):
        async def think(_observations):
            return "模型直接回答"

        async def act(_action):
            self.fail("No tool was requested")

        self.assertEqual(await run_loop(think, act), "模型直接回答")

    async def test_repeated_tool_call_is_stopped(self):
        async def think(_observations):
            return ToolAction("get_weather", {"location": "潮州", "date": "today"})

        async def act(action):
            return Observation(action, {"error": "location_not_found"})

        with self.assertRaisesRegex(ValueError, "repeated the same tool call"):
            await run_loop(think, act, max_steps=2)
