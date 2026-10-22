# 编排一轮提问：调用模型与工具，并更新成功的对话历史。

import json
from pathlib import Path
from typing import Callable

from .config import load_model_settings
from .deepseek import ENV_FILE, DeepSeekSession, create_client, usable_history
from .loop import Observation, ToolAction, run_loop
from .prompts import ASSISTANT_INSTRUCTIONS
from .tools.weather import (
    WEATHER_TOOL, WeatherDateInvalid, WeatherLocationAmbiguous, WeatherLocationMismatch,
    WeatherLocationNotFound,
    WeatherUnavailable, get_weather,
)
from .tools.weather_contract import weather_data


async def ask_deepseek(
    question: str,
    *,
    history: list[dict[str, object]] | None = None,
    env_file: Path = ENV_FILE,
    on_stream: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
) -> str:
    """Run one bounded agent task and return the model's final text."""
    if not question.strip():
        raise ValueError("Please provide a question.")

    cleaned_history = usable_history(history)
    settings = load_model_settings(env_file.parent / ".local" / "config.json")
    if not settings.default_model:
        raise ValueError("当前默认 Key 尚未添加模型，请在 /config 的 model 中先添加模型。")
    client = create_client(env_file, key_id=settings.default_key_id)
    def observe_error(action: ToolAction, code: str) -> Observation:
        result = {"error": code, "requested_location": action.arguments["location"],
                  "requested_date": action.arguments["date"]}
        session.observe(action, json.dumps(result, ensure_ascii=False))
        return Observation(action, result)

    async def act_weather(action: ToolAction) -> Observation:
        try:
            result = await get_weather(action.arguments["location"], action.arguments["date"])
        except WeatherLocationAmbiguous:
            return observe_error(action, "location_ambiguous")
        except WeatherDateInvalid:
            return observe_error(action, "invalid_date")
        except WeatherLocationMismatch:
            return observe_error(action, "location_mismatch")
        except WeatherLocationNotFound:
            return observe_error(action, "location_not_found")
        except WeatherUnavailable:
            return observe_error(action, "weather_unavailable")
        session.observe(action, weather_data(result))
        return Observation(action, result)

    # 同一份注册表绑定模型可见的工具定义和实际执行函数。
    tool_registry = {WEATHER_TOOL["function"]["name"]: (WEATHER_TOOL, act_weather)}
    session = DeepSeekSession(
        client, question, cleaned_history, ASSISTANT_INSTRUCTIONS,
        [schema for schema, _ in tool_registry.values()],
        on_stream=on_stream,
        on_thinking=on_thinking,
        model=settings.default_model,
    )

    async def act(action: ToolAction) -> Observation:
        registration = tool_registry.get(action.name)
        if registration is None:
            raise ValueError("模型返回了无法执行的工具调用。")
        _, handler = registration
        return await handler(action)

    async with client:
        answer = await run_loop(session.think, act, max_steps=6)
    if history is not None:
        turn_messages = session.messages[len(cleaned_history) + 1:]
        if (not turn_messages or turn_messages[-1].get("role") != "assistant"
                or turn_messages[-1].get("content") != answer):
            turn_messages = [{"role": "user", "content": question},
                             {"role": "assistant", "content": answer}]
        history.extend(turn_messages)
    return answer
