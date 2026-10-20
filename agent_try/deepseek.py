# DeepSeek SDK 适配层：创建客户端、组织消息并解析文字或工具调用响应。

import asyncio
import json
import re
from pathlib import Path
from xml.etree import ElementTree

from dotenv import dotenv_values
from openai import AsyncOpenAI

from .loop import Observation, ToolAction, Verdict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"


def create_client(env_file: Path = ENV_FILE) -> AsyncOpenAI:
    """Load the local credential without placing it in model messages."""
    if not env_file.is_file():
        raise ValueError(".env is missing from the project root.")
    api_key = (dotenv_values(env_file).get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("Set DEEPSEEK_API_KEY in the project .env file.")
    return AsyncOpenAI(api_key=api_key, base_url=BASE_URL, timeout=60.0, max_retries=0)


def _is_unparsed_tool_call(content: str | None) -> bool:
    """Recognize leaked DeepSeek tool markup without treating it as an answer."""
    return bool(content and "DSML" in content and "invoke" in content)


def _parse_unparsed_weather_call(content: str) -> dict[str, str]:
    """Accept only one complete DSML get_weather call with two string arguments."""
    if len(content) > 2048:
        raise ValueError("DeepSeek returned invalid tool markup.")
    normalized = re.sub(r"<(/?)[|｜]{1,2}DSML[|｜]{1,2}\s*", r"<\1", content.strip())
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


def _tool_arguments(call, tool_schemas: list[dict]) -> dict[str, str]:
    schemas = {tool["function"]["name"]: tool["function"] for tool in tool_schemas}
    schema = schemas.get(call.function.name)
    if schema is None:
        raise ValueError("DeepSeek returned an unsupported tool call.")
    try:
        arguments = json.loads(call.function.arguments)
    except (ValueError, TypeError) as error:
        raise ValueError("DeepSeek returned invalid tool arguments.") from error
    required = set(schema["parameters"]["required"])
    allowed = set(schema["parameters"]["properties"])
    if not isinstance(arguments, dict) or not required <= set(arguments) <= allowed:
        raise ValueError("DeepSeek returned invalid tool arguments.")
    if not all(isinstance(value, str) and value.strip() for value in arguments.values()):
        raise ValueError("DeepSeek returned invalid tool arguments.")
    return arguments


def usable_history(history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Skip old turns whose assistant answer leaked tool markup."""
    cleaned = []
    for message in history or []:
        if message["role"] == "assistant" and _is_unparsed_tool_call(message["content"]):
            if cleaned and cleaned[-1]["role"] == "user":
                cleaned.pop()
            continue
        cleaned.append(message)
    return cleaned


class DeepSeekSession:
    """Translate loop decisions and observations to DeepSeek chat messages."""

    def __init__(
        self, client: AsyncOpenAI, question: str, history: list[dict[str, str]],
        instructions: str, tools: list[dict],
    ) -> None:
        self.client = client
        self.question = question
        self.history = history
        self.instructions = instructions
        self.tools = tools
        self.messages = [
            {"role": "system", "content": instructions},
            *history,
            {"role": "user", "content": question},
        ]
        self.clean_retry = False
        self.last_candidate = ""
        self.last_observation_text = ""

    def _fresh_tool_result_context(self) -> list[dict]:
        return [
            {"role": "system", "content": self.instructions},
            *self.history,
            {"role": "user", "content": self.question},
            {"role": "user", "content": f"工具查询结果：{self.last_observation_text}"},
        ]

    async def think(self, observations: list[Observation], feedback: Verdict | None) -> str | ToolAction:
        if feedback is not None:
            if self.last_candidate:
                self.messages.append({"role": "assistant", "content": self.last_candidate})
            self.messages.append({"role": "user", "content": f"回答验收未通过：{feedback.feedback}"})
        for _ in range(2):
            response = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=MODEL,
                    messages=self.messages,
                    stream=False,
                    tools=self.tools,
                    tool_choice="auto" if not observations or (feedback and feedback.retry_tool) else "none",
                    extra_body={"thinking": {"type": "disabled"}},
                ),
                timeout=60.0,
            )
            if not response.choices:
                raise ValueError("DeepSeek returned no answer.")
            choice = response.choices[0]
            content = choice.message.content
            if choice.finish_reason == "stop" and content and not _is_unparsed_tool_call(content):
                self.last_candidate = content
                return content
            if choice.finish_reason == "tool_calls":
                calls = choice.message.tool_calls or []
                if len(calls) != 1:
                    raise ValueError("DeepSeek returned an unsupported tool call.")
                call = calls[0]
                arguments = _tool_arguments(call, self.tools)
                return ToolAction(call.function.name, arguments, call.id, call.function.arguments)
            if _is_unparsed_tool_call(content):
                arguments = _parse_unparsed_weather_call(content)
                if observations and not self.clean_retry:
                    self.clean_retry = True
                    self.messages = self._fresh_tool_result_context()
                    continue
                return ToolAction("get_weather", arguments)
            raise ValueError("DeepSeek did not return a complete text answer.")
        raise ValueError("DeepSeek repeated invalid tool markup.")

    def observe(self, action: ToolAction, result_text: str) -> None:
        """Record a tool result without exposing fields withheld by the caller."""
        self.last_candidate = ""
        self.last_observation_text = result_text
        if action.call_id is None:
            # Raw DSML has no tool_call_id, so resume with a fresh text context.
            self.messages = self._fresh_tool_result_context()
            return
        self.messages.extend((
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": action.call_id, "type": "function", "function": {
                    "name": action.name, "arguments": action.raw_arguments,
                },
            }]},
            {"role": "tool", "tool_call_id": action.call_id, "content": result_text},
        ))
