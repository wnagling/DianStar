# DeepSeek SDK 适配层：创建客户端、组织消息并解析文字或工具调用响应。

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from xml.etree import ElementTree

from openai import AsyncOpenAI

from .config import ENV_FILE, PROJECT_ROOT, read_api_key
from .loop import Observation, ToolAction


MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"


def create_client(env_file: Path = ENV_FILE, *, key_id: str = "primary") -> AsyncOpenAI:
    """Load the local credential without placing it in model messages."""
    if not env_file.is_file():
        raise ValueError("尚未配置 API Key，请在 /config 中设置。")
    api_key = read_api_key(env_file, key_id=key_id)
    if not api_key:
        raise ValueError("尚未配置 API Key，请在 /config 中设置。"
                         if key_id == "primary" else "所选 API Key 尚未配置。")
    return AsyncOpenAI(api_key=api_key, base_url=BASE_URL, timeout=60.0, max_retries=0)


def _is_unparsed_tool_call(content: str | None) -> bool:
    """Recognize leaked DeepSeek tool markup without treating it as an answer."""
    return bool(content and "DSML" in content and "invoke" in content)


def _parse_unparsed_tool_calls(content: str, tool_schemas: list[dict]) -> list[ToolAction]:
    """Parse complete DSML calls against the tool schemas supplied to this session."""
    if len(content) > 8192:
        raise ValueError("DeepSeek returned invalid tool markup.")
    normalized = re.sub(r"<(/?)[|｜]{1,2}DSML[|｜]{1,2}\s*", r"<\1", content.strip())
    try:
        root = ElementTree.fromstring(normalized)
    except ElementTree.ParseError as error:
        raise ValueError("DeepSeek returned invalid tool markup.") from error
    if root.tag in {"calls", "tool_calls", "function_calls"}:
        if root.text and root.text.strip():
            raise ValueError("DeepSeek returned invalid tool markup.")
        invokes = list(root)
    else:
        invokes = [root]
    if not invokes or len(invokes) > 8:
        raise ValueError("DeepSeek returned invalid tool markup.")
    schemas = {tool["function"]["name"]: tool["function"] for tool in tool_schemas}
    actions = []
    for invoke in invokes:
        if invoke.tag != "invoke" or set(invoke.attrib) != {"name"} or invoke.attrib["name"] not in schemas:
            raise ValueError("DeepSeek returned invalid tool markup.")
        if (invoke.text and invoke.text.strip()) or (invoke.tail and invoke.tail.strip()):
            raise ValueError("DeepSeek returned invalid tool markup.")
        schema = schemas[invoke.attrib["name"]]
        required = set(schema["parameters"]["required"])
        allowed = set(schema["parameters"]["properties"])
        arguments = {}
        for parameter in invoke:
            if (
                parameter.tag != "parameter"
                or parameter.attrib.get("string") != "true"
                or set(parameter.attrib) != {"name", "string"}
                or parameter.attrib["name"] not in allowed
                or parameter.attrib["name"] in arguments
                or list(parameter)
                or (parameter.tail and parameter.tail.strip())
            ):
                raise ValueError("DeepSeek returned invalid tool markup.")
            arguments[parameter.attrib["name"]] = parameter.text or ""
        if not required <= set(arguments) or not all(value.strip() for value in arguments.values()):
            raise ValueError("DeepSeek returned invalid tool markup.")
        actions.append(ToolAction(invoke.attrib["name"], arguments))
    return actions


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


def usable_history(history: list[dict[str, object]] | None) -> list[dict[str, object]]:
    """Skip old turns whose assistant answer leaked tool markup."""
    cleaned = []
    for message in history or []:
        if message["role"] == "assistant" and isinstance(message.get("content"), str) and _is_unparsed_tool_call(message["content"]):
            if cleaned and cleaned[-1]["role"] == "user":
                cleaned.pop()
            continue
        cleaned.append(message)
    return cleaned


