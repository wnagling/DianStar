"""Live tool cards, result ordering, redaction and conversation persistence."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dianstar.app.tool_events import ToolEvents, historical_activity
from dianstar.app.chat_service import ask_and_save
from dianstar.core.types import ToolAction, Observation
from dianstar.security.content_guard import ContentScanner
from dianstar.sessions.conversations import Conversation
from dianstar.sessions.tool_activity import ToolActivity, valid_activity
from dianstar.ui.tui.app import DianStarApp
from dianstar.ui.tui.widgets import ChatInput, ChatLog


class ToolEventTests(unittest.TestCase):
    def setUp(self):
        self.events = ToolEvents(ContentScanner(known=("fixture-private-key-value",)), "fixture-request", "fixture-conversation")

    def test_real_results_distinguish_failure_denial_cancellation_and_incomplete_audit(self):
        action = ToolAction("workspace_run", {"repository": "project", "argv": ["go", "run", "."]}, "run")
        for result, expected in (({"success": True, "exit_code": 0, "content": "Hello World\n"}, "success"),
                ({"success": False, "exit_code": 1, "content": "compile failed"}, "failed"),
                ({"error": "authorization_required"}, "not_executed"),
                ({"error": "tool_cancelled"}, "cancelled"),
                ({"success": True, "security_review": {"complete": False}}, "warning")):
            started = self.events.started(action)
            finished = self.events.completed(Observation(action, result))
            self.assertEqual(finished["status"], expected)
            self.assertEqual(started["event_id"], finished["event_id"])
            self.assertEqual(finished["conversation_id"], "fixture-conversation")
            if result.get("exit_code") == 0:
                self.assertIn("Hello World", finished["summary"])

    def test_parameters_and_output_are_masked_bounded_and_file_bodies_omitted(self):
        action = ToolAction("bash", {"repository": "project", "command": "echo fixture-private-key-value", "content": "must-not-copy-source"}, "fixture")
        start = self.events.started(action)
        finish = self.events.completed(Observation(action, {"success": True, "content": "fixture-private-key-value\n" + "x" * 10000}))
        self.assertNotIn("fixture-private-key-value", json.dumps([start, finish]))
        self.assertNotIn("must-not-copy-source", json.dumps(start))
        self.assertLessEqual(len(finish["output"]), 3000)
        malformed = self.events.started(ToolAction("bash", {}, "invalid"))
        self.assertEqual(malformed["title"], "调用工具")

    def test_same_call_ids_in_different_requests_are_separate_and_records_are_bounded(self):
        activity = ToolActivity()
        action = ToolAction("read", {"repository": "project", "path": "main.go"}, "same")
        first = self.events.started(action)
        other = ToolEvents(ContentScanner(), "other-request", "other-conversation").started(action)
        activity.consume(first)
        activity.consume(other)
        self.assertEqual(len(activity.records), 2)
        activity.interrupt()
        self.assertTrue(all(item["status"] == "interrupted" for item in activity.records))
        for index in range(300):
            activity.consume({"event_id": str(index), "phase": "completed", "tool": "fixture", "success": True})
        self.assertEqual(len(activity.records), 256)
        self.assertTrue(valid_activity(activity.snapshot()))
        invalid = activity.snapshot()
        invalid[0]["status"] = []
        self.assertFalse(valid_activity(invalid))

    def test_legacy_pairs_restore_by_turn_and_mask_multiline_values_before_json_encoding(self):
        secret = "fixture-firstline\nfixture-secondline"
        events = ToolEvents(ContentScanner(known=(secret,)), "fixture", None)
        self.assertNotIn("fixture-secondline", json.dumps(events.started(ToolAction("bash", {"command": "echo '" + secret + "'"}, "secret"))))
        messages = []
        for number in (1, 2):
            messages += [{"role": "user", "content": "same question"}, {"role": "assistant", "content": None,
                "tool_calls": [{"id": "same", "function": {"name": "calculate_number", "arguments": '{"expression":"1+1"}'}}]},
                {"role": "tool", "tool_call_id": "same", "content": json.dumps({"result": str(number)})}]
        restored = historical_activity(messages, ContentScanner())
        self.assertEqual(len(restored), 2)
        self.assertIn("1", restored[0][0]["summary"])
        self.assertIn("2", restored[1][0]["summary"])
        self.assertIsNone(restored[0][0]["duration_ms"])


class ToolActivityPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_turn_restores_cards_without_feeding_display_data_to_model(self):
        with tempfile.TemporaryDirectory() as directory:
            conversation = Conversation.create(Path(directory))
            presenter = ToolEvents(ContentScanner(), "fixture", conversation.id)
            action = ToolAction("read", {"repository": "project", "path": "main.go"}, "read")
            async def runner(question, *, history, on_tool):
                on_tool(presenter.started(action))
                on_tool(presenter.completed(Observation(action, {"type": "file"})))
                return "完成"
            seen = []
            await ask_and_save("读取文件", conversation, runner=runner, on_tool=seen.append)
            restored = Conversation.load(conversation.id, Path(directory))
            self.assertEqual(restored.messages[-1]["tool_activity"][0]["status"], "success")
            self.assertEqual(len(seen), 2)
            self.assertNotIn("tool_activity", json.dumps(restored.model_context_messages()))
            async def mismatched(question, *, history, on_tool):
                on_tool({**seen[0], "conversation_id": "other"})
                return "另一轮"
            await ask_and_save("测试", conversation, runner=mismatched, on_tool=seen.append)
            self.assertNotIn("tool_activity", conversation.messages[-1])

    async def test_damaged_display_metadata_is_rejected_without_executing_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            conversation = Conversation.create(Path(directory))
            conversation.add_exchange("question", "answer")
            path = Path(directory) / f"{conversation.id}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["messages"][-1]["tool_activity"] = [{"status": "success"}]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                Conversation.load(conversation.id, Path(directory))


class ToolPanelUITests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, directory):
        root = Path(directory)
        return DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")

    @staticmethod
    def display(app):
        return "\n".join(line.text for line in app.query_one("#chat", ChatLog).lines)

    async def click_action(self, pilot, app, action):
        chat = app.query_one("#chat", ChatLog)
        for y in range(chat.content_region.height):
            x = 0
            for segment in chat.render_line(y):
                if segment.style and segment.style.meta.get("@click") == action:
                    await pilot.click(chat, offset=(chat.content_region.x-chat.region.x+x, chat.content_region.y-chat.region.y+y))
                    return
                x += segment.cell_length
        self.fail("Clickable tool control not visible: " + action)

    async def test_parallel_progress_live_thinking_stream_toggle_and_history_restore(self):
        for size in ((90, 30), (52, 20)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                app = self.make_app(directory)
                ready, release = asyncio.Event(), asyncio.Event()
                presenter = ToolEvents(ContentScanner(), "fixture-turn", app.conversation.id)
                first = ToolAction("read", {"repository": "project", "path": "main.go"}, "first")
                second = ToolAction("workspace_run", {"repository": "project", "argv": ["go", "run", "."]}, "second")
                async def runner(question, *, history, on_tool, on_thinking, on_stream, **kwargs):
                    on_thinking("准备检查项目")
                    on_tool(presenter.started(first))
                    on_tool(presenter.started(second))
                    on_tool(presenter.completed(Observation(first, {"type": "file"})))
                    ready.set()
                    await release.wait()
                    on_thinking("文件已读取，核对运行结果")
                    on_stream("正在整理")
                    on_tool(presenter.completed(Observation(second, {"success": True, "exit_code": 0, "content": "Hello World", "duration_ms": 120})))
                    on_stream("任务完成")
                    return "任务完成"
                with patch("dianstar.ui.tui.app.run_turn", side_effect=runner):
                    async with app.run_test(size=size) as pilot:
                        app.query_one("#input", ChatInput).text = "读取并运行"
                        await pilot.press("enter")
                        await asyncio.wait_for(ready.wait(), 5)
                        await pilot.pause(.2)
                        block = next(item for item in app._chat_blocks if item["kind"] == "tools")
                        self.assertEqual([item["status"] for item in block["calls"]], ["success", "running"])
                        self.assertIn("工具执行", self.display(app))
                        self.assertIn("处理中", self.display(app))
                        self.assertNotIn('"argv"', self.display(app))
                        index = app._chat_blocks.index(block)
                        await self.click_action(pilot, app, f"app.toggle_tools({index})")
                        self.assertIn(index, app._expanded_tools)
                        app.action_toggle_tool_item(index, 1)
                        await pilot.pause()
                        self.assertIn("workspace_run", self.display(app))
                        self.assertIn("argv", self.display(app))
                        self.assertFalse(app._expanded_thinking)
                        release.set()
                        await app.workers.wait_for_complete()
                        await pilot.pause(.2)
                        self.assertIn("Hello World", self.display(app))
                        self.assertIn("任务完成", self.display(app))
                        self.assertIn("模型思考", self.display(app))
                        self.assertEqual([item["status"] for item in block["calls"]], ["success", "success"])
                        self.assertIsNone(app._active_tools)
                        restored = Conversation.load(app.conversation.id, app.conversation.directory)
                        self.assertEqual(len(restored.messages[-1]["tool_activity"]), 2)
                        app.conversation = restored
                        app._render_history()
                        self.assertIn("Hello World", self.display(app))
                        self.assertEqual(len([item for item in app._chat_blocks if item["kind"] == "tools"]), 1)
                        if size[0] > 52:
                            await pilot.resize_terminal(52, 20)
                            await pilot.pause(.2)
                            from rich.cells import cell_len
                            chat = app.query_one("#chat", ChatLog)
                            title = next(line.text.rstrip() for line in chat.lines if "工具执行" in line.text)
                            self.assertLessEqual(cell_len(title), chat.scrollable_content_region.width)

    async def test_failed_turn_marks_unfinished_calls_and_does_not_fabricate_saved_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            async def runner(question, *, on_tool, **kwargs):
                on_tool({"phase": "started", "tool": "bash", "call_id": "pending", "activity": "检查项目"})
                raise ValueError("fixture failure")
            with patch("dianstar.ui.tui.app.run_turn", side_effect=runner):
                async with app.run_test(size=(80, 24)) as pilot:
                    app.query_one("#input", ChatInput).text = "执行任务"
                    await pilot.press("enter")
                    await app.workers.wait_for_complete()
                    await pilot.pause(.2)
                    block = next(item for item in app._chat_blocks if item["kind"] == "tools")
                    self.assertEqual(block["calls"][0]["status"], "interrupted")
                    self.assertIn("结果待核对", self.display(app))
                    self.assertEqual(app.conversation.messages, [])

    async def test_cancellation_keeps_unknown_results_and_stale_callbacks_cannot_enter_next_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            callbacks = []
            async def runner(question, *, on_tool, **kwargs):
                callbacks.append(on_tool)
                if len(callbacks) == 1:
                    on_tool({"phase": "started", "tool": "bash", "call_id": "pending"})
                    raise asyncio.CancelledError()
                callbacks[0]({"phase": "started", "tool": "bash", "call_id": "stale"})
                return "next answer"
            with patch("dianstar.ui.tui.app.run_turn", side_effect=runner):
                async with app.run_test(size=(80, 24)):
                    with self.assertRaises(asyncio.CancelledError):
                        await app._ask("cancel first turn")
                    from textual.widgets import Static
                    self.assertIn("已取消", str(app.query_one("#status", Static).render()))
                    self.assertEqual(next(item for item in app._chat_blocks if item["kind"] == "tools")["calls"][0]["status"], "interrupted")
                    await app._ask("next turn")
                    self.assertFalse(any(call["call_id"] == "stale" for block in app._chat_blocks if block["kind"] == "tools" for call in block["calls"]))

    async def test_real_runtime_events_reach_ui_and_saved_conversation(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        from tests.test_deepseek import FakeStream
        from dianstar.config.mcp import MCPSettings
        import os
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            app.env_file.write_text("DEEPSEEK_API_KEY=fixture-tool-panel-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(Path(directory) / "state")}), \
                    patch("dianstar.app.runtime.load_mcp_settings", return_value=MCPSettings(git_enabled=False, github_enabled=False)), \
                    patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
                def chunk(content=None, calls=None, finish="stop"):
                    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, delta=SimpleNamespace(
                        content=content, reasoning_content=None, tool_calls=calls))])
                call = SimpleNamespace(index=0, id="actual", function=SimpleNamespace(name="calculate_number", arguments='{"expression":"1+1"}'))
                factory.return_value.chat.completions.create = AsyncMock(side_effect=[FakeStream([
                    chunk(calls=[call], finish="tool_calls")]), FakeStream([chunk(content="2")])])
                async with app.run_test(size=(80, 24)) as pilot:
                    app.query_one("#input", ChatInput).text = "计算1+1"
                    await pilot.press("enter")
                    await app.workers.wait_for_complete()
                    await pilot.pause(.2)
                    calls = next(block for block in app._chat_blocks if block["kind"] == "tools")["calls"]
                    self.assertEqual(calls[0]["title"], "计算数值")
                    self.assertEqual(calls[0]["status"], "success")
                    self.assertIn("2", calls[0]["summary"])
                    restored = Conversation.load(app.conversation.id, app.conversation.directory)
                    self.assertEqual(restored.messages[-1]["tool_activity"], calls)

    async def test_foreign_conversation_callbacks_do_not_change_display(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            async with app.run_test(size=(80, 24)):
                before = deepcopy(app._chat_blocks)
                app._tool_progress({"phase": "started", "tool": "read", "call_id": "other", "conversation_id": "not-this-session"})
                self.assertEqual(app._chat_blocks, before)
                self.assertFalse(app._tool_pending)
