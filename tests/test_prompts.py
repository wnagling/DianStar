"""检查每轮日期注入、工具循环日期一致性及跨天连续对话。"""

from datetime import date, datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.runtime import run_turn
from dianstar.prompts.builder import ASSISTANT_INSTRUCTIONS, build_system_prompt


class PromptTests(unittest.TestCase):
    def test_explicit_day_keeps_static_rules_and_day_precision(self):
        prompt = build_system_prompt(date(2028, 2, 29))
        self.assertTrue(prompt.startswith(ASSISTANT_INSTRUCTIONS + "\n\n"))
        self.assertIn("当前本地日期：2028-02-29。", prompt)
        self.assertEqual(prompt.count("当前本地日期："), 1)
        context = prompt[len(ASSISTANT_INSTRUCTIONS):]
        self.assertNotRegex(context, r"\d{2}:\d{2}")
        self.assertNotIn("2028-02-29", ASSISTANT_INSTRUCTIONS)


class PromptRequestTests(unittest.IsolatedAsyncioTestCase):
    @patch("dianstar.prompts.builder.datetime")
    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_next_question_refreshes_day_but_tool_loop_keeps_its_snapshot(
        self, client_class, weather, clock,
    ):
        clock.now.side_effect = [datetime(2030, 12, 31, 23, 59), datetime(2031, 1, 1, 0, 1)]
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"北京","date":"today"}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
                message=SimpleNamespace(content=None, tool_calls=[call]))]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content="北京今天 1～5℃。"))]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content="新的一天是 2031-01-01。"))]),
        ])
        weather.return_value = {"location": "北京", "date": "2030-12-31",
                                "temperature_max_c": 5, "temperature_min_c": 1}
        history = []
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=fake-test-key\n", encoding="utf-8")
            await run_turn("北京今天天气？", env_file=env_file, history=history)
            first_turn_history = [message.copy() for message in history]
            await run_turn("今天几号？", env_file=env_file, history=history)

        requests = client.chat.completions.create.await_args_list
        self.assertEqual(len(requests), 3)
        prompts = [request.kwargs["messages"][0] for request in requests]
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(prompts[0], {"role": "system",
            "content": build_system_prompt(date(2030, 12, 31))})
        self.assertEqual(prompts[2], {"role": "system",
            "content": build_system_prompt(date(2031, 1, 1))})
        self.assertEqual(clock.now.call_count, 2)
        weather.assert_awaited_once_with("北京", "today")
        self.assertFalse(any(message["role"] == "system" for message in history))
        self.assertEqual(requests[2].kwargs["messages"][1:-1], first_turn_history)
