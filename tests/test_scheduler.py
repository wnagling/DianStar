import asyncio
import unittest
from unittest.mock import AsyncMock

from dianstar.core.registry import ToolRegistration
from dianstar.core.scheduler import BatchExecutor
from dianstar.core.types import Observation, ToolAction


def entry(parallel=True, **options):
    return ToolRegistration({}, "fixture.read", AsyncMock(), parallel_safe=parallel, **options)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_overlap_progress_finishes_early_and_results_remain_ordered(self):
        both = asyncio.Event()
        started, completed = [], []
        async def act(action):
            started.append(action.call_id)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            await asyncio.sleep(0.03 if action.call_id == "slow" else 0)
            return Observation(action, {"content": action.call_id})
        async def done(observation):
            completed.append(observation.action.call_id)
        execute = BatchExecutor({"read": entry()}, act, on_completed=done)
        result = await execute([ToolAction("read", {}, "slow"), ToolAction("read", {}, "fast")])
        self.assertEqual(completed, ["fast", "slow"])
        self.assertEqual([item.action.call_id for item in result], ["slow", "fast"])

    async def test_mutation_and_unknown_tools_are_ordered_barriers(self):
        events, active = [], 0
        async def act(action):
            nonlocal active
            if action.name != "read":
                self.assertEqual(active, 0)
            active += 1
            events.append(action.call_id)
            await asyncio.sleep(0.01)
            active -= 1
            return Observation(action, {})
        execute = BatchExecutor({"read": entry(), "write": entry(effects=("files",))}, act)
        actions = [ToolAction(name, {}, str(index)) for index, name in enumerate(("read", "read", "write", "read", "unknown", "read"))]
        await execute(actions)
        self.assertEqual(events, ["0", "1", "2", "3", "4", "5"])

    async def test_failure_timeout_and_concurrency_limit_do_not_cancel_siblings(self):
        active, peak = 0, 0
        async def act(action):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            try:
                if action.call_id == "failure":
                    raise RuntimeError("fixture failure")
                if action.call_id == "timeout":
                    await asyncio.Event().wait()
                return Observation(action, {"success": True})
            finally:
                active -= 1
        execute = BatchExecutor({"query": entry()}, act, concurrency=2, timeout=1)
        results = await execute([ToolAction("query", {}, identifier) for identifier in ("failure", "timeout", "ok1", "ok2")])
        self.assertEqual(results[0].result["error"], "tool_execution_failed")
        self.assertEqual(results[1].result["error"], "tool_timeout")
        self.assertTrue(results[2].result.get("success") and results[3].result.get("success"), results)
        self.assertEqual(peak, 2)

    async def test_cancellation_accounts_for_every_call_and_waits_for_cleanup(self):
        began, cleaned = asyncio.Event(), []
        async def act(action):
            began.set()
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.append(action.call_id)
        execute = BatchExecutor({"read": entry()}, act, concurrency=1)
        actions = [ToolAction("read", {}, str(index)) for index in range(3)]
        task = asyncio.create_task(execute(actions))
        await began.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(cleaned, ["0"])
        self.assertEqual(len(execute.last_results), 3)
        self.assertTrue(all(item.result["error"] == "tool_cancelled" for item in execute.last_results))

    async def test_approval_is_a_barrier_even_with_incorrect_parallel_flag(self):
        execute = BatchExecutor({"confirm": entry(approval_description=lambda _: "approve")}, AsyncMock())
        self.assertFalse(execute.parallel(ToolAction("confirm", {})))