class DeepSeekSession:
    """Translate loop decisions and observations to DeepSeek chat messages."""

    def __init__(
        self, client: AsyncOpenAI, question: str, history: list[dict[str, object]],
        instructions: str, tools: list[dict],
        on_stream: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        model: str = MODEL,
    ) -> None:
        self.client = client
        self.question = question
        self.history = history
        self.instructions = instructions
        self.tools = tools
        self.on_stream = on_stream
        self.on_thinking = on_thinking
        self.model = model
        self.messages = [
            {"role": "system", "content": instructions},
            *history,
            {"role": "user", "content": question},
        ]
        self.clean_retry = False
        self.last_observation_text = ""
        self.pending_reasoning_content: str | None = None
        self.pending_content: str | None = None
        self.pending_calls: list[ToolAction] = []
        self.observed_call_ids: set[str] = set()
        self.raw_results: list[str] = []

    def _fresh_tool_result_context(self) -> list[dict]:
        messages = [
            {"role": "system", "content": self.instructions},
            *self.history,
            {"role": "user", "content": self.question},
        ]
        if self.pending_reasoning_content is not None:
            messages.append({"role": "assistant", "content": "",
                             "reasoning_content": self.pending_reasoning_content})
        messages.append({"role": "user", "content": f"工具查询结果：{self.last_observation_text}"})
        return messages

    async def think(self, observations: list[Observation]) -> str | ToolAction | list[ToolAction]:
        force_text_retry = False
        for _ in range(2):
            request = dict(
                model=self.model,
                messages=[message.copy() for message in self.messages],
                tools=self.tools,
                tool_choice="none" if force_text_retry else "auto",
                extra_body={"thinking": {"type": "enabled"}},
            )
            if self.on_stream is None:
                response = await asyncio.wait_for(
                    self.client.chat.completions.create(**request, stream=False), timeout=60.0,
                )
            else:
                response = await self._stream_completion(request)
            if not response.choices:
                raise ValueError("DeepSeek returned no answer.")
            choice = response.choices[0]
            content = choice.message.content
            reasoning_content = getattr(choice.message, "reasoning_content", None)
            if choice.finish_reason == "stop" and content and not _is_unparsed_tool_call(content):
                assistant_message = {"role": "assistant", "content": content}
                if reasoning_content is not None:
                    assistant_message["reasoning_content"] = reasoning_content
                self.messages.append(assistant_message)
                return content
            if choice.finish_reason == "tool_calls":
                calls = choice.message.tool_calls or []
                try:
                    if not calls or len(calls) > 8:
                        raise ValueError("DeepSeek returned an unsupported tool call.")
                    actions = [ToolAction(call.function.name, _tool_arguments(call, self.tools),
                                          call.id, call.function.arguments) for call in calls]
                    if len({action.call_id for action in actions}) != len(actions):
                        raise ValueError("DeepSeek returned duplicate tool call IDs.")
                except ValueError as error:
                    if force_text_retry:
                        raise ValueError("模型连续返回无法执行的工具调用。") from error
                    guidance = (
                        "刚才的工具调用无法执行。请重新判断：能直接回答的问题请直接回答；"
                        "需要外部数据时只能调用提供的工具。"
                    )
                    assistant_message = {"role": "assistant", "content": content or ""}
                    if reasoning_content is not None:
                        assistant_message["reasoning_content"] = reasoning_content
                    self.messages.append(assistant_message)
                    self.messages.append({"role": "user", "content":
                                          guidance})
                    force_text_retry = True
                    continue
                self.pending_calls = actions
                self.pending_content = content
                self.pending_reasoning_content = reasoning_content
                self.observed_call_ids = set()
                return actions if len(actions) > 1 else actions[0]
            if _is_unparsed_tool_call(content):
                if force_text_retry:
                    raise ValueError("模型连续返回无法执行的工具调用。")
                actions = _parse_unparsed_tool_calls(content, self.tools)
                self.pending_reasoning_content = reasoning_content
                if observations and not self.clean_retry:
                    self.clean_retry = True
                    self.messages = self._fresh_tool_result_context()
                    continue
                self.pending_calls = actions
                self.raw_results = []
                return actions if len(actions) > 1 else actions[0]
            raise ValueError("DeepSeek did not return a complete text answer.")
        raise ValueError("DeepSeek repeated invalid tool markup.")

    async def _stream_completion(self, request: dict) -> SimpleNamespace:
        """Collect a complete decision while showing only provisional answer text."""
        self.on_stream("")
        if self.on_thinking is not None:
            self.on_thinking("")
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        async with asyncio.timeout(60.0):
            stream = await self.client.chat.completions.create(**request, stream=True)
            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta is not None:
                        reasoning = getattr(delta, "reasoning_content", None)
                        if reasoning:
                            reasoning_parts.append(reasoning)
                            if self.on_thinking is not None:
                                self.on_thinking("".join(reasoning_parts))
                        content = getattr(delta, "content", None)
                        if content:
                            content_parts.append(content)
                            visible = "".join(content_parts)
                            if not visible.lstrip().startswith("<") and not calls:
                                self.on_stream(visible)
                        for call in getattr(delta, "tool_calls", None) or []:
                            target = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                            if call.id:
                                target["id"] = call.id
                            function = getattr(call, "function", None)
                            if function is not None:
                                if function.name:
                                    target["name"] = function.name
                                if function.arguments:
                                    target["arguments"] += function.arguments
                            self.on_stream("")
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
            finally:
                await stream.close()
        content = "".join(content_parts) or None
        if finish_reason != "stop" or _is_unparsed_tool_call(content):
            self.on_stream("")
        tool_calls = [SimpleNamespace(
            id=call["id"], function=SimpleNamespace(name=call["name"], arguments=call["arguments"]),
        ) for _, call in sorted(calls.items())]
        message = SimpleNamespace(
            content=content, reasoning_content="".join(reasoning_parts) or None,
            tool_calls=tool_calls,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])

    def observe(self, action: ToolAction, result_text: str) -> None:
        """Record a tool result without exposing fields withheld by the caller."""
        self.last_observation_text = result_text
        if action.call_id is None:
            # Raw DSML has no tool_call_id, so resume with a fresh text context.
            self.raw_results.append(result_text)
            if len(self.raw_results) == len(self.pending_calls):
                self.last_observation_text = (self.raw_results[0] if len(self.raw_results) == 1
                                              else "[" + ", ".join(self.raw_results) + "]")
                self.messages = self._fresh_tool_result_context()
                self.pending_calls = []
                self.raw_results = []
            return
        if not self.observed_call_ids:
            assistant_message = {"role": "assistant", "content": self.pending_content, "tool_calls": [{
                "id": pending.call_id, "type": "function", "function": {
                    "name": pending.name, "arguments": pending.raw_arguments,
                },
            } for pending in self.pending_calls]}
            if self.pending_reasoning_content is not None:
                assistant_message["reasoning_content"] = self.pending_reasoning_content
            self.messages.append(assistant_message)
        self.messages.append({"role": "tool", "tool_call_id": action.call_id, "content": result_text})
        self.observed_call_ids.add(action.call_id)
        if len(self.observed_call_ids) == len(self.pending_calls):
            self.pending_calls = []
            self.observed_call_ids = set()
