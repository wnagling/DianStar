"""Offline checks for the first DeepSeek SDK integration."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_try.deepseek import ask_deepseek


class DeepSeekTests(unittest.TestCase):
    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""})
    @patch("agent_try.deepseek.OpenAI")
    def test_missing_key_stops_before_creating_client(self, client_class):
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            ask_deepseek("Hello")
        client_class.assert_not_called()

    @patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_MODEL": "deepseek-flash",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        },
    )
    @patch("agent_try.deepseek.OpenAI")
    def test_sends_one_question_and_returns_text(self, client_class):
        client = client_class.return_value
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="Hello back"),
                )
            ]
        )

        self.assertEqual(ask_deepseek("Hello"), "Hello back")
        client_class.assert_called_once_with(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            timeout=60.0,
            max_retries=1,
        )
        client.chat.completions.create.assert_called_once_with(
            model="deepseek-flash",
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=512,
            stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )


if __name__ == "__main__":
    unittest.main()
