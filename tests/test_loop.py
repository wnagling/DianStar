# 测试通用任务循环的停止规则。

import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from dianstar.core.types import Observation, ToolAction
from dianstar.core.loop import run_loop
from dianstar.core.registry import ToolRegistration
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway

class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_final_answer_returns_without_answer_verifier(self):
        async def think(_observations):
            return "模型直接回答"

        async def act(_action):
            self.fail("No tool was requested")

        self.assertEqual(await run_loop(think, act), "模型直接回答")

    async def test_identical_calls_execute_again_and_return_fresh_results(self):
        action = ToolAction("query", {"items": [1, {"enabled": False}]})
        results = []

        async def think(observations):
            if len(observations) == 2:
                results.extend(item.result for item in observations)
                return "done"
            return action

        count = 0

        async def act(request):
            nonlocal count
            count += 1
            return Observation(request, {"revision": count})

        self.assertEqual(await run_loop(think, act), "done")
        self.assertEqual(results, [{"revision": 1}, {"revision": 2}])

    async def test_retry_after_failure_can_use_identical_arguments(self):
        action = ToolAction("query", {"target": "same"})
        think = AsyncMock(side_effect=[action, action, "recovered"])
        act = AsyncMock(side_effect=[Observation(action, {"error": "temporary_failure"}),
                                    Observation(action, {"content": "available"})])
        self.assertEqual(await run_loop(think, act), "recovered")
        self.assertEqual(act.await_count, 2)

    async def test_read_edit_read_returns_changed_file_contents(self):
        # Fixture tools exercise the loop; this does not add product file tools.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "example.py"
            path.write_text("old content\n", encoding="utf-8")
            read = ToolAction("read_file", {"path": str(path)})
            edit = ToolAction("edit_file", {"path": str(path), "content": "new content\n"})
            think = AsyncMock(side_effect=[read, edit, read, "done"])
            reads = []

            async def act(action):
                if action.name == "edit_file":
                    path.write_text(action.arguments["content"], encoding="utf-8")
                    return Observation(action, {"saved": True})
                content = path.read_text(encoding="utf-8")
                reads.append(content)
                return Observation(action, {"content": content})

            self.assertEqual(await run_loop(think, act), "done")
            self.assertEqual(reads, ["old content\n", "new content\n"])

    async def test_same_test_command_can_run_after_a_fix(self):
        # Run a real disposable Python check through fixture execution adapters.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "check.py"
            path.write_text("def add(a, b): return a - b\nassert add(2, 3) == 5\n", encoding="utf-8")
            run = ToolAction("run_command", {"argv": [sys.executable, str(path)]})
            fix = ToolAction("edit_file", {"path": str(path)})
            think = AsyncMock(side_effect=[run, fix, run, "passed"])
            exit_codes = []

            async def act(action):
                if action.name == "edit_file":
                    path.write_text("def add(a, b): return a + b\nassert add(2, 3) == 5\n", encoding="utf-8")
                    return Observation(action, {"saved": True})
                result = await asyncio.to_thread(subprocess.run, action.arguments["argv"],
                                                capture_output=True, timeout=10)
                exit_codes.append(result.returncode)
                return Observation(action, {"exit_code": result.returncode})

            self.assertEqual(await run_loop(think, act), "passed")
            self.assertEqual(exit_codes, [1, 0])

    async def test_identical_calls_in_one_batch_preserve_distinct_call_ids(self):
        first = ToolAction("query", {"target": "same"}, call_id="first")
        second = ToolAction("query", {"target": "same"}, call_id="second")
        seen = []

        async def act(action):
            seen.append(action.call_id)
            return Observation(action, {"call": action.call_id})

        self.assertEqual(await run_loop(AsyncMock(side_effect=[[first, second], "done"]), act), "done")
        self.assertEqual(seen, ["first", "second"])

    async def test_unending_identical_calls_stop_at_the_step_limit(self):
        action = ToolAction("query", {"target": "same"})
        think = AsyncMock(return_value=action)
        act = AsyncMock(return_value=Observation(action, {"content": "same"}))
        with self.assertRaisesRegex(ValueError, "loop limit"):
            await run_loop(think, act, max_steps=3)
        self.assertEqual(think.await_count, 3)
        self.assertEqual(act.await_count, 3)

    async def test_final_answer_on_sixteenth_step_succeeds_with_batched_tools(self):
        actions = [ToolAction("query", {"target": "first"}),
                   ToolAction("query", {"target": "second"})]
        think = AsyncMock(side_effect=[actions] * 15 + ["done"])
        act = AsyncMock(side_effect=lambda action: Observation(action, {"content": "ok"}))

        self.assertEqual(await run_loop(think, act), "done")
        self.assertEqual(think.await_count, 16)
        self.assertEqual(act.await_count, 30)

    async def test_default_limit_stops_before_seventeenth_decision(self):
        action = ToolAction("query", {})
        think = AsyncMock(side_effect=[action] * 16 + ["too late"])
        act = AsyncMock(return_value=Observation(action, {"content": "ok"}))

        with self.assertRaisesRegex(ValueError, "loop limit"):
            await run_loop(think, act)
        self.assertEqual(think.await_count, 16)
        self.assertEqual(act.await_count, 16)

    async def test_repeated_calls_still_consume_scoped_grants(self):
        schema = {"type": "function", "function": {"name": "query", "parameters": {
            "type": "object", "properties": {}, "additionalProperties": False}}}
        action = ToolAction("query", {})
        authorization = Authorization()
        authorization.grant("fixture.read", uses=1)
        handler = AsyncMock(return_value=Observation(action, {"content": "ok"}))
        gateway = ToolGateway(authorization, {"query": ToolRegistration(schema, "fixture.read", handler)})
        results = []

        async def act(request):
            result = await gateway.execute(request)
            results.append(result.result)
            return result

        await run_loop(AsyncMock(side_effect=[action, action, "done"]), act)
        handler.assert_awaited_once()
        self.assertEqual(results[0], {"content": "ok"})
        self.assertEqual(results[1]["error"], "authorization_required")

    async def test_repeated_sensitive_calls_require_fresh_approval(self):
        schema = {"type": "function", "function": {"name": "write", "parameters": {
            "type": "object", "properties": {}, "additionalProperties": False}}}
        action = ToolAction("write", {})
        handler = AsyncMock(return_value=Observation(action, {"saved": True}))
        approve = AsyncMock(side_effect=[True, False])
        gateway = ToolGateway(Authorization(), {"write": ToolRegistration(
            schema, "fixture.write", handler, lambda _: "Preview current operation")}, approve)
        await run_loop(AsyncMock(side_effect=[action, action, "done"]), gateway.execute)
        self.assertEqual(approve.await_count, 2)
        handler.assert_awaited_once()

    async def test_batch_size_limit_is_preserved(self):
        for actions in ([], [ToolAction("query", {})] * 9):
            with self.subTest(count=len(actions)):
                act = AsyncMock()
                with self.assertRaisesRegex(ValueError, "工具调用数量无效"):
                    await run_loop(AsyncMock(return_value=actions), act)
                act.assert_not_awaited()
