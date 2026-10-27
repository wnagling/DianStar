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

from dianstar.__main__ import main
from dianstar.sessions.conversations import Conversation, list_conversations
from dianstar.app.runtime import run_turn
from dianstar.providers.deepseek import DeepSeekSession, _parse_unparsed_tool_calls
from dianstar.core.types import Observation, ToolAction
from dianstar.prompts.builder import ASSISTANT_INSTRUCTIONS, build_system_prompt
from dianstar.tools.local.weather import WEATHER_TOOL
from dianstar.tools.local.date_calculator import DATE_TOOL
from dianstar.tools.local.number_calculator import NUMBER_TOOL
from dianstar.tools.local.web import SEARCH_TOOL, FETCH_TOOL
from dianstar.tools.local.web_access import WEB_ACCESS_STATUS_TOOL as WEB_ACCESS_TOOL


class FakeStream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        self.closed = True


class DeepSeekTests(unittest.IsolatedAsyncioTestCase):
    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_streaming_keeps_tool_calls_and_reasoning_out_of_visible_answer(self, client_class, weather):
        def chunk(*, content=None, reasoning=None, calls=None, finish=None):
            return SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=calls),
                finish_reason=finish,
            )])

        def tool(index, *, call_id=None, name=None, arguments=None):
            return SimpleNamespace(index=index, id=call_id,
                                   function=SimpleNamespace(name=name, arguments=arguments))

        first = FakeStream([
            chunk(reasoning="先查询"),
            chunk(calls=[tool(0, call_id="call_1", name="get_weather",
                              arguments='{"location":"北京",')]),
            chunk(calls=[tool(0, arguments='"date":"today"}')], finish="tool_calls"),
        ])
        second = FakeStream([
            chunk(reasoning="根据结果回答"),
            chunk(content="北京今天"),
            chunk(content="最高 18℃。", finish="stop"),
        ])
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[first, second])
        weather.return_value = {"location": "北京", "date": "2026-10-22",
                                "temperature_max_c": 18.0, "temperature_min_c": 8.0}
        updates = []
        thinking_updates = []
        history = []
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("北京今天天气？", history=history,
                                        env_file=env_file, on_stream=updates.append,
                                        on_thinking=thinking_updates.append)
        self.assertEqual(answer, "北京今天最高 18℃。")
        self.assertEqual([item for item in updates if item], ["北京今天", answer])
        self.assertEqual([item for item in thinking_updates if item], ["先查询", "根据结果回答"])
        self.assertTrue(first.closed and second.closed)
        self.assertTrue(all(call.kwargs["stream"] for call in client.chat.completions.create.await_args_list))
        sent = client.chat.completions.create.await_args_list[1].kwargs["messages"]
        self.assertEqual(sent[2]["reasoning_content"], "先查询")
        self.assertEqual(sent[2]["tool_calls"][0]["function"]["arguments"],
                         '{"location":"北京","date":"today"}')
        self.assertEqual(history[-1]["reasoning_content"], "根据结果回答")
        weather.assert_awaited_once_with("北京", "today")

    def test_dsml_parser_uses_supplied_tool_schema_for_multiple_calls(self):
        tool = {"type": "function", "function": {"name": "lookup", "parameters": {
            "type": "object", "properties": {"item": {"type": "string"}},
            "required": ["item"],
        }}}
        raw = ('<｜DSML｜ calls>'
               '<｜DSML｜ invoke name="lookup"><｜DSML｜ parameter name="item" string="true">A</｜DSML｜ parameter></｜DSML｜ invoke>'
               '<｜DSML｜ invoke name="lookup"><｜DSML｜ parameter name="item" string="true">B</｜DSML｜ parameter></｜DSML｜ invoke>'
               '</｜DSML｜ calls>')
        actions = _parse_unparsed_tool_calls(raw, [tool])
        self.assertEqual([action.arguments for action in actions], [{"item": "A"}, {"item": "B"}])

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_ambiguous_city_returns_control_to_model_with_history(self, client_class, weather):
        from dianstar.tools.local.weather import WeatherLocationAmbiguous

        weather.side_effect = WeatherLocationAmbiguous("City name is ambiguous.")
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"洛阳","date":"today"}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content="你之前提到西安，我理解你指的是河南洛阳。",
                ),
            )]),
        ])
        history = [
            {"role": "user", "content": "西安有哪些景点？"},
            {"role": "assistant", "content": "兵马俑、城墙等。"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("我刚刚提到了西安，你觉得是哪个洛阳呢？", history=history, env_file=env_file)
        self.assertIn("河南洛阳", answer)
        self.assertEqual(history[-1]["content"], answer)
        self.assertEqual(client.chat.completions.create.await_count, 2)
        retry = client.chat.completions.create.await_args_list[1].kwargs
        self.assertEqual(retry["tool_choice"], "auto")
        self.assertIn("西安有哪些景点", str(retry["messages"]))
        self.assertIn("location_ambiguous", str(retry["messages"]))

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_invalid_tool_date_lets_model_answer_location_question(self, client_class, weather):
        from dianstar.tools.local.weather import WeatherDateInvalid

        weather.side_effect = WeatherDateInvalid("Date must be today or within the next 7 local days.")
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"洛阳","date":"2026-01-01"}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content="结合你刚才提到的西安，我理解你说的是河南洛阳。",
                ),
            )]),
        ])
        history = [
            {"role": "user", "content": "西安有哪些景点？"},
            {"role": "assistant", "content": "兵马俑、城墙等。"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("我刚刚提到了西安，你觉得是哪个洛阳呢？", history=history, env_file=env_file)
        self.assertIn("河南洛阳", answer)
        self.assertEqual(client.chat.completions.create.await_count, 2)
        retry = client.chat.completions.create.await_args_list[1].kwargs
        self.assertIn("invalid_date", str(retry["messages"]))
        self.assertEqual(retry["tool_choice"], "auto")

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_invalid_tool_date_can_be_retried_as_tomorrow(self, client_class, weather):
        from dianstar.tools.local.weather import WeatherDateInvalid

        weather.side_effect = [
            WeatherDateInvalid("Date must be today or within the next 7 local days."),
            {"location": "洛阳市, 河南, 中国", "date": "2026-10-22",
             "temperature_max_c": 14.9, "temperature_min_c": 9.6},
        ]
        first = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"洛阳","date":"2026-01-01"}',
        ))
        second = SimpleNamespace(id="call_2", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"洛阳","date":"tomorrow"}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[first]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[second]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content="河南洛阳明天预计 9.6～14.9℃。",
                ),
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("洛阳明天天气怎么样？", env_file=env_file)
        self.assertIn("河南洛阳明天", answer)
        self.assertEqual(weather.await_count, 2)
        self.assertEqual(weather.await_args_list[1].args[1], "tomorrow")
        self.assertEqual(client.chat.completions.create.await_count, 3)

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_unknown_city_asks_to_check_spelling(self, client_class, weather):
        from dianstar.tools.local.weather import WeatherLocationNotFound

        weather.side_effect = WeatherLocationNotFound("City not found.")
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"Xyzcity","date":"today"}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content="I couldn't find Xyzcity. Could you check the spelling or tell me its country?",
                ),
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("Xyzcity weather?", env_file=env_file)
        self.assertIn("Xyzcity", answer)
        self.assertIn("spelling", answer)
        self.assertEqual(weather.await_count, 1)
        self.assertEqual(client.chat.completions.create.await_args.kwargs["tool_choice"], "auto")

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_chinese_city_can_retry_in_pinyin_after_geocoding_failure(self, client_class, weather):
        from dianstar.tools.local.weather import WeatherLocationNotFound

        weather.side_effect = [
            WeatherLocationNotFound("City not found."),
            {"location": "哈密地区, 新疆, 中国", "date": "2026-10-20",
             "temperature_current_c": 8.0, "temperature_max_c": 13.0,
             "temperature_min_c": 7.0},
        ]
        first = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"哈密, 新疆","date":"today"}',
        ))
        second = SimpleNamespace(id="call_2", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"Hami, Xinjiang","date":"today"}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[first]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[second]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content="哈密今天当前 8°C，最高 13°C，最低 7°C。",
                ),
            )]),
        ])
        history = [
            {"role": "user", "content": "新疆今天气温呢？"},
            {"role": "assistant", "content": "你想查询新疆的哪个城市？"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("哈密", history=history, env_file=env_file)

        self.assertIn("哈密今天", answer)
        self.assertEqual(weather.await_count, 2)
        self.assertEqual(weather.await_args_list[1].args[0], "Hami, Xinjiang")

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_region_name_can_be_clarified_without_fabricating_weather(self, client_class, weather):
        from dianstar.tools.local.weather import WeatherLocationNotFound

        weather.side_effect = WeatherLocationNotFound("City not found.")
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"潮汕","date":"today"}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content="潮汕包括潮州、汕头、揭阳。你想查询哪座城市今天的气温？",
                ),
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("潮汕今天气温呢？", env_file=env_file)

        self.assertIn("潮州、汕头、揭阳", answer)
        weather.assert_awaited_once_with("潮汕", "today")

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_unsupported_tool_call_retries_as_text_answer(self, client_class, weather):
        unsupported = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="web_search", arguments='{}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[unsupported]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="苏州附近有无锡、上海、嘉兴。"),
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("苏州附近有哪些城市？", env_file=env_file)

        self.assertEqual(answer, "苏州附近有无锡、上海、嘉兴。")
        weather.assert_not_awaited()
        self.assertEqual(client.chat.completions.create.await_count, 2)
        self.assertEqual(client.chat.completions.create.await_args.kwargs["tool_choice"], "none")

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_multiple_weather_cities_are_queried_and_summarized(self, client_class, weather):
        calls = [
            SimpleNamespace(id="call_1", function=SimpleNamespace(
                name="get_weather", arguments='{"location":"广州","date":"today"}',
            )),
            SimpleNamespace(id="call_2", function=SimpleNamespace(
                name="get_weather", arguments='{"location":"杭州","date":"today"}',
            )),
            SimpleNamespace(id="call_3", function=SimpleNamespace(
                name="get_weather", arguments='{"location":"潮州","date":"today"}',
            )),
        ]
        weather.side_effect = [
            {"location": city, "date": "2026-10-20", "temperature_current_c": current,
             "temperature_max_c": high, "temperature_min_c": low}
            for city, current, high, low in [
                ("广州", 25.0, 29.0, 22.0),
                ("杭州", 18.0, 21.0, 15.0),
                ("潮州", 24.0, 28.0, 20.0),
            ]
        ]
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=calls),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content="广州今天当前 25°C，最高 29°C，最低 22°C。\n"
                            "杭州今天当前 18°C，最高 21°C，最低 15°C。\n"
                            "潮州今天当前 24°C，最高 28°C，最低 20°C。",
                ),
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("我想查询广州、杭州、潮州今天的气温", env_file=env_file)

        self.assertIn("广州今天", answer)
        self.assertIn("杭州今天", answer)
        self.assertIn("潮州今天", answer)
        self.assertEqual(weather.await_count, 3)
        messages = client.chat.completions.create.await_args.kwargs["messages"]
        self.assertEqual(len(messages[2]["tool_calls"]), 3)
        self.assertEqual([message["tool_call_id"] for message in messages[3:]],
                         ["call_1", "call_2", "call_3"])

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_failed_city_does_not_block_other_cities(self, client_class, weather):
        from dianstar.tools.local.weather import WeatherLocationNotFound

        calls = [SimpleNamespace(id=f"call_{index}", function=SimpleNamespace(
            name="get_weather", arguments=f'{{"location":"{city}","date":"today"}}',
        )) for index, city in enumerate(("潮汕", "北京"), 1)]
        weather.side_effect = [
            WeatherLocationNotFound("City not found."),
            {"location": "北京", "date": "2026-10-21", "temperature_max_c": 15.0,
             "temperature_min_c": 6.0},
        ]
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
                message=SimpleNamespace(content=None, tool_calls=calls))]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content="北京今天 6～15℃；潮汕需要具体城市。"))]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("潮汕和北京今天的天气？", env_file=env_file)

        self.assertIn("潮汕", answer)
        self.assertEqual(weather.await_count, 2)
        messages = client.chat.completions.create.await_args.kwargs["messages"]
        self.assertIn("location_not_found", messages[-2]["content"])
        self.assertIn('"temperature_max_c": 15.0', messages[-1]["content"])
        self.assertEqual(client.chat.completions.create.await_args.kwargs["tool_choice"], "auto")

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_model_retries_missing_location_with_shorter_region(self, client_class, weather):
        from dianstar.tools.local.weather import WeatherLocationNotFound

        weather.side_effect = [
            WeatherLocationNotFound("City not found."),
            {"location": "乌鲁木齐, 新疆, 中国", "date": "2026-10-20",
             "temperature_current_c": 8.0, "temperature_max_c": 12.0,
             "temperature_min_c": 2.0},
        ]
        first = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather",
            arguments='{"location":"乌鲁木齐市, 新疆维吾尔自治区, 中国","date":"today"}',
        ))
        second = SimpleNamespace(id="call_2", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"乌鲁木齐, 新疆, 中国","date":"today"}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[first]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[second]),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content="乌鲁木齐今天当前 8°C，最高 12°C，最低 2°C。",
                ),
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("乌鲁木齐市今天天气多少度？", env_file=env_file)
        self.assertIn("乌鲁木齐今天", answer)
        self.assertEqual(weather.await_count, 2)
        self.assertEqual(weather.await_args_list[1].args[0], "乌鲁木齐, 新疆, 中国")
        retry = client.chat.completions.create.await_args_list[1].kwargs
        self.assertEqual(retry["tool_choice"], "auto")
        self.assertIn("location_not_found", str(retry["messages"]))
    async def test_tool_result_allows_another_tool_call(self):
        call = SimpleNamespace(id="call_2", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"潮州","date":"tomorrow"}',
        ))
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call]),
        )])
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        session = DeepSeekSession(client, "潮州明天气温？", [], ASSISTANT_INSTRUCTIONS, [WEATHER_TOOL])
        observation = Observation(
            ToolAction("get_weather", {"location": "潮州", "date": "today"}),
            {"location": "潮州", "date": "2026-10-20"},
        )
        action = await session.think([observation])
        self.assertEqual(action.arguments["date"], "tomorrow")
        self.assertEqual(client.chat.completions.create.await_args.kwargs["tool_choice"], "auto")

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_empty_local_key_stops_before_network(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "API Key"):
                await run_turn("Hello", env_file=env_file)

        client_class.assert_not_called()

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
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

            self.assertEqual(await run_turn("Hello", env_file=env_file), "Hello back")

        client_class.assert_called_once_with(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            timeout=60.0,
            max_retries=0,
        )
        client.chat.completions.create.assert_awaited_once_with(
            model="deepseek-flash",
            messages=[{"role": "system", "content": build_system_prompt()},
                      {"role": "user", "content": "Hello"}],
            stream=False,
            tools=[WEATHER_TOOL, DATE_TOOL, NUMBER_TOOL, WEB_ACCESS_TOOL, SEARCH_TOOL, FETCH_TOOL],
            tool_choice="auto",
            extra_body={"thinking": {"type": "enabled"}},
        )

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_geography_question_after_weather_can_be_answered_directly(self, client_class, weather):
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="汕头附近有潮州和揭阳。",
            )),
        ]))
        history = [
            {"role": "user", "content": "北京今天多少度？"},
            {"role": "assistant", "content": "北京今天当前 8°C，最高 13°C，最低 7°C。"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("汕头附近城市有哪些？", history=history, env_file=env_file)

        self.assertEqual(answer, "汕头附近有潮州和揭阳。")
        weather.assert_not_awaited()
        client.chat.completions.create.assert_awaited_once()
        sent = client.chat.completions.create.await_args.kwargs["messages"]
        self.assertEqual(sent[0], {"role": "system", "content": build_system_prompt()})
        self.assertIn("自行选择可用工具", sent[0]["content"])
        self.assertNotIn("get_weather", sent[0]["content"])
        self.assertEqual(sent[-1], {"role": "user", "content": "汕头附近城市有哪些？"})

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_weather_tool_result_is_returned_to_deepseek(self, client_class, weather):
        weather.return_value = {"location": "北京", "date": "2026-10-20", "temperature_max_c": 18.0,
                                "temperature_min_c": 8.0, "source": "Open-Meteo forecast"}
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"北京","date":"2026-10-20"}'
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls", message=SimpleNamespace(
                    content=None, reasoning_content="先查北京天气", tool_calls=[call],
                )
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="北京明天最高 18°C，最低 8°C。")
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("北京明天气温？", env_file=env_file)
        self.assertEqual(answer, "北京明天最高 18°C，最低 8°C。")
        self.assertNotIn("当前气温", answer)
        weather.assert_awaited_once_with("北京", "2026-10-20")
        self.assertEqual(client.chat.completions.create.await_count, 2)
        sent = client.chat.completions.create.await_args_list[1].kwargs["messages"]
        self.assertEqual(sent[1], {"role": "user", "content": "北京明天气温？"})
        self.assertEqual(sent[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(sent[2]["reasoning_content"], "先查北京天气")
        self.assertEqual(sent[3]["role"], "tool")
        self.assertEqual(sent[3]["tool_call_id"], "call_1")
        self.assertIn('"temperature_max_c": 18.0', sent[3]["content"])
        self.assertNotIn("source", sent[3]["content"])

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_weather_answer_is_returned_without_temperature_rule(self, client_class, weather):
        weather.return_value = {"location": "潮州", "date": "2026-10-20",
                                "temperature_current_c": 25.2,
                                "temperature_max_c": 33.3, "temperature_min_c": 22.9}
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"潮州","date":"today"}'
        ))
        replies = [
            SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[call])),
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="潮州今天当前 25.2°C，最高 34°C，最低 22.9°C。")),
        ]
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[reply]) for reply in replies
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            history = []
            answer = await run_turn("潮州天气多少度？", history=history, env_file=env_file)
        self.assertEqual(answer, "潮州今天当前 25.2°C，最高 34°C，最低 22.9°C。")
        weather.assert_awaited_once_with("潮州", "today")
        self.assertEqual(history[-1]["content"], answer)
        self.assertEqual(client.chat.completions.create.await_count, 2)

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_numeric_general_knowledge_does_not_force_weather_tool(self, client_class, weather):
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="在标准大气压下，水约在 100°C 沸腾。")),
        ]))
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("标准大气压下水的沸点是多少？", env_file=env_file)
        self.assertIn("100°C", answer)
        weather.assert_not_awaited()

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_weather_without_city_can_request_location(self, client_class):
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="你想查询哪个城市的天气？"))
        ]))
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            answer = await run_turn("天气怎么样？", env_file=env_file)
        self.assertEqual(answer, "你想查询哪个城市的天气？")
        client.chat.completions.create.assert_awaited_once()
        self.assertNotIn("数据来源", answer)

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
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
                await run_turn("潮州今天天气多少度？", history=history, env_file=env_file)
        self.assertEqual(history, [])

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
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
            answer = await run_turn("潮州今天天气多少度？", history=history, env_file=env_file)
        self.assertEqual(answer, "潮州今天当前约 25.2°C，最高 27°C，最低 20°C。")
        self.assertNotIn("当地时间", answer)
        self.assertNotIn("模型估计", answer)
        self.assertNotIn("数据来源", answer)
        self.assertNotIn("DSML", answer)
        self.assertNotIn("DSML", repr(history))
        weather.assert_awaited_once_with("潮州, 广东, 中国", "today")
        self.assertEqual(client.chat.completions.create.await_count, 2)
        sent = client.chat.completions.create.await_args_list[1].kwargs["messages"]
        self.assertEqual(sent[1], {"role": "user", "content": "潮州今天天气多少度？"})
        self.assertEqual(sent[-1]["role"], "tool")
        self.assertIn('"temperature_current_c": 25.2', sent[-1]["content"])
        self.assertNotIn("current_time", sent[-1]["content"])

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
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
                finish_reason="stop", message=SimpleNamespace(content="潮州现在 25.2°C。")
            )]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            history = []
            answer = await run_turn("潮州现在几度？", history=history, env_file=env_file)
        self.assertEqual(answer, "潮州现在 25.2°C。")
        self.assertEqual(weather.await_count, 1)
        sent = client.chat.completions.create.await_args_list[2].kwargs["messages"]
        self.assertEqual(sent[1], {"role": "user", "content": "潮州现在几度？"})
        self.assertEqual(sent[-1]["role"], "tool")
        self.assertIn('"temperature_current_c": 25.2', sent[-1]["content"])
        self.assertNotIn("DSML", repr(sent))
        self.assertEqual(history[-1]["content"], answer)

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
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
                await run_turn("潮州今天天气多少度？", history=history, env_file=env_file)
        self.assertEqual(history, [])
        client.chat.completions.create.assert_awaited_once()

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
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
            await run_turn("你好", history=old_history, env_file=env_file)
        self.assertEqual(client.chat.completions.create.await_args.kwargs["messages"], [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": "你好"},
        ])

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_follow_up_sends_prior_successful_exchange(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            client = client_class.return_value
            client.chat.completions.create = AsyncMock(side_effect=[
                SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(
                        content="First answer", reasoning_content="First reasoning",
                    )
                )]),
                SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(
                        content="Second answer", reasoning_content="Second reasoning",
                    )
                )]),
            ])
            history = []

            await run_turn("First question", history=history, env_file=env_file)
            await run_turn("Follow up", history=history, env_file=env_file)

        self.assertEqual(client.chat.completions.create.await_args_list[1].kwargs["messages"], [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer", "reasoning_content": "First reasoning"},
            {"role": "user", "content": "Follow up"},
        ])
        self.assertEqual(history[-1], {"role": "assistant", "content": "Second answer",
                                       "reasoning_content": "Second reasoning"})

    def test_conversation_persists_reasoning_for_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            conversation = Conversation.create(directory)
            conversation.add_exchange("First", "Answer", reasoning_content="Reasoning")
            restored = Conversation.load(conversation.id, directory)
            self.assertEqual(restored.model_context_messages()[-1], {
                "role": "assistant", "content": "Answer", "reasoning_content": "Reasoning",
            })

    @patch("dianstar.tools.local.registry.get_weather", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_resumed_turn_sends_prior_tool_result_and_reasoning(self, client_class, weather):
        weather.return_value = {"location": "洛阳市, 河南, 中国", "date": "2026-10-21",
                                "temperature_max_c": 18.0, "temperature_min_c": 9.0}
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="get_weather", arguments='{"location":"洛阳, 河南, 中国","date":"today"}',
        ))
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
                message=SimpleNamespace(content=None, reasoning_content="先查询", tool_calls=[call]))]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content="洛阳今天 9～18℃。", reasoning_content="根据结果回答"))]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content="河南省的洛阳。", reasoning_content="参考工具结果"))]),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            env_file = directory / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            conversation = Conversation.create(directory)
            history = conversation.model_context_messages()
            answer = await run_turn("洛阳今天天气？", history=history, env_file=env_file)
            conversation.add_exchange("洛阳今天天气？", answer, model_turn_messages=history)
            resumed = Conversation.load(conversation.id, directory)
            self.assertEqual(len(resumed.context_messages()), 2)
            self.assertEqual(len(resumed.model_context_messages()), 4)
            second_history = resumed.model_context_messages()
            await run_turn("刚才是哪个省？", history=second_history, env_file=env_file)
            resumed.compress()
            self.assertEqual(Conversation.load(conversation.id, directory).model_context_messages(), [])

        request = client.chat.completions.create.await_args_list[2].kwargs["messages"]
        self.assertEqual(request[2]["reasoning_content"], "先查询")
        self.assertEqual(request[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(request[3]["role"], "tool")
        self.assertIn("河南", request[3]["content"])
        self.assertEqual(request[4]["reasoning_content"], "根据结果回答")
        self.assertEqual(request[5], {"role": "user", "content": "刚才是哪个省？"})

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
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
                await run_turn("Failed question", history=history, env_file=env_file)
            self.assertEqual(history, [])
            await run_turn("Retry", history=history, env_file=env_file)

        self.assertEqual(client.chat.completions.create.await_args_list[1].kwargs["messages"], [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": "Retry"},
        ])

    def test_connection_error_returns_failure_without_request_details(self):
        error = APIConnectionError(
            message="request contained a sensitive value",
            request=Request("POST", "https://api.deepseek.com/chat/completions"),
        )
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["dianstar", "Hello"]),
            patch("dianstar.ui.cli.run_turn", side_effect=error),
            redirect_stderr(stderr),
        ):
            result = main()

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "错误：无法连接 DeepSeek，请检查网络后重试。\n")

    def test_no_argument_enters_chat_and_exit_stops(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            with (
                patch.object(sys, "argv", ["dianstar", "--plain"]),
                patch("builtins.input", side_effect=["Hello", "/exit"]),
                patch("dianstar.ui.cli.Conversation.create", return_value=conversation),
                patch("dianstar.ui.cli.run_turn", new_callable=AsyncMock, return_value="Hi") as ask,
                redirect_stdout(stdout),
            ):
                result = main()

            restored = Conversation.load(conversation.id, Path(temporary))

        self.assertEqual(result, 0)
        ask.assert_awaited_once()
        self.assertEqual(ask.await_args.args, ("Hello",))
        self.assertIn("DianStar> Hi", stdout.getvalue())
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
                patch.object(sys, "argv", ["dianstar", "--plain", "--resume", old.id]),
                patch("builtins.input", side_effect=[
                    "/clear", "/compress", "After compress", "/new", "Fresh question", "/exit"
                ]),
                patch("dianstar.ui.cli.Conversation.load", return_value=old),
                patch("dianstar.ui.cli.Conversation.create", return_value=new),
                patch("dianstar.ui.cli.run_turn", new_callable=AsyncMock,
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

    def test_interactive_resume_switches_to_saved_context(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = Conversation.create(directory)
            first.add_exchange("First history", "First reply")
            second = Conversation.create(directory)
            second.add_exchange("Second history", "Second reply")
            load = Conversation.load
            with (
                patch.object(sys, "argv", ["dianstar", "--plain"]),
                patch("builtins.input", side_effect=[
                    "Before switch", f"/resume {second.id}", "After switch", "/exit",
                ]),
                patch("dianstar.ui.cli.Conversation.create", return_value=first),
                patch("dianstar.ui.cli.Conversation.load",
                      side_effect=lambda conversation_id, _directory=None: load(conversation_id, directory)),
                patch("dianstar.app.commands.list_conversations", return_value=[]),
                patch("dianstar.ui.cli.run_turn", new_callable=AsyncMock,
                      side_effect=["Before answer", "After answer"]) as ask,
                redirect_stdout(output),
            ):
                result = main()
            first_restored = load(first.id, directory)
            second_restored = load(second.id, directory)

        self.assertEqual(result, 0)
        self.assertEqual(ask.await_args_list[0].kwargs["history"][0]["content"], "First history")
        self.assertEqual(ask.await_args_list[1].kwargs["history"][0]["content"], "Second history")
        self.assertEqual(first_restored.messages[-1]["content"], "Before answer")
        self.assertEqual(second_restored.messages[-1]["content"], "After answer")
        self.assertIn("已切换到会话：Second", output.getvalue())

    def test_invalid_interactive_resume_keeps_current_session(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            current = Conversation.create(directory)
            load = Conversation.load
            with (
                patch.object(sys, "argv", ["dianstar", "--plain"]),
                patch("builtins.input", side_effect=["/resume ../.env", "Still here", "/exit"]),
                patch("dianstar.ui.cli.Conversation.create", return_value=current),
                patch("dianstar.ui.cli.Conversation.load",
                      side_effect=lambda conversation_id, _directory=None: load(conversation_id, directory)),
                patch("dianstar.app.commands.list_conversations", return_value=[]),
                patch("dianstar.ui.cli.run_turn", new_callable=AsyncMock,
                      return_value="Yes") as ask,
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                result = main()
            restored = load(current.id, directory)

        self.assertEqual(result, 0)
        self.assertIn("没有找到该会话", stderr.getvalue())
        ask.assert_awaited_once()
        self.assertEqual(restored.messages[-1]["content"], "Yes")

    def test_resume_by_name_uses_named_conversation_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            current = Conversation.create(directory)
            target = Conversation.create(directory)
            target.add_exchange("Earlier question", "Earlier answer")
            target.rename("旅行计划")
            entries = list_conversations(directory)
            load = Conversation.load
            with (
                patch.object(sys, "argv", ["dianstar", "--plain"]),
                patch("builtins.input", side_effect=["/resume 旅行计划", "Continue", "/exit"]),
                patch("dianstar.ui.cli.Conversation.create", return_value=current),
                patch("dianstar.ui.cli.Conversation.load",
                      side_effect=lambda conversation_id, _directory=None: load(conversation_id, directory)),
                patch("dianstar.app.commands.list_conversations", return_value=entries),
                patch("dianstar.ui.cli.run_turn", new_callable=AsyncMock,
                      return_value="Continued") as ask,
                redirect_stdout(io.StringIO()),
            ):
                result = main()
            restored = load(target.id, directory)

        self.assertEqual(result, 0)
        self.assertEqual(ask.await_args.kwargs["history"][0]["content"], "Earlier question")
        self.assertEqual(restored.messages[-1]["content"], "Continued")

    def test_switch_selects_numbered_conversation_and_rename_before_first_question(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            current = Conversation.create(directory)
            target = Conversation.create(directory)
            target.add_exchange("Old question", "Old answer")
            target.rename("目标会话")
            entries = list_conversations(directory)
            selection = next(index for index, entry in enumerate(entries, 1) if entry.id == target.id)
            load = Conversation.load
            with (
                patch.object(sys, "argv", ["dianstar", "--plain"]),
                patch("builtins.input", side_effect=[
                    "/rename", "临时会话", "/switch", str(selection), "Next", "/exit",
                ]),
                patch("dianstar.ui.cli.Conversation.create", return_value=current),
                patch("dianstar.ui.cli.Conversation.load",
                      side_effect=lambda conversation_id, _directory=None: load(conversation_id, directory)),
                patch("dianstar.app.commands.list_conversations", return_value=entries),
                patch("dianstar.ui.cli.run_turn", new_callable=AsyncMock,
                      return_value="Next answer") as ask,
                redirect_stdout(output),
            ):
                result = main()
            renamed = load(current.id, directory)
            switched = load(target.id, directory)

        self.assertEqual(result, 0)
        self.assertEqual(renamed.name, "临时会话")
        self.assertEqual(switched.messages[-1]["content"], "Next answer")
        self.assertEqual(ask.await_args.kwargs["history"][0]["content"], "Old question")
        self.assertIn("最近使用的会话：", output.getvalue())
        self.assertIn("已切换到会话：目标会话", output.getvalue())

    def test_duplicate_resume_name_keeps_current_conversation(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            current = Conversation.create(directory)
            first = Conversation.create(directory)
            second = Conversation.create(directory)
            first.rename("同名")
            second.rename("同名")
            entries = list_conversations(directory)
            with (
                patch.object(sys, "argv", ["dianstar", "--plain"]),
                patch("builtins.input", side_effect=["/resume 同名", "Still here", "/exit"]),
                patch("dianstar.ui.cli.Conversation.create", return_value=current),
                patch("dianstar.app.commands.list_conversations", return_value=entries),
                patch("dianstar.ui.cli.run_turn", new_callable=AsyncMock,
                      return_value="Yes"),
                redirect_stdout(output),
            ):
                result = main()
            restored = Conversation.load(current.id, directory)

        self.assertEqual(result, 0)
        self.assertIn("多个同名会话", output.getvalue())
        self.assertEqual(restored.messages[-1]["content"], "Yes")


if __name__ == "__main__":
    unittest.main()
