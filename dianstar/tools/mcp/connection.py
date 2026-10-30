"""Bounded stdio transport and MCP SDK lifecycle; no product tool policy."""
import asyncio
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

    async def _serve(self):
        pending = None
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
                        while True:
                            name, arguments, pending = await self.queue.get()
                            if pending.cancelled():
                                pending = None
                                continue
                            if self.changed:
                                raise MCPUnavailable()
                            timeout = 190 if name == "git_apply" else 45 if name in {"git_pull", "git_remote_branch_delete", "git_push"} else 12
                            result = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=timeout))
                            if not pending.done():
                                pending.set_result(result)
                            pending = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not expose SDK exception text: it can contain server output/secrets.
            self.failure_type = type(error).__name__
        finally:
            if not self.ready.done():
                self.ready.set_exception(MCPUnavailable())
            if pending is not None and not pending.done():
                pending.set_exception(MCPUnavailable())
            while not self.queue.empty():
                _, _, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(MCPUnavailable())

    async def call(self, name, arguments):
        if self.task is None or self.task.done() or self.changed:
            raise MCPUnavailable()
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((name, arguments, future))
        try:
            return await asyncio.wait_for(future, 195 if name == "git_apply" else 47 if name in {"git_pull", "git_remote_branch_delete", "git_push"} else 14)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
