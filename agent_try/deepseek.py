"""Send a question and optional conversation history to DeepSeek."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

from dotenv import dotenv_values
from openai import AsyncOpenAI

from .location import LocationUnavailable, get_location


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"
LOCATION_TOOL = {
    "type": "function",
    "function": {
        "name": "get_location",
        "description": "Get this device's current geographic latitude, longitude and accuracy. Use only when the user explicitly asks for their device location.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


async def ask_deepseek(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    env_file: Path = ENV_FILE,
    approve_location: Callable[[], bool] | None = None,
) -> str:
    """Ask DeepSeek, optionally executing one approved device-location tool call."""
    if not question.strip():
        raise ValueError("Please provide a question.")
    if not env_file.is_file():
        raise ValueError(".env is missing from the project root.")

    api_key = (dotenv_values(env_file).get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("Set DEEPSEEK_API_KEY in the project .env file.")

    messages = [*(history or []), {"role": "user", "content": question}]
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=BASE_URL,
        timeout=60.0,
        max_retries=0,
    )
    async def request(tool_choice: str):
        return await asyncio.wait_for(
            client.chat.completions.create(
                model=MODEL,
                messages=messages,
                stream=False,
                tools=[LOCATION_TOOL],
                tool_choice=tool_choice,
                extra_body={"thinking": {"type": "disabled"}},
            ),
            timeout=60.0,
        )

    async with client:
        response = await request("auto")
        if not response.choices:
            raise ValueError("DeepSeek returned no answer.")
        choice = response.choices[0]
        if choice.finish_reason == "tool_calls":
            calls = choice.message.tool_calls or []
            if not calls:
                raise ValueError("DeepSeek returned no tool call.")
            messages.append(choice.message)
            location_used = False
            for call in calls:
                result: dict[str, object]
                try:
                    arguments = json.loads(call.function.arguments)
                except (ValueError, TypeError):
                    arguments = None
                if call.function.name != "get_location" or arguments != {}:
                    result = {"error": "Unknown tool or invalid arguments."}
                elif location_used:
                    result = {"error": "Location can be read only once per question."}
                elif approve_location is None or not approve_location():
                    location_used = True
                    result = {"error": "The user did not approve sharing the device location."}
                else:
                    location_used = True
                    try:
                        result = (await get_location()).as_tool_result()
                    except LocationUnavailable as error:
                        result = {"error": str(error)}
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
            response = await request("none")
            if not response.choices:
                raise ValueError("DeepSeek returned no answer.")
            choice = response.choices[0]
    if choice.finish_reason != "stop" or not choice.message.content:
        raise ValueError("DeepSeek did not return a complete text answer.")
    answer = choice.message.content
    if history is not None:
        history.extend(({"role": "user", "content": question}, {"role": "assistant", "content": answer}))
    return answer
