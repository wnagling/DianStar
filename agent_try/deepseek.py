"""One-shot DeepSeek chat through the OpenAI-compatible Python SDK."""

import os

from openai import OpenAI


DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"


def ask_deepseek(question: str) -> str:
    """Send one question and return the final text answer."""
    if not question.strip():
        raise ValueError("Question must not be empty.")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is not set.")

    model = os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip()
    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip()
    if not model or not base_url:
        raise ValueError("DEEPSEEK_MODEL and DEEPSEEK_BASE_URL must not be empty.")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=1)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": question}],
        max_tokens=512,
        stream=False,
        extra_body={"thinking": {"type": "disabled"}},
    )
    if not response.choices:
        raise RuntimeError("DeepSeek returned no answer choices.")

    choice = response.choices[0]
    if choice.finish_reason != "stop" or not choice.message.content:
        raise RuntimeError(f"DeepSeek did not return a complete text answer ({choice.finish_reason}).")
    return choice.message.content
