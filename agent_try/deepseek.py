"""Send a question and optional conversation history to DeepSeek."""

import asyncio
import json
import re
from pathlib import Path
from xml.etree import ElementTree

from dotenv import dotenv_values
from openai import AsyncOpenAI

from .loop import Observation, ToolAction, Verdict, run_loop
from .tools.weather import WeatherUnavailable, get_weather
from .weather_contract import DATE_WORDS, verify_weather, weather_goal


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get a city's temperature in Celsius. For today, returns current temperature and the day's high and low; future dates return the high and low. If date is missing, use today. Ask for a city if missing; never guess a city. Date must be today, tomorrow, or YYYY-MM-DD within the next 7 days.",
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


WEATHER_REPLY_INSTRUCTIONS = (
    "天气问题必须先调用 get_weather，缺少日期时默认 today；缺少地点时询问用户地点。"
    "请根据用户的问题和天气查询结果回答。今天要回答当前气温及当日最高、最低气温；"
    "未来日期回答最高、最低气温。当前气温为空时说明暂不可用。"
    "不要写数据来源、观测时间或‘模型估计’，不要输出工具调用标记。"
    "只使用查询结果中的气温，准确写出查询到的城市，不要编造数值。"
)


def _weather_data(weather: dict[str, object]) -> str:
    """Only give the model the fields it needs to answer the question."""
    fields = ("location", "date", "temperature_current_c", "temperature_max_c", "temperature_min_c")
    return json.dumps({key: weather[key] for key in fields if key in weather}, ensure_ascii=False)


def _tool_arguments(call) -> dict[str, str]:
    if call.function.name != "get_weather":
        raise ValueError("DeepSeek returned an unsupported tool call.")
    try:
        arguments = json.loads(call.function.arguments)
    except (ValueError, TypeError) as error:
        raise ValueError("DeepSeek returned invalid tool arguments.") from error
    if not isinstance(arguments, dict) or set(arguments) != {"location", "date"}:
        raise ValueError("DeepSeek returned invalid tool arguments.")
    if not all(isinstance(value, str) and value.strip() for value in arguments.values()):
        raise ValueError("DeepSeek returned invalid tool arguments.")
    return arguments


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
    """Ask DeepSeek, returning any weather result to the model before answering."""
    if not question.strip():
        raise ValueError("Please provide a question.")
    if not env_file.is_file():
        raise ValueError(".env is missing from the project root.")

    api_key = (dotenv_values(env_file).get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("Set DEEPSEEK_API_KEY in the project .env file.")

    cleaned_history = _usable_history(history)
    must_check_weather = weather_goal(question, cleaned_history)
    date_unspecified = must_check_weather and not DATE_WORDS.search(question)
    messages = [{"role": "system", "content": WEATHER_REPLY_INSTRUCTIONS},
                *cleaned_history, {"role": "user", "content": question}]
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=BASE_URL,
        timeout=60.0,
        max_retries=0,
    )

    clean_retry = False
    last_candidate = ""

    async def think(observations: list[Observation], feedback: str) -> str | ToolAction:
        nonlocal messages, clean_retry, last_candidate
        if feedback:
            messages.extend(({"role": "assistant", "content": last_candidate},
                             {"role": "user", "content": f"回答验收未通过：{feedback}"}))
        for _ in range(2):
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    stream=False,
                    tools=[WEATHER_TOOL],
                    tool_choice="auto" if not observations or feedback.startswith("请重新查询") else "none",
                    extra_body={"thinking": {"type": "disabled"}},
                ),
                timeout=60.0,
            )
            if not response.choices:
                raise ValueError("DeepSeek returned no answer.")
            choice = response.choices[0]
            content = choice.message.content
            if choice.finish_reason == "stop" and content and not _is_unparsed_tool_call(content):
                last_candidate = content
                return content
            if choice.finish_reason == "tool_calls":
                calls = choice.message.tool_calls or []
                if len(calls) != 1:
                    raise ValueError("DeepSeek returned an unsupported tool call.")
                call = calls[0]
                arguments = _tool_arguments(call)
                return ToolAction(call.function.name, arguments, call.id, call.function.arguments)
            elif _is_unparsed_tool_call(content):
                arguments = _parse_unparsed_weather_call(content)
                if observations and not clean_retry:
                    clean_retry = True
                    data = _weather_data(observations[-1].result)
                    messages = [{"role": "system", "content": WEATHER_REPLY_INSTRUCTIONS},
                                *cleaned_history, {"role": "user", "content": question},
                                {"role": "user", "content": f"天气查询结果：{data}"}]
                    continue
                return ToolAction("get_weather", arguments)
            else:
                raise ValueError("DeepSeek did not return a complete text answer.")
        raise ValueError("DeepSeek repeated invalid tool markup.")

    async def act(action: ToolAction) -> Observation:
        nonlocal messages
        if action.name != "get_weather":
            raise ValueError("DeepSeek returned an unsupported tool call.")
        try:
            result = await get_weather(action.arguments["location"], action.arguments["date"])
        except WeatherUnavailable as error:
            raise ValueError(f"Weather lookup failed: {error}") from error
        data = _weather_data(result)
        if action.call_id is not None:
            messages.extend((
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": action.call_id, "type": "function", "function": {
                        "name": action.name, "arguments": action.raw_arguments,
                    },
                }]},
                {"role": "tool", "tool_call_id": action.call_id, "content": data},
            ))
        else:
            # Raw DSML has no tool_call_id, so resume with a fresh text context.
            messages = [{"role": "system", "content": WEATHER_REPLY_INSTRUCTIONS},
                        *cleaned_history, {"role": "user", "content": question},
                        {"role": "user", "content": f"天气查询结果：{data}"}]
        return Observation(action, result)

    def verify(answer: str, observations: list[Observation]) -> Verdict:
        if must_check_weather or observations:
            return verify_weather(answer, observations, question=question,
                                  date_unspecified=date_unspecified)
        return Verdict("completed", answer=answer)

    async with client:
        answer = await run_loop(think, act, verify, max_steps=6)
    if history is not None:
        history.extend(({"role": "user", "content": question}, {"role": "assistant", "content": answer}))
    return answer
