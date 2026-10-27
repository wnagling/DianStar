"""Exercise the ReAct limit through the application and provider boundary."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.runtime import run_turn


class RuntimeLoopLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_uses_sixteen_decisions_independently_of_tool_count(self):
        for tool_steps, batch_size, completes in ((15, 1, True), (15, 2, True), (16, 1, False)):
            with self.subTest(tool_steps=tool_steps, batch_size=batch_size), \
                    tempfile.TemporaryDirectory() as temporary, \
                    patch("dianstar.providers.deepseek.AsyncOpenAI") as client_class:
                responses = []
                for step in range(tool_steps):
                    calls = [SimpleNamespace(id=f"call-{step}-{index}", function=SimpleNamespace(
                        name="calculate_number", arguments='{"expression":"0.1+0.2"}'))
                        for index in range(batch_size)]
                    responses.append(SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
                        message=SimpleNamespace(content=None, tool_calls=calls))]))
                responses.append(SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                    message=SimpleNamespace(content="复核完成，结果为 0.3。"))]))
                create = client_class.return_value.chat.completions.create = AsyncMock(side_effect=responses)
                env_file = Path(temporary) / ".env"
                env_file.write_text("DEEPSEEK_API_KEY=fake-test-key\n", encoding="utf-8")
                history = []

                if completes:
                    answer = await run_turn("重复计算并复核", env_file=env_file, history=history)
                    results = [message for message in history if message["role"] == "tool"]
                    self.assertEqual(len(results), tool_steps * batch_size)
                    self.assertTrue(all(json.loads(message["content"])["result"] == "0.3"
                                        for message in results))
                    self.assertEqual(history[-1]["content"], answer)
                else:
                    with self.assertRaisesRegex(ValueError, "loop limit"):
                        await run_turn("重复计算并复核", env_file=env_file, history=history)
                    self.assertEqual(history, [])

                self.assertEqual(create.await_count, 16)
                final_results = [message for message in create.await_args_list[-1].kwargs["messages"]
                                 if message["role"] == "tool"]
                self.assertEqual(len(final_results), 15 * batch_size)
