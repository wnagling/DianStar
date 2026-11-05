"""Retry model transport, never replay tools or recover old authorization."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIStatusError

from dianstar.providers.retries import model_request
from dianstar.providers.deepseek import DeepSeekSession
from dianstar.sessions.conversations import Conversation
from dianstar.app import commands


class ModelRetryTests(unittest.IsolatedAsyncioTestCase):
    @patch("dianstar.providers.retries.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_are_bounded_and_cancelable(self, sleep):
        operation = AsyncMock(side_effect=[TimeoutError(), TimeoutError(), "ok"])
        events = []
        self.assertEqual(await model_request(operation, events.append), "ok")
        self.assertEqual(operation.await_count, 3)
        self.assertEqual(len(events), 2)
        operation.side_effect = TimeoutError
        operation.reset_mock()
        with self.assertRaises(TimeoutError):
            await model_request(operation)
        self.assertEqual(operation.await_count, 3)
        operation.side_effect = asyncio.CancelledError
        operation.reset_mock()
        with self.assertRaises(asyncio.CancelledError):
            await model_request(operation)
        self.assertEqual(operation.await_count, 1)

    @patch("dianstar.providers.retries.asyncio.sleep", new_callable=AsyncMock)
    async def test_401_is_not_retried_and_503_is(self, sleep):
        for status, attempts in ((401, 1), (403, 1), (429, 3), (503, 3)):
            error = APIStatusError("fixture", response=httpx.Response(status,
                request=httpx.Request("POST", "https://fixture.invalid")), body=None)
            operation = AsyncMock(side_effect=error)
            with self.assertRaises(APIStatusError):
                await model_request(operation)
            self.assertEqual(operation.await_count, attempts)

    @patch("dianstar.providers.retries.asyncio.sleep", new_callable=AsyncMock)
    async def test_partial_tool_stream_is_discarded_before_retry(self, sleep):
        from tests.test_deepseek import FakeStream
        from dianstar.tools.local.number_calculator import NUMBER_TOOL
        call = SimpleNamespace(index=0, id="partial", function=SimpleNamespace(name="calculate_number", arguments='{"expression":'))
        incomplete = FakeStream([SimpleNamespace(choices=[SimpleNamespace(finish_reason=None,
            delta=SimpleNamespace(tool_calls=[call], content=None, reasoning_content="fixture partial"))])])
        complete = FakeStream([SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            delta=SimpleNamespace(tool_calls=[], content="完整回答", reasoning_content=None))])])
        create = AsyncMock(side_effect=[incomplete, complete])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        output = []
        session = DeepSeekSession(client, "fixture question", [], "fixture rules", [NUMBER_TOOL], on_stream=output.append)
        self.assertEqual(await session.think([]), "完整回答")
        self.assertEqual(session.pending_calls, [])
        self.assertTrue(incomplete.closed and complete.closed)
        self.assertEqual(create.await_count, 2)
        self.assertFalse(any(message.get("tool_calls") for message in session.messages))


class RetryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    @patch("dianstar.providers.retries.asyncio.sleep", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_failed_turn_preserves_tool_pairs_for_manual_retry(self, factory, sleep):
        from dianstar.app.chat_service import ask_and_save
        from dianstar.config.credentials import save_api_key
        from tests.test_permissions import call
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict("os.environ", DIANSTAR_FILE_STATE_ROOT=str(root / "state")):
                env = root / ".env"
                save_api_key("fixture-fake-key", env)
                conversation = Conversation.create(root / "chats")
                create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                    call("calculate_number", {"expression": "1+1"}), TimeoutError(), TimeoutError(), TimeoutError()])
                with self.assertRaises(TimeoutError):
                    await ask_and_save("计算1+1再说明结果", conversation, env_file=env, project_directory=root)
                restored = Conversation.load(conversation.id, root / "chats")
                self.assertTrue(restored.messages[-1]["interrupted"])
                self.assertEqual(len([message for message in restored.model_messages if message["role"] == "tool"]), 1)
                self.assertEqual(create.await_count, 4)
                retry = commands.retry_question(restored)
                self.assertIn("计算1+1", retry)
                self.assertIn("不要重复", retry)
                self.assertIn("旧审批不恢复", retry)

    @patch("dianstar.providers.retries.asyncio.sleep", new_callable=AsyncMock)
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_automatic_retry_preserves_prior_tool_result_without_reexecution(self, factory, sleep):
        from dianstar.app.runtime import run_turn
        from dianstar.config.credentials import save_api_key
        from tests.test_permissions import call, reply
        from dianstar.tools.local.number_calculator import calculate_number
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict("os.environ", DIANSTAR_FILE_STATE_ROOT=str(root / "state")):
                env = root / ".env"
                save_api_key("fixture-fake-key", env)
                create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                    call("calculate_number", {"expression": "1+1"}), TimeoutError(), reply("结果为2")])
                with patch("dianstar.tools.local.registry.calculate_number", wraps=calculate_number) as calculate:
                    answer = await run_turn("计算1+1", env_file=env, project_directory=root)
                self.assertIn("2", answer)
                calculate.assert_called_once()
                self.assertEqual(create.await_count, 3)

    async def test_tui_retry_command_and_no_retry_after_success(self):
        from dianstar.ui.tui.app import DianStarApp
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict("os.environ", DIANSTAR_FILE_STATE_ROOT=str(root / "state")):
                chat = Conversation.create(root / "chats")
                with self.assertRaises(ValueError):
                    commands.retry_question(chat)
                chat.add_exchange("完成fixture项目", "请求中断", interrupted=True)
                with patch("dianstar.ui.tui.app.run_turn", new_callable=AsyncMock, return_value="已核对现场") as runner:
                    app = DianStarApp(chat, config_file=root / "config.json", env_file=root / "unused.env")
                    async with app.run_test(size=(120, 30)) as pilot:
                        await app._command("/retry")
                        await pilot.pause(.4)
                        await app.workers.wait_for_complete()
                        self.assertIn("完成fixture项目", runner.await_args.args[0])
                        self.assertFalse(app.busy)
                with self.assertRaises(ValueError):
                    commands.retry_question(chat)
