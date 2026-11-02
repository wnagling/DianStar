"""Bounded stdio transport and MCP SDK lifecycle; no product tool policy."""
import asyncio
import copy
from datetime import timedelta
import os
from mcp import ClientSession
from mcp.client.stdio import stdio_client


class MCPUnavailable(Exception):
    pass


class StdioConnection:
    """One owner task for SDK cancel scopes; no cross-task context exits."""

    def __init__(self, parameters):
        self.parameters = parameters
        self.queue = asyncio.Queue(maxsize=8)
        self.task = None
        self.changed = False
        self.tools = []
        self.workers = {}
        self.pending = set()
        self.slots = asyncio.Semaphore(4)
        self.capacity = asyncio.Semaphore(8)

    async def start(self):
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._serve())
        try:
            await asyncio.wait_for(asyncio.shield(self.ready), 15)
        except BaseException:
            await self.close()
            if self.ready.done() and not self.ready.cancelled():
                self.ready.exception()
            raise

    async def _notice(self, message):
        root = getattr(message, "root", None)
        if getattr(root, "method", None) == "notifications/tools/list_changed":
            self.changed = True

    @staticmethod
    def timeout(name):
        return 190 if name == "git_apply" else 45 if name in {"git_pull", "git_remote_branch_delete", "git_push"} else 12

    async def _request(self, session, name, arguments, future):
        try:
            async with self.slots:
                if future.cancelled():
                    return
                if self.changed:
                    raise MCPUnavailable()
                result = await asyncio.wait_for(session.call_tool(name, arguments,
                    read_timeout_seconds=timedelta(seconds=self.timeout(name))), self.timeout(name) + 1)
                if self.changed:
                    raise MCPUnavailable()
                if not future.done():
                    future.set_result(result)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except Exception as error:
            self.failure_type = type(error).__name__
            if not future.done():
                future.set_exception(MCPUnavailable())
        finally:
            self.workers.pop(future, None)

    async def _serve(self):
        try:
            with open(os.devnull, "w") as errors:
                async with stdio_client(self.parameters, errlog=errors) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=12),
                                             message_handler=self._notice) as session:
                        await session.initialize()
                        cursor = None
                        for _ in range(4):
                            result = await session.list_tools(cursor=cursor)
                            self.tools.extend(result.tools)
                            if len(self.tools) > 128:
                                raise MCPUnavailable()
                            cursor = result.nextCursor
                            if not cursor:
                                break
                        if cursor:
                            raise MCPUnavailable()
                        self.ready.set_result(True)
                        # SDK contexts enter/exit in this owner; request children
                        # are joined before those contexts close.
                        async with asyncio.TaskGroup() as group:
                            while True:
                                name, arguments, future = await self.queue.get()
                                if future.cancelled():
                                    continue
                                if self.changed:
                                    raise MCPUnavailable()
                                self.workers[future] = group.create_task(self._request(session, name, arguments, future))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure_type = type(error).__name__
        finally:
            if not self.ready.done():
                self.ready.set_exception(MCPUnavailable())
            for future in tuple(self.pending):
                if not future.done():
                    future.set_exception(MCPUnavailable())
            self.workers.clear()
            while not self.queue.empty():
                self.queue.get_nowait()

    async def call(self, name, arguments):
        if self.task is None or self.task.done() or self.changed:
            raise MCPUnavailable()
        acquired = False
        future = asyncio.get_running_loop().create_future()
        try:
            await asyncio.wait_for(self.capacity.acquire(), self.timeout(name) + 2)
            acquired = True
            if self.task.done() or self.changed:
                raise MCPUnavailable()
            self.pending.add(future)
            self.queue.put_nowait((name, copy.deepcopy(arguments), future))
            return await asyncio.wait_for(future, self.timeout(name) + 3)
        except BaseException:
            # A request timeout/cancellation does not close the shared session.
            future.cancel()
            worker = self.workers.get(future)
            if worker is not None:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
            raise
        finally:
            self.pending.discard(future)
            if acquired:
                self.capacity.release()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
