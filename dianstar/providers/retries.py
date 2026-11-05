"""Retry model transport only; tool execution is never inside this boundary."""
import asyncio

from openai import APIConnectionError, APIStatusError


class IncompleteResponseError(ConnectionError):
    pass


def retryable(error):
    return isinstance(error, (APIConnectionError, TimeoutError, IncompleteResponseError)) or (
        isinstance(error, APIStatusError) and error.status_code in {408, 409, 429, 500, 502, 503, 504})


async def model_request(operation, notify=None):
    for attempt in range(3):
        try:
            return await operation()
        except Exception as error:
            if not retryable(error) or attempt == 2:
                raise
            if notify:
                try:
                    notify({"attempt": attempt + 1, "maximum": 2,
                        "message": f"模型连接暂时中断，正在重试（{attempt + 1}/2）；已完成的工具操作不会自动重放。"})
                except Exception:
                    pass
            await asyncio.sleep(attempt + 1)
