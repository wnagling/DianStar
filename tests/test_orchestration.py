import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.runtime import run_turn
from dianstar.config.mcp import MCPSettings
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.platform.file_state import protect
from dianstar.tools.local.registry import build_local_registry
from tests.test_permissions import reply


def calls(*items):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(content=None,
        tool_calls=[SimpleNamespace(id=identifier, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
                    for identifier, name, arguments in items]))])


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True, capture_output=True)
        self.env = self.base / ".env"
        self.env.write_text("DEEPSEEK_API_KEY=fixture-model-key\n")
        self.state = self.base / "state"
        environment = patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.state)})
        environment.start()
        self.addCleanup(environment.stop)
        settings = patch("dianstar.app.runtime.load_mcp_settings", return_value=MCPSettings(git_enabled=True,
            git_repositories=(str(self.root),), git_write_enabled=False, github_enabled=False, workspace_write_enabled=True))
        settings.start()
        self.addCleanup(settings.stop)

    def events(self):
        with closing(sqlite3.connect(self.state / "tasks.sqlite3")) as connection:
            rows = connection.execute("SELECT payload FROM events ORDER BY seq").fetchall()
        return [json.loads(protect(row[0], decrypt=True)) for row in rows]

    async def test_batch_trash_is_one_confirmation_with_visible_receipts_and_recovery_ids(self):
        first, second = self.root / "unused-one", self.root / "unused-two"
        for path in (first, second):
            path.mkdir()
            (path / "fixture.bin").write_bytes(bytes(range(256)))
        history, progress = [], []
        approve = AsyncMock(return_value=True)
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                calls(("recycle", "workspace_trash_put", {"paths": [str(first), str(second)]})), reply("两个目录已移入回收站。")])
            answer = await run_turn("将 unused-one 和 unused-two 两个目录都删除", env_file=self.env,
                project_directory=self.root, conversation_id="fixture-storage-conversation", on_permission=approve,
                history=history, on_tool=progress.append)
        approve.assert_awaited_once()
        self.assertEqual(create.await_count, 2)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertIn("回收站", answer)
        completion = next(event for event in progress if event["call_id"] == "recycle" and event["phase"] == "completed")
        self.assertEqual(completion["title"], "移入回收站")
        self.assertEqual(completion["status"], "success")
        receipt = next(event for event in self.events() if event.get("call_id") == "recycle" and event["event"] == "completed")
        self.assertEqual(len(receipt["result"]["entries"]), 2)
        self.assertTrue(all(len(entry["id"]) == 32 for entry in receipt["result"]["entries"]))
        self.assertIn(receipt["result"]["entries"][0]["id"], completion["output"])

    async def test_scratch_scripts_run_and_are_released_without_project_review_claims(self):
        step = 0
        async def respond(**request):
            nonlocal step
            step += 1
            actual = [json.loads(item["content"]) for item in request["messages"] if item["role"] == "tool"]
            if step == 1:
                return calls(("scratch", "workspace_scratch_create", {"repository": str(self.root)}))
            scratch = actual[0]["repository"]
            if step == 2:
                return calls(("script", "write", {"repository": scratch, "path": "probe.py", "content": "print('fixture-scratch-ok')\n"}))
            if step == 3:
                import sys
                return calls(("execute", "workspace_run", {"repository": scratch, "argv": [sys.executable, "probe.py"]}))
            self.assertIn("fixture-scratch-ok", actual[-1]["content"])
            return reply("临时脚本已运行。")
        history = []
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=respond)
            answer = await run_turn("用临时脚本输出一条信息", env_file=self.env, project_directory=self.root,
                conversation_id="fixture-storage-conversation", on_permission=AsyncMock(return_value=True), history=history)
        self.assertEqual(step, 4)
        self.assertIn("临时脚本", answer)
        self.assertFalse((self.root / "probe.py").exists())
        from dianstar.tools.local.storage.journal import StorageJournal
        self.assertEqual(StorageJournal().records("scratch")[0]["status"], "released")
        self.assertFalse(any(event.get("tool") == "record_review" for event in self.events()))

    async def test_mistaken_deletion_is_exposed_to_reflection_then_restored(self):
        helper = self.root / "helper.py"
        helper.write_text("def useful(): return 1\n")
        step = 0
        async def respond(**request):
            nonlocal step
            step += 1
            if step == 1:
                return calls(("delete", "workspace_delete", {"repository": str(self.root), "path": "helper.py"}))
            if step == 2:
                return reply("已经全部完成。")
            if step == 3:
                snapshot = json.loads(request["messages"][-1]["content"])
                self.assertEqual(snapshot["origin"], "application_change_review")
                self.assertEqual(snapshot["files"][0]["operation"], "delete")
                self.assertIn("-def useful()", snapshot["files"][0]["diff"])
                return calls(("restore", "file_restore", {"repository": str(self.root),
                    "transaction_id": snapshot["files"][0]["transaction_ids"][0]}))
            return reply("误删已恢复。")
        history, progress = [], []
        approve = AsyncMock(return_value=True)
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=respond)
            answer = await run_turn("保留 helper.py，不应删除它", env_file=self.env, project_directory=self.root,
                on_permission=approve, history=history, on_tool=progress.append)
        self.assertEqual(step, 4)
        self.assertEqual(helper.read_text(), "def useful(): return 1\n")
        self.assertIn("已恢复至原状态", answer)
        self.assertEqual(approve.await_count, 2)
        self.assertTrue(any(event["tool"] == "review_changes" for event in progress))
        self.assertEqual(history[-1]["content"], answer)

    async def test_unverified_model_assessment_is_kept_distinct_from_test_success(self):
        step = 0
        async def respond(**request):
            nonlocal step
            step += 1
            if step == 1:
                return calls(("write", "write", {"repository": str(self.root), "path": "notes.md", "content": "hello"}))
            if step == 2:
                return reply("完成了。")
            if step == 3:
                snapshot = json.loads(request["messages"][-1]["content"])
                return calls(("reflect", "record_review", {"decision": "keep", "reason": "文本差异符合任务，没有运行测试",
                    "evidence_call_ids": [snapshot["evidence_call_id"]]}))
            return reply("文本修改已核对。")
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=respond)
            answer = await run_turn("创建 notes.md", env_file=self.env, project_directory=self.root, on_permission=AsyncMock(return_value=True))
        self.assertIn("已记录模型复核", answer)
        self.assertIn("未取得当前修改后的测试通过记录", answer)
        self.assertTrue(any(event["event"] == "reflection" for event in self.events()))

    async def test_budget_exhaustion_preserves_effects_pairs_and_never_calls_seventeenth_decision(self):
        count = 0
        async def respond(**request):
            nonlocal count
            count += 1
            return calls((f"write-{count}", "write", {"repository": str(self.root), "path": f"file-{count}.txt", "content": "saved"}))
        history = []
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=respond)
            answer = await run_turn("依次创建文件", env_file=self.env, project_directory=self.root,
                on_permission=AsyncMock(return_value=True), history=history)
        self.assertEqual(count, 16)
        self.assertEqual(len(list(self.root.glob("file-*.txt"))), 16)
        self.assertEqual(len([message for message in history if message["role"] == "tool"
                              and not json.loads(message["content"]).get("origin")]), 16)
        self.assertIn("16 次模型决策上限", answer)
        self.assertIn("尚未完成验收", answer)
        with closing(sqlite3.connect(self.state / "tasks.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT state FROM turns").fetchone()[0], "budget_exhausted")

    async def test_result_is_persisted_before_sibling_finishes_and_cancel_keeps_all_receipts(self):
        fast_done = asyncio.Event()
        both = asyncio.Event()
        entered, cleaned = [], []
        def registry(*args):
            result, act_web = build_local_registry(*args)
            original = result["calculate_number"]
            async def act(action):
                entered.append(action.call_id)
                if len(entered) == 2:
                    both.set()
                try:
                    await both.wait()
                    if action.call_id == "slow":
                        await asyncio.sleep(30)
                    return Observation(action, {"result": "2"})
                finally:
                    cleaned.append(action.call_id)
            result["calculate_number"] = ToolRegistration(original.schema, original.capability, act, parallel_safe=True)
            return result, act_web
        def progress(event):
            if event["phase"] == "completed" and event["call_id"] == "fast":
                self.assertTrue(any(item.get("call_id") == "fast" and item["event"] == "completed" for item in self.events()))
                fast_done.set()
        with patch("dianstar.app.runtime.build_local_registry", side_effect=registry), patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            create = factory.return_value.chat.completions.create = AsyncMock(return_value=calls(
                ("slow", "calculate_number", {"expression": "1+1"}), ("fast", "calculate_number", {"expression": "1+1"})))
            task = asyncio.create_task(run_turn("同时查询", env_file=self.env, project_directory=self.root, on_tool=progress))
            await asyncio.wait_for(fast_done.wait(), 10)
            self.assertEqual(create.await_count, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finished = [event for event in self.events() if event["event"] == "completed"]
        self.assertEqual({event["call_id"] for event in finished}, {"fast", "slow"})
        self.assertEqual(len(finished), 2)
        self.assertEqual(set(cleaned), {"slow", "fast"})

    async def test_ui_displays_each_completion_before_batch_finishes(self):
        from dianstar.sessions.conversations import Conversation
        from dianstar.ui.tui.app import DianStarApp
        from textual.widgets import Static
        app = DianStarApp(Conversation.create(self.base / "conversations"), env_file=self.env,
                          config_file=self.base / ".local/config.json")
        async with app.run_test(size=(80, 24)):
            app._tool_progress({"phase": "started", "tool": "read", "call_id": "slow"})
            app._tool_progress({"phase": "started", "tool": "read", "call_id": "fast"})
            app._tool_progress({"phase": "completed", "tool": "read", "call_id": "fast", "success": True})
            self.assertEqual(app._tool_pending, {"slow"})
            self.assertEqual(app._tool_finished, 1)
            self.assertIn("1 项操作", str(app.query_one("#status", Static).render()))
