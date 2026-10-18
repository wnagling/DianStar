"""Offline checks for DeepSeek questions and interactive chat."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import Request
from openai import APIConnectionError

from agent_try.__main__ import main
from agent_try.conversations import Conversation
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

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_follow_up_sends_prior_successful_exchange(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            client = client_class.return_value
            client.chat.completions.create = AsyncMock(side_effect=[
                SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="First answer")
                )]),
                SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="Second answer")
                )]),
            ])
            history = []

            await ask_deepseek("First question", history=history, env_file=env_file)
            await ask_deepseek("Follow up", history=history, env_file=env_file)

        self.assertEqual(client.chat.completions.create.await_args_list[1].kwargs["messages"], [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow up"},
        ])
        self.assertEqual(history[-1], {"role": "assistant", "content": "Second answer"})

    @patch("agent_try.deepseek.AsyncOpenAI")
    async def test_failed_turn_does_not_enter_history(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            client = client_class.return_value
            client.chat.completions.create = AsyncMock(side_effect=[
                SimpleNamespace(choices=[]),
                SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="Recovered")
                )]),
            ])
            history = []

            with self.assertRaisesRegex(ValueError, "no answer"):
                await ask_deepseek("Failed question", history=history, env_file=env_file)
            self.assertEqual(history, [])
            await ask_deepseek("Retry", history=history, env_file=env_file)

        self.assertEqual(client.chat.completions.create.await_args_list[1].kwargs["messages"], [
            {"role": "user", "content": "Retry"},
        ])

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

    def test_no_argument_enters_chat_and_exit_stops(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            with (
                patch.object(sys, "argv", ["agent_try"]),
                patch("builtins.input", side_effect=["Hello", "/exit"]),
                patch("agent_try.__main__.Conversation.create", return_value=conversation),
                patch("agent_try.__main__.ask_deepseek", new_callable=AsyncMock, return_value="Hi") as ask,
                redirect_stdout(stdout),
            ):
                result = main()

            restored = Conversation.load(conversation.id, Path(temporary))

        self.assertEqual(result, 0)
        ask.assert_awaited_once()
        self.assertEqual(ask.await_args.args, ("Hello",))
        self.assertIn("DeepSeek> Hi", stdout.getvalue())
        self.assertEqual(restored.messages[-1], {"role": "assistant", "content": "Hi"})

    def test_compress_keeps_record_but_clears_model_context_after_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            conversation = Conversation.create(directory)
            conversation.add_exchange("First", "Answer")
            conversation.compress()
            restored = Conversation.load(conversation.id, directory)

            self.assertEqual(len(restored.messages), 2)
            self.assertEqual(restored.context_messages(), [])
            restored.add_exchange("New", "Reply")
            restored_again = Conversation.load(conversation.id, directory)
            self.assertEqual(restored_again.context_messages(), [
                {"role": "user", "content": "New"},
                {"role": "assistant", "content": "Reply"},
            ])

    def test_conversation_ids_are_unique_and_invalid_resume_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = Conversation.create(directory)
            second = Conversation.create(directory)
            self.assertNotEqual(first.id, second.id)
            with self.assertRaisesRegex(ValueError, "Invalid conversation ID"):
                Conversation.load("../.env", directory)

    def test_resume_compress_and_new_route_questions_to_separate_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old = Conversation.create(directory)
            old.add_exchange("Old question", "Old answer")
            new = Conversation.create(directory)
            with (
                patch.object(sys, "argv", ["agent_try", "--resume", old.id]),
                patch("builtins.input", side_effect=[
                    "/clear", "/compress", "After compress", "/new", "Fresh question", "/exit"
                ]),
                patch("agent_try.__main__.Conversation.load", return_value=old),
                patch("agent_try.__main__.Conversation.create", return_value=new),
                patch("agent_try.__main__.ask_deepseek", new_callable=AsyncMock,
                      side_effect=["After answer", "Fresh answer"]) as ask,
                redirect_stdout(io.StringIO()),
            ):
                result = main()

            old_restored = Conversation.load(old.id, directory)
            new_restored = Conversation.load(new.id, directory)

        self.assertEqual(result, 0)
        self.assertEqual(ask.await_count, 2)
        self.assertEqual(ask.await_args_list[0].kwargs["history"], [])
        self.assertEqual(ask.await_args_list[1].kwargs["history"], [])
        self.assertEqual(old_restored.context_messages()[-1]["content"], "After answer")
        self.assertEqual(new_restored.messages[0]["content"], "Fresh question")


if __name__ == "__main__":
    unittest.main()
