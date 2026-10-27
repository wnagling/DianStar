"""Acceptance checks for shared turn persistence and headless operation."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dianstar.app.chat_service import ask_and_save
from dianstar.app.commands import AmbiguousConversation, resolve_conversation
from dianstar.sessions.conversations import Conversation


class ChatServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_preserves_tool_transcript_and_all_thinking_parts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            conversation = Conversation.create(directory)
            seen = []

            async def runner(question, *, history, on_thinking):
                on_thinking("查询前")
                on_thinking("查询前思考")
                on_thinking("")
                on_thinking("查询后思考")
                history.extend([
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": "", "reasoning_content": "查询前思考",
                     "tool_calls": [{"id": "call-1", "type": "function", "function": {
                         "name": "calculate_number", "arguments": '{"expression":"1+1"}'}}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": '{"result":"2"}'},
                    {"role": "assistant", "content": "2", "reasoning_content": "查询后思考"},
                ])
                return "2"

            self.assertEqual(await ask_and_save("1+1", conversation, runner=runner,
                                                on_thinking=seen.append), "2")
            restored = Conversation.load(conversation.id, directory)
            self.assertEqual(len(restored.messages), 2)
            self.assertEqual(len(restored.model_messages), 4)
            self.assertEqual(restored.messages[-1]["reasoning_content"], "查询前思考\n\n查询后思考")
            self.assertEqual(restored.model_messages[2]["tool_call_id"], "call-1")
            self.assertEqual(seen, ["查询前", "查询前思考", "", "查询后思考"])

    async def test_failure_and_cancellation_do_not_persist_partial_turn(self):
        for failure in (ValueError("failed"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                conversation = Conversation.create(directory)
                conversation.add_exchange("已有问题", "已有回答")
                path = directory / f"{conversation.id}.json"
                before = path.read_bytes()

                async def runner(question, *, history):
                    history.append({"role": "user", "content": question})
                    raise failure

                with self.assertRaises(type(failure)):
                    await ask_and_save("新问题", conversation, runner=runner)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(len(conversation.model_messages), 2)

    async def test_duplicate_name_does_not_choose_an_arbitrary_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for _ in range(2):
                Conversation.create(directory).rename("同名")
            with self.assertRaises(AmbiguousConversation):
                resolve_conversation("同名", directory)


class HeadlessAcceptance(unittest.TestCase):
    def test_plain_chat_and_tool_loop_run_when_textual_import_is_blocked(self):
        source = '''
import importlib.abc, sys
class NoTextual(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "textual" or fullname.startswith("textual."):
            raise ImportError("Textual intentionally unavailable")
sys.meta_path.insert(0, NoTextual())
import asyncio, tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
from dianstar.ui.cli import main
from dianstar.sessions.conversations import Conversation
from dianstar.core.loop import run_loop
from dianstar.core.types import Observation, ToolAction
async def think(observations):
    return "2" if observations else ToolAction("calculate_number", {"expression":"1+1"})
async def act(action):
    return Observation(action, {"result":"2"})
assert asyncio.run(run_loop(think, act)) == "2"
with tempfile.TemporaryDirectory() as temporary:
    conversation = Conversation.create(Path(temporary))
    with patch.object(sys, "argv", ["dianstar", "--plain"]), \
         patch("builtins.input", side_effect=["1+1", "/exit"]), \
         patch("dianstar.ui.cli.Conversation.create", return_value=conversation), \
         patch("dianstar.ui.cli.run_turn", new_callable=AsyncMock, return_value="2"):
        assert main() == 0
    assert Conversation.load(conversation.id, Path(temporary)).messages[-1]["content"] == "2"
assert not any(name == "textual" or name.startswith("textual.") for name in sys.modules)
'''
        result = subprocess.run([sys.executable, "-c", source], capture_output=True,
                                text=True, encoding="utf-8", timeout=30,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
