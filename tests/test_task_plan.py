"""Persistent metadata, evidence and responsive layout, without provider calls."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dianstar.app.task_plan import TaskPlan
from dianstar.sessions.task_plans import TaskPlanStore
from dianstar.security.content_guard import ContentScanner
from dianstar.core.types import ToolAction, Observation
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.todo import registrations


def draft():
    return {"revision": 0, "title": "修复登录失败", "steps": [
        {"id": "inspect", "title": "定位失败原因", "status": "in_progress", "evidence": []},
        {"id": "fix", "title": "修改登录逻辑", "status": "pending", "evidence": []},
        {"id": "test", "title": "运行相关测试并核对结果", "status": "pending", "evidence": []}]}


class TaskPlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = TaskPlanStore(self.root)
        self.events = []
        self.plan = TaskPlan("fixture-chat", "修复登录问题", ContentScanner(),
            store=self.store, notify=self.events.append)

    async def test_roundtrip_scope_and_revision_conflict(self):
        first = await self.plan.write(draft())
        self.assertEqual(first["plan"]["revision"], 1)
        self.assertEqual((await self.plan.write(draft()))["error"], "todo_conflict")
        self.assertIsNone(self.store.load("other-chat"))
        restored = TaskPlan("fixture-chat", "继续", ContentScanner(), store=TaskPlanStore(self.root))
        self.assertEqual((await restored.load())["title"], "修复登录失败")
        self.assertEqual(self.events[-1]["conversation_id"], "fixture-chat")

    async def test_completion_requires_successful_actual_tool_evidence(self):
        data = draft()
        data["steps"][0].update(status="completed", evidence=["read-1"])
        self.assertEqual((await self.plan.write(data))["error"], "todo_evidence_required")
        action = ToolAction("read", {}, "read-1")
        self.plan.observe(Observation(action, {"error": "fixture-failure"}))
        self.assertEqual((await self.plan.write(data))["error"], "todo_evidence_required")
        self.plan.observe(Observation(action, {"content": "fixture source"}))
        self.assertIn("plan", await self.plan.write(data))
        data["revision"] = 1
        data["steps"][1]["status"] = "in_progress"
        # Preserve an unchanged completed step across subsequent updates.
        self.assertIn("plan", await self.plan.write(data))

    async def test_multiple_active_steps_and_duplicates_rejected(self):
        data = draft()
        data["steps"][1]["status"] = "in_progress"
        self.assertEqual((await self.plan.write(data))["error"], "todo_invalid")
        data = draft()
        data["steps"][1]["id"] = "inspect"
        self.assertEqual((await self.plan.write(data))["error"], "todo_invalid")

    async def test_control_characters_cannot_spoof_sidebar_layout(self):
        data = draft()
        data["title"] = "任务\n伪造标题"
        self.assertEqual((await self.plan.write(data))["error"], "todo_invalid")

    async def test_cancelled_progress_persists_without_grants(self):
        await self.plan.write(draft())
        await self.plan.finish("cancelled")
        self.assertEqual((await self.plan.load())["run_state"], "cancelled")
        self.assertEqual((await self.plan.load())["revision"], 2)
        self.assertNotIn("authorization", await self.plan.load())

    async def test_old_turn_finish_does_not_overwrite_another_writer(self):
        await self.plan.write(draft())
        other = TaskPlan("fixture-chat", "新任务", ContentScanner(), store=self.store)
        data = draft()
        data.update(revision=1, title="新的任务")
        await other.write(data)
        await self.plan.finish("cancelled")
        self.assertEqual((await other.load())["run_state"], "active")

    async def test_sensitive_content_not_saved(self):
        secret = "fixture-sensitive-value-123456789"
        self.plan.scanner = ContentScanner(known=(secret,))
        data = draft()
        data["title"] = secret
        self.assertEqual((await self.plan.write(data))["error"], "todo_sensitive")
        self.assertIsNone(await self.plan.load())

    async def test_tools_use_gateway_and_reject_forged_fields(self):
        auth = Authorization()
        self.addCleanup(auth.close)
        gateway = ToolGateway(auth, registrations(self.plan))
        result = await gateway.execute(ToolAction("todo_write", draft()))
        self.assertIn("plan", result.result)
        bad = {**draft(), "grant_permission": True}
        result = await gateway.execute(ToolAction("todo_write", bad))
        self.assertEqual(result.result["error"], "invalid_tool_arguments")
        auth.deny("task.progress")
        result = await gateway.execute(ToolAction("todo_read", {}))
        self.assertEqual(result.result["error"], "authorization_denied")


class TaskSidebarTests(unittest.IsolatedAsyncioTestCase):
    async def test_wide_narrow_restore_switch_and_literal_titles(self):
        from dianstar.sessions.conversations import Conversation
        from dianstar.ui.tui.app import DianStarApp
        from textual.widgets import Static
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict("os.environ", DIANSTAR_FILE_STATE_ROOT=str(root / "state")):
                conversation = Conversation.create(root / "chats")
                conversation.add_exchange("修复登录失败", "开始检查。")
                plan = TaskPlan(conversation.id, "修复登录失败", ContentScanner())
                await plan.write(draft())
                app = DianStarApp(conversation, config_file=root / "config.json", env_file=root / "unused.env")
                async with app.run_test(size=(160, 45)) as pilot:
                    await pilot.pause()
                    pane = app.query_one("#todo-pane")
                    chat = app.query_one("#chat-pane")
                    self.assertTrue(pane.display)
                    self.assertGreaterEqual(chat.size.width, 90)
                    self.assertGreaterEqual(pane.size.width, 26)
                    self.assertLessEqual(pane.region.right, 160)
                    self.assertLessEqual(chat.region.right, pane.region.x)
                    stale = deepcopy(app._task_plan)
                    stale["title"] = "[bold red]只是文字[/]"
                    app._task_progress({"conversation_id": "other-chat", "plan": stale})
                    self.assertEqual(app._task_plan["title"], "修复登录失败")

                    app._task_progress({"conversation_id": conversation.id, "plan": stale})
                    self.assertIn("[bold red]", str(app.query_one("#todo-title", Static).render()))
                    await pilot.resize_terminal(80, 25)
                    await pilot.pause()
                    self.assertFalse(pane.display)
                    self.assertTrue(chat.display)
                    await pilot.click("#toggle-todo")
                    await pilot.pause()
                    self.assertTrue(pane.display)
                    self.assertFalse(chat.display)
                    self.assertLessEqual(pane.region.right, 80)
                    # Textual ignores clicks during the button's 0.2s active effect.
                    await pilot.pause(.25)
                    await pilot.click("#toggle-todo")
                    await pilot.pause()
                    self.assertTrue(chat.display)
                    await app._command("/new")
                    await pilot.pause()
                    self.assertFalse(pane.display)
                    self.assertFalse(app.query_one("#toggle-todo").display)
                    await app._open_conversation(conversation.id)
                    await pilot.resize_terminal(160, 45)
                    await pilot.pause()
                    self.assertTrue(pane.display)
                    self.assertEqual(app._task_plan["title"], "修复登录失败")


class TaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_project_and_test_keywords_do_not_force_a_plan(self, factory):
        from unittest.mock import AsyncMock
        from dianstar.app.runtime import run_turn
        from dianstar.config.credentials import save_api_key
        from tests.test_permissions import reply
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict("os.environ", DIANSTAR_FILE_STATE_ROOT=str(root / "state")):
                env = root / ".env"
                save_api_key("fixture-fake-key", env)
                factory.return_value.chat.completions.create = AsyncMock(return_value=reply("fixture"))
                events = []
                await run_turn("新建一个简单计算器项目并测试", env_file=env, project_directory=root,
                    conversation_id="fixture-simple", on_task=events.append)
                self.assertEqual(events, [])
                self.assertIsNone(TaskPlanStore().load("fixture-simple"))

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_model_tool_update_emits_live_event_and_restores_next_turn(self, factory):
        from unittest.mock import AsyncMock
        from dianstar.app.runtime import run_turn
        from dianstar.config.credentials import save_api_key
        from tests.test_permissions import call, reply
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict("os.environ", DIANSTAR_FILE_STATE_ROOT=str(root / "state")):
                env = root / ".env"
                save_api_key("fixture-fake-key", env)
                data = draft()
                data["title"] = "计算并核对结果"
                data["steps"] = data["steps"][:2]
                data["steps"][0]["title"] = "计算1+1"
                data["steps"][1]["title"] = "核对结果"
                completed = deepcopy(data)
                completed["revision"] = 1
                for step in completed["steps"]:
                    step.update(status="completed", evidence=["call_calculate_number"])
                create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                    call("todo_write", data), call("calculate_number", {"expression": "1+1"}),
                    call("todo_write", completed), reply("计算结果是2。")])
                events = []
                await run_turn("分步骤计算1+1并核对结果", env_file=env, project_directory=root,
                    conversation_id="fixture-runtime", on_task=events.append)
                self.assertEqual(events[-1]["plan"]["run_state"], "completed")
                self.assertEqual(len(events[-1]["plan"]["steps"]), 2)
                create.side_effect = [reply("这是上次的进度，需要核对现场。")]
                await run_turn("看一下上次进度", env_file=env, project_directory=root,
                    conversation_id="fixture-runtime", on_task=events.append)
                messages = create.await_args.kwargs["messages"]
                self.assertTrue(any("同会话历史待办" in str(message) for message in messages))
