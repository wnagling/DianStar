"""Send one question to DeepSeek using its OpenAI-compatible API."""

import asyncio
from pathlib import Path

from dotenv import dotenv_values
from openai import AsyncOpenAI


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"


async def ask_deepseek(question: str, *, env_file: Path = ENV_FILE) -> str:
    """Read the local key, make one request, and return the answer text."""
    if not question.strip():
        raise ValueError("Please provide a question.")
    if not env_file.is_file():
        raise ValueError(".env is missing from the project root.")

    api_key = (dotenv_values(env_file).get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("Set DEEPSEEK_API_KEY in the project .env file.")

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
                messages=[{"role": "user", "content": question}],
                stream=False,
            ),
            timeout=60.0,
        )
    if not response.choices:
        raise ValueError("DeepSeek returned no answer.")

    choice = response.choices[0]
    if choice.finish_reason != "stop" or not choice.message.content:
        raise ValueError("DeepSeek did not return a complete text answer.")
    return choice.message.content
