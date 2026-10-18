"""Offline checks for the first DeepSeek SDK call."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import Request
from openai import APIConnectionError

from agent_try.__main__ import main
from agent_try.deepseek import ask_deepseek


class DeepSeekTests(unittest.IsolatedAsyncioTestCase):
    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_empty_local_key_stops_before_network(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                await ask_deepseek("Hello", env_file=env_file)

        client_class.assert_not_called()

    @patch("agent_try.deepseek.AsyncOpenAI")
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

            self.assertEqual(await ask_deepseek("Hello", env_file=env_file), "Hello back")

        client_class.assert_called_once_with(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            timeout=60.0,
            max_retries=0,
        )
        client.chat.completions.create.assert_awaited_once_with(
            model="deepseek-flash",
            messages=[{"role": "user", "content": "Hello"}],
            stream=False,
        )

    def test_connection_error_returns_failure_without_request_details(self):
        error = APIConnectionError(
            message="request contained a sensitive value",
            request=Request("POST", "https://api.deepseek.com/chat/completions"),
        )
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["agent_try", "Hello"]),
            patch("agent_try.__main__.ask_deepseek", side_effect=error),
            redirect_stderr(stderr),
        ):
            result = main()

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "Error: Could not connect to DeepSeek.\n")


if __name__ == "__main__":
    unittest.main()
