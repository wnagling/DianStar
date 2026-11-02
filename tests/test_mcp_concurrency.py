import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
import json
from pathlib import Path
import sys
from unittest.mock import patch

from dianstar.tools.mcp.connection import StdioConnection, MCPUnavailable


class MCPConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.active = 0
        self.peak = 0
        self.entered = []
        self.stopped = []
        self.release = asyncio.Event()
        owner = self
        class Session:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                owner.context_task = asyncio.current_task()
                return self
            async def __aexit__(self, *args):
                owner.assertIs(asyncio.current_task(), owner.context_task)
                owner.assertEqual(owner.active, 0)
            async def initialize(self):
                pass
            async def list_tools(self, **kwargs):
                return SimpleNamespace(tools=[], nextCursor=None)
            async def call_tool(self, name, arguments, **kwargs):
                owner.active += 1
                owner.peak = max(owner.peak, owner.active)
                owner.entered.append(name)
                try:
                    if name == "bad":
                        raise ValueError("fixture request error")
                    if name.startswith("wait"):
                        await owner.release.wait()
                    return {"name": name}
                finally:
                    owner.active -= 1
                    owner.stopped.append(name)
        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield None, None
        self.session_patch = patch("dianstar.tools.mcp.connection.ClientSession", Session)
        self.transport_patch = patch("dianstar.tools.mcp.connection.stdio_client", transport)
        self.session_patch.start()
        self.transport_patch.start()
        self.connection = StdioConnection(None)
        await self.connection.start()

    async def asyncTearDown(self):
        await self.connection.close()
        self.transport_patch.stop()
        self.session_patch.stop()

    async def test_requests_overlap_and_one_cancel_does_not_close_connection(self):
        first = asyncio.create_task(self.connection.call("wait1", {}))
        second = asyncio.create_task(self.connection.call("wait2", {}))
        for _ in range(50):
            if self.peak == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.peak, 2)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(self.connection.task.done())
        self.release.set()
        self.assertEqual((await second)["name"], "wait2")
        self.assertEqual((await self.connection.call("later", {}))["name"], "later")

    async def test_request_failure_isolated_and_shutdown_joins_all_requests(self):
        result = await asyncio.gather(self.connection.call("bad", {}), self.connection.call("good", {}), return_exceptions=True)
        self.assertIsInstance(result[0], MCPUnavailable)
        self.assertEqual(result[1], {"name": "good"})
        waiting = asyncio.create_task(self.connection.call("wait1", {}))
        await asyncio.sleep(0.02)
        await self.connection.close()
        with self.assertRaises((MCPUnavailable, asyncio.CancelledError)):
            await waiting
        self.assertEqual(self.active, 0)
        self.assertFalse(self.connection.workers)

    async def test_request_timeout_does_not_close_shared_connection(self):
        self.connection.timeout = lambda name: 0.01 if name == "wait1" else 12
        with self.assertRaises(MCPUnavailable):
            await self.connection.call("wait1", {})
        self.assertFalse(self.connection.task.done())
        self.assertEqual((await self.connection.call("after-timeout", {}))["name"], "after-timeout")

    async def test_queued_cancellation_and_concurrency_bound(self):
        tasks = [asyncio.create_task(self.connection.call(f"wait{index}", {})) for index in range(8)]
        for _ in range(100):
            if len(self.connection.workers) == 8 and self.peak == 4:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.peak, 4)
        tasks[-1].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tasks[-1]
        self.release.set()
        results = await asyncio.gather(*tasks[:-1])
        self.assertEqual(len(results), 7)
        self.assertFalse(self.connection.task.done())


class RealStdioConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_overlaps_calls_and_survives_one_cancellation(self):
        from mcp import StdioServerParameters
        connection = StdioConnection(StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).parent / "fixtures/concurrent_mcp_server.py")]))
        await connection.start()
        try:
            slow = asyncio.create_task(connection.call("probe", {"delay": 2}))
            await asyncio.sleep(0.04)
            fast = await connection.call("probe", {"delay": 0.01})
            result = fast.structuredContent or json.loads(fast.content[0].text)
            self.assertGreaterEqual(result["overlap"], 2)
            self.assertFalse(slow.done())
            slow.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await slow
            self.assertFalse(connection.task.done())
            later = await connection.call("probe", {"delay": 0})
            self.assertEqual((later.structuredContent or json.loads(later.content[0].text))["delay"], 0)
        finally:
            await connection.close()
