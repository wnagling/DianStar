"""Send a question and optional conversation history to DeepSeek."""

import asyncio
import json
import re
from pathlib import Path
from xml.etree import ElementTree

from dotenv import dotenv_values
from openai import AsyncOpenAI

from .tools.weather import WeatherUnavailable, get_weather


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get a city's temperature in Celsius. For today, returns current modeled temperature with its local time plus the day's forecast high and low; future dates return forecast high and low. Ask for a city and date if missing. The date must be today, tomorrow, or YYYY-MM-DD within the next 7 days. Do not guess a missing city.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City, optionally followed by a region or country"},
                "date": {"type": "string", "description": "today, tomorrow, or YYYY-MM-DD"},
            },
            "required": ["location", "date"],
            "additionalProperties": False,
        },
    },
}


def _is_unparsed_tool_call(content: str | None) -> bool:
    """Recognize leaked DeepSeek tool markup without treating it as executable data."""
    return bool(content and "DSML" in content and "invoke" in content)


def _parse_unparsed_weather_call(content: str) -> dict[str, str]:
    """Accept only one complete DSML get_weather call with two string arguments."""
    if len(content) > 2048:
        raise ValueError("DeepSeek returned invalid tool markup.")
    normalized = re.sub(
        r"<(/?)[|｜]{1,2}DSML[|｜]{1,2}\s*", r"<\1", content.strip()
    )
    try:
        root = ElementTree.fromstring(normalized)
    except ElementTree.ParseError as error:
        raise ValueError("DeepSeek returned invalid tool markup.") from error
    if root.tag in {"calls", "tool_calls", "function_calls"}:
        if root.text and root.text.strip():
            raise ValueError("DeepSeek returned invalid tool markup.")
        calls = list(root)
        if len(calls) != 1:
            raise ValueError("DeepSeek returned invalid tool markup.")
        invoke = calls[0]
    else:
        invoke = root
    if invoke.tag != "invoke" or invoke.attrib != {"name": "get_weather"}:
        raise ValueError("DeepSeek returned invalid tool markup.")
    if invoke.text and invoke.text.strip():
        raise ValueError("DeepSeek returned invalid tool markup.")
    arguments = {}
    for parameter in invoke:
        if (
            parameter.tag != "parameter"
            or parameter.attrib.get("string") != "true"
            or set(parameter.attrib) != {"name", "string"}
            or parameter.attrib["name"] not in {"location", "date"}
            or parameter.attrib["name"] in arguments
            or list(parameter)
            or (parameter.tail and parameter.tail.strip())
        ):
            raise ValueError("DeepSeek returned invalid tool markup.")
        arguments[parameter.attrib["name"]] = parameter.text or ""
    if set(arguments) != {"location", "date"} or (invoke.tail and invoke.tail.strip()):
        raise ValueError("DeepSeek returned invalid tool markup.")
    return arguments


def _format_weather(weather: dict[str, object]) -> str:
    lines = [f"{weather['location']} {weather['date']}："]
    if "temperature_current_c" in weather:
        if weather["temperature_current_c"] is None:
            lines.append("当前气温暂不可用。")
        else:
            local_time = str(weather["current_time"])[11:16]
            lines.append(f"当前气温约 {weather['temperature_current_c']}°C（当地时间 {local_time}，模型估计）。")
    lines.append(f"当日最高 {weather['temperature_max_c']}°C，最低 {weather['temperature_min_c']}°C。")
    return "\n".join(lines)


def _usable_history(history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Skip old turns whose assistant answer leaked tool markup."""
    cleaned = []
    for message in history or []:
        if message["role"] == "assistant" and _is_unparsed_tool_call(message["content"]):
            if cleaned and cleaned[-1]["role"] == "user":
                cleaned.pop()
            continue
        cleaned.append(message)
    return cleaned


async def ask_deepseek(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    env_file: Path = ENV_FILE,
) -> str:
    """Ask DeepSeek and optionally answer one forecast tool call."""
    if not question.strip():
        raise ValueError("Please provide a question.")
    if not env_file.is_file():
        raise ValueError(".env is missing from the project root.")

    api_key = (dotenv_values(env_file).get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("Set DEEPSEEK_API_KEY in the project .env file.")

    messages = [*_usable_history(history), {"role": "user", "content": question}]
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=BASE_URL,
        timeout=60.0,
        max_retries=0,
    )

    async with client:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=MODEL,
                messages=messages,
                stream=False,
                tools=[WEATHER_TOOL],
                tool_choice="auto",
                extra_body={"thinking": {"type": "disabled"}},
            ),
            timeout=60.0,
        )
    if not response.choices:
        raise ValueError("DeepSeek returned no answer.")
    choice = response.choices[0]
    if choice.finish_reason == "tool_calls":
        calls = choice.message.tool_calls or []
        if len(calls) != 1 or calls[0].function.name != "get_weather":
            raise ValueError("DeepSeek returned an unsupported tool call.")
        try:
            arguments = json.loads(calls[0].function.arguments)
        except (ValueError, TypeError) as error:
            raise ValueError("DeepSeek returned invalid tool arguments.") from error
        if not isinstance(arguments, dict) or set(arguments) != {"location", "date"}:
            raise ValueError("DeepSeek returned invalid tool arguments.")
    elif _is_unparsed_tool_call(choice.message.content):
        arguments = _parse_unparsed_weather_call(choice.message.content)
    elif choice.finish_reason == "stop" and choice.message.content:
        answer = choice.message.content
        arguments = None
    else:
        raise ValueError("DeepSeek did not return a complete text answer.")
    if arguments is not None:
        try:
            answer = _format_weather(await get_weather(arguments["location"], arguments["date"]))
        except WeatherUnavailable as error:
            raise ValueError(f"Weather lookup failed: {error}") from error
    if history is not None:
        history.extend(({"role": "user", "content": question}, {"role": "assistant", "content": answer}))
    return answer
