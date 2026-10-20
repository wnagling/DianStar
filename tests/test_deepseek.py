# 离线测试 DeepSeek 问答、工具调用、终端交互和会话行为。

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import Request
from openai import APIConnectionError

from agent_try.__main__ import main
from agent_try.conversations import Conversation
from agent_try.agent import ask_deepseek
from agent_try.deepseek import DeepSeekSession
from agent_try.loop import Observation, ToolAction, Verdict
from agent_try.tools.weather import WEATHER_TOOL
from agent_try.tools.weather_contract import WEATHER_REPLY_INSTRUCTIONS


class DeepSeekTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_date_feedback_allows_another_tool_call(self):
        call = SimpleNamespace(id="call_2", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"潮州","date":"tomorrow"}',
        ))
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call]),
        )])
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        session = DeepSeekSession(client, "潮州明天气温？", [], WEATHER_REPLY_INSTRUCTIONS, [WEATHER_TOOL])
        observation = Observation(
            ToolAction("get_weather", {"location": "潮州", "date": "today"}),
            {"location": "潮州", "date": "2026-10-20"},
        )
        action = await session.think(
            [observation], Verdict("retry", "请重新查询用户指定的日期。", retry_tool=True),
        )
        self.assertEqual(action.arguments["date"], "tomorrow")
        self.assertEqual(client.chat.completions.create.await_args.kwargs["tool_choice"], "auto")

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_empty_local_key_stops_before_network(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                await ask_deepseek("Hello", env_file=env_file)

        client_class.assert_not_called()

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_local_key_sends_one_question_and_returns_answer(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            client = client_class.return_value
            client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="Hello back"),
                    )
                ]
            ))

            self.assertEqual(await ask_deepseek("Hello", env_file=env_file), "Hello back")

        client_class.assert_called_once_with(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            timeout=60.0,
            max_retries=0,
        )
        client.chat.completions.create.assert_awaited_once_with(
            model="deepseek-flash",
            messages=[{"role": "system", "content": WEATHER_REPLY_INSTRUCTIONS},
                      {"role": "user", "content": "Hello"}],
            stream=False,
            tools=[WEATHER_TOOL],
            tool_choice="auto",
            extra_body={"thinking": {"type": "disabled"}},
        )

    @patch("agent_try.agent.get_weather", new_callable=AsyncMock)
    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_weather_tool_result_is_returned_to_deepseek(self, client_class, weather):
        weather.return_value = {"location": "北京", "date": "2026-10-20", "temperature_max_c": 18.0,
                                "temperature_min_c": 8.0, "source": "Open-Meteo forecast"}
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"北京","date":"2026-10-20"}'
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call])
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="北京明天最高 18°C，最低 8°C。")
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await ask_deepseek("北京明天气温？", env_file=env_file)
        self.assertEqual(answer, "北京明天最高 18°C，最低 8°C。")
        self.assertNotIn("当前气温", answer)
        weather.assert_awaited_once_with("北京", "2026-10-20")
        self.assertEqual(client.chat.completions.create.await_count, 2)
        sent = client.chat.completions.create.await_args_list[1].kwargs["messages"]
        self.assertEqual(sent[1], {"role": "user", "content": "北京明天气温？"})
        self.assertEqual(sent[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(sent[3]["role"], "tool")
        self.assertEqual(sent[3]["tool_call_id"], "call_1")
        self.assertIn('"temperature_max_c": 18.0', sent[3]["content"])
        self.assertNotIn("source", sent[3]["content"])

    @patch("agent_try.agent.get_weather", new_callable=AsyncMock)
    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_weather_answer_requires_evidence_and_retries_bad_temperature(self, client_class, weather):
        weather.return_value = {"location": "潮州", "date": "2026-10-20",
                                "temperature_current_c": 25.2,
                                "temperature_max_c": 33.3, "temperature_min_c": 22.9}
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"潮州","date":"today"}'
        ))
        replies = [
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="潮州现在 30°C。")),
            SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call])),
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="潮州今天当前 25.2°C，最高 34°C，最低 22.9°C。")),
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="潮州今天当前 25.2°C，最高 33.3°C，最低 22.9°C。")),
        ]
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[reply]) for reply in replies
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            history = []
            answer = await ask_deepseek("潮州天气多少度？", history=history, env_file=env_file)
        self.assertEqual(answer, "潮州今天当前 25.2°C，最高 33.3°C，最低 22.9°C。 你想查询哪天的天气？")
        weather.assert_awaited_once_with("潮州", "today")
        self.assertEqual(history[-1]["content"], answer)
        self.assertEqual(client.chat.completions.create.await_count, 4)

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_weather_without_city_can_request_location(self, client_class):
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="你想查询哪个城市的天气？"))
        ]))
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await ask_deepseek("天气怎么样？", env_file=env_file)
        self.assertEqual(answer, "你想查询哪个城市的天气？")
        client.chat.completions.create.assert_awaited_once()
        self.assertNotIn("数据来源", answer)

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_incomplete_raw_dsml_is_rejected(self, client_class):
        raw = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="get_weather">...</｜｜DSML｜｜ invoke>'
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(return_value=
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=raw, tool_calls=None)
            )])
        )
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            history = []
            with self.assertRaisesRegex(ValueError, "invalid tool markup"):
                await ask_deepseek("潮州今天天气多少度？", history=history, env_file=env_file)
        self.assertEqual(history, [])

    @patch("agent_try.agent.get_weather", new_callable=AsyncMock)
    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_complete_raw_dsml_executes_weather_without_leaking_markup(self, client_class, weather):
        weather.return_value = {
            "location": "潮州, 广东, 中国", "date": "2026-10-19",
            "temperature_max_c": 27.0, "temperature_min_c": 20.0,
            "temperature_current_c": 25.2, "current_time": "2026-10-19T14:15",
        }
        raw = (
            '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="get_weather">'
            '<｜｜DSML｜｜ parameter name="location" string="true">潮州, 广东, 中国</｜｜DSML｜｜ parameter>'
            '<｜｜DSML｜｜ parameter name="date" string="true">today</｜｜DSML｜｜ parameter>'
            '</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>'
        )
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=raw, tool_calls=None)
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content="潮州今天当前约 25.2°C，最高 27°C，最低 20°C。"
                )
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            history = []
            answer = await ask_deepseek("潮州今天天气多少度？", history=history, env_file=env_file)
        self.assertEqual(answer, "潮州今天当前约 25.2°C，最高 27°C，最低 20°C。")
        self.assertNotIn("当地时间", answer)
        self.assertNotIn("模型估计", answer)
        self.assertNotIn("数据来源", answer)
        self.assertNotIn("DSML", answer)
        self.assertNotIn("DSML", repr(history))
        weather.assert_awaited_once_with("潮州, 广东, 中国", "today")
        self.assertEqual(client.chat.completions.create.await_count, 2)
        sent = client.chat.completions.create.await_args_list[1].kwargs["messages"]
        self.assertEqual(sent[-2], {"role": "user", "content": "潮州今天天气多少度？"})
        self.assertIn('"temperature_current_c": 25.2', sent[-1]["content"])
        self.assertNotIn("current_time", sent[-1]["content"])

    @patch("agent_try.agent.get_weather", new_callable=AsyncMock)
    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_repeated_raw_tool_markup_uses_clean_result_context(self, client_class, weather):
        weather.return_value = {"location": "潮州", "date": "2026-10-19",
                                "temperature_current_c": 25.2,
                                "temperature_max_c": 27.0, "temperature_min_c": 20.0}
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"潮州","date":"today"}'
        ))
        raw = (
            '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="get_weather">'
            '<｜｜DSML｜｜ parameter name="location" string="true">潮州</｜｜DSML｜｜ parameter>'
            '<｜｜DSML｜｜ parameter name="date" string="true">today</｜｜DSML｜｜ parameter>'
            '</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>'
        )
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call])
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=raw, tool_calls=None)
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="潮州现在 25.2°C，最高 27°C，最低 20°C。")
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            history = []
            answer = await ask_deepseek("潮州现在几度？", history=history, env_file=env_file)
        self.assertEqual(answer, "潮州现在 25.2°C，最高 27°C，最低 20°C。")
        self.assertEqual(weather.await_count, 1)
        sent = client.chat.completions.create.await_args_list[2].kwargs["messages"]
        self.assertEqual(sent[1], {"role": "user", "content": "潮州现在几度？"})
        self.assertIn('"temperature_current_c": 25.2', sent[2]["content"])
        self.assertNotIn("DSML", repr(sent))
        self.assertEqual(history[-1]["content"], answer)

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_raw_dsml_failure_is_not_shown_or_saved(self, client_class):
        raw = '<｜｜DSML｜｜ invoke name="get_weather">...</｜｜DSML｜｜ invoke>'
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=raw, tool_calls=None))
        ]))
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            history = []
            with self.assertRaisesRegex(ValueError, "invalid tool markup"):
                await ask_deepseek("潮州今天天气多少度？", history=history, env_file=env_file)
        self.assertEqual(history, [])
        client.chat.completions.create.assert_awaited_once()

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_old_raw_dsml_turn_is_not_sent_as_context(self, client_class):
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="你好"))
        ]))
        old_history = [
            {"role": "user", "content": "潮州今天天气多少度？"},
            {"role": "assistant", "content": '<｜｜DSML｜｜ invoke name="get_weather">...' },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            await ask_deepseek("你好", history=old_history, env_file=env_file)
        self.assertEqual(client.chat.completions.create.await_args.kwargs["messages"], [
            {"role": "system", "content": WEATHER_REPLY_INSTRUCTIONS},
            {"role": "user", "content": "你好"},
        ])

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_follow_up_sends_prior_successful_exchange(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            client = client_class.return_value
            client.chat.completions.create = AsyncMock(side_effect=[
                SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="First answer")
                )]),
                SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="Second answer")
                )]),
            ])
            history = []

            await ask_deepseek("First question", history=history, env_file=env_file)
            await ask_deepseek("Follow up", history=history, env_file=env_file)

        self.assertEqual(client.chat.completions.create.await_args_list[1].kwargs["messages"], [
            {"role": "system", "content": WEATHER_REPLY_INSTRUCTIONS},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow up"},
        ])
        self.assertEqual(history[-1], {"role": "assistant", "content": "Second answer"})

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_failed_turn_does_not_enter_history(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            client = client_class.return_value
            client.chat.completions.create = AsyncMock(side_effect=[
                SimpleNamespace(choices=[]),
                SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="Recovered")
                )]),
            ])
            history = []

            with self.assertRaisesRegex(ValueError, "no answer"):
                await ask_deepseek("Failed question", history=history, env_file=env_file)
            self.assertEqual(history, [])
            await ask_deepseek("Retry", history=history, env_file=env_file)

        self.assertEqual(client.chat.completions.create.await_args_list[1].kwargs["messages"], [
            {"role": "system", "content": WEATHER_REPLY_INSTRUCTIONS},
            {"role": "user", "content": "Retry"},
        ])

    def test_connection_error_returns_failure_without_request_details(self):
        error = APIConnectionError(
            message="request contained a sensitive value",
            request=Request("POST", "https://api.deepseek.com/chat/completions"),
        )
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["agent_try", "Hello"]),
            patch("agent_try.__main__.ask_deepseek", side_effect=error),
            redirect_stderr(stderr),
        ):
            result = main()

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "Error: Could not connect to DeepSeek.\n")

    def test_no_argument_enters_chat_and_exit_stops(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            with (
                patch.object(sys, "argv", ["agent_try"]),
                patch("builtins.input", side_effect=["Hello", "/exit"]),
                patch("agent_try.__main__.Conversation.create", return_value=conversation),
                patch("agent_try.__main__.ask_deepseek", new_callable=AsyncMock, return_value="Hi") as ask,
                redirect_stdout(stdout),
            ):
                result = main()

            restored = Conversation.load(conversation.id, Path(temporary))

        self.assertEqual(result, 0)
        ask.assert_awaited_once()
        self.assertEqual(ask.await_args.args, ("Hello",))
        self.assertIn("DeepSeek> Hi", stdout.getvalue())
        self.assertEqual(restored.messages[-1], {"role": "assistant", "content": "Hi"})

    def test_compress_keeps_record_but_clears_model_context_after_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            conversation = Conversation.create(directory)
            conversation.add_exchange("First", "Answer")
            conversation.compress()
            restored = Conversation.load(conversation.id, directory)

            self.assertEqual(len(restored.messages), 2)
            self.assertEqual(restored.context_messages(), [])
            restored.add_exchange("New", "Reply")
            restored_again = Conversation.load(conversation.id, directory)
            self.assertEqual(restored_again.context_messages(), [
                {"role": "user", "content": "New"},
                {"role": "assistant", "content": "Reply"},
            ])

    def test_conversation_ids_are_unique_and_invalid_resume_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = Conversation.create(directory)
            second = Conversation.create(directory)
            self.assertNotEqual(first.id, second.id)
            with self.assertRaisesRegex(ValueError, "Invalid conversation ID"):
                Conversation.load("../.env", directory)

    def test_resume_compress_and_new_route_questions_to_separate_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old = Conversation.create(directory)
            old.add_exchange("Old question", "Old answer")
            new = Conversation.create(directory)
            with (
                patch.object(sys, "argv", ["agent_try", "--resume", old.id]),
                patch("builtins.input", side_effect=[
                    "/clear", "/compress", "After compress", "/new", "Fresh question", "/exit"
                ]),
                patch("agent_try.__main__.Conversation.load", return_value=old),
                patch("agent_try.__main__.Conversation.create", return_value=new),
                patch("agent_try.__main__.ask_deepseek", new_callable=AsyncMock,
                      side_effect=["After answer", "Fresh answer"]) as ask,
                redirect_stdout(io.StringIO()),
            ):
                result = main()

            old_restored = Conversation.load(old.id, directory)
            new_restored = Conversation.load(new.id, directory)

        self.assertEqual(result, 0)
        self.assertEqual(ask.await_count, 2)
        self.assertEqual(ask.await_args_list[0].kwargs["history"], [])
        self.assertEqual(ask.await_args_list[1].kwargs["history"], [])
        self.assertEqual(old_restored.context_messages()[-1]["content"], "After answer")
        self.assertEqual(new_restored.messages[0]["content"], "Fresh question")


if __name__ == "__main__":
    unittest.main()
