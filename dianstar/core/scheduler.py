"""Bounded tool concurrency with resource ordering and complete batch results."""
import asyncio

from .types import Observation, ToolAction
from .resources import ResourceClaim, conflicts


class BatchExecutor:
    def __init__(self, registry, execute, *, on_started=None, on_completed=None, concurrency=4, timeout=210, resources=None):
        self.registry, self.execute = registry, execute
        self.on_started, self.on_completed = on_started, on_completed
        self.concurrency, self.timeout = concurrency, timeout
        self.resources = resources

    def claims(self, action):
        registration = self.registry.get(action.name)
        try:
            if self.resources is not None:
                claims = tuple(self.resources(action))
            elif registration is not None and registration.resources is not None:
                claims = tuple(registration.resources(action))
            else:
                # Undeclared effects have global scope, rather than assuming
                # an unknown handler is independent just because it is async.
                claims = (ResourceClaim("runtime", write=not self.parallel(action)),)
            if not claims or any(not isinstance(claim, ResourceClaim) for claim in claims):
                raise ValueError("Invalid resource declaration")
            return claims
        except Exception:
            return (ResourceClaim("runtime", write=True),)

    def parallel(self, action):
        registration = self.registry.get(action.name)
        return (registration is not None and registration.parallel_safe is True
                and registration.approval_description is None and not registration.effects)

    async def __call__(self, actions):
        results = [None] * len(actions)
        self.last_results = results
        semaphore = asyncio.Semaphore(self.concurrency)
        claims = [self.claims(action) for action in actions]
        finished = [asyncio.Event() for _ in actions]
        predecessors = [[previous for previous in range(index) if conflicts(claims[previous], current)]
                        for index, current in enumerate(claims)]

        async def run(index):
            # Edges always point to an earlier call: no resource-order cycle,
            # and waiting for conflicts never consumes a worker slot.
            try:
                for previous in predecessors[index]:
                    await finished[previous].wait()
                await execute(index)
            finally:
                finished[index].set()

        async def execute(index):
            action = actions[index]
            async with semaphore:
                if self.on_started is not None:
                    await self.on_started(action)
                try:
                    # Approval/mutation tools own their timeouts. An outer
                    # query timeout must never interrupt a user's dialog.
                    if self.parallel(action):
                        result = await asyncio.wait_for(self.execute(action), self.timeout)
                    else:
                        result = await self.execute(action)
                    if not isinstance(result, Observation) or result.action != action:
                        raise ValueError("Invalid tool observation")
                except asyncio.CancelledError:
                    result = Observation(action, {"error": "tool_cancelled", "message": "请求已取消；修改类操作需依据持久记录核对实际结果。"})
                    results[index] = result
                    if self.on_completed is not None:
                        await self.on_completed(result)
                    raise
                except TimeoutError:
                    result = Observation(action, {"error": "tool_timeout", "message": "本次查询超时，不能当作没有匹配或已经完成。"})
                except Exception:
                    result = Observation(action, {"error": "tool_execution_failed", "message": "本次工具执行失败；未自动重试，其他独立操作继续处理。"})
                results[index] = result
                if self.on_completed is not None:
                    await self.on_completed(result)

        try:
            async with asyncio.TaskGroup() as group:
                for index in range(len(actions)):
                    group.create_task(run(index))
        except BaseException:
            # Account for queued and not-started calls as well. The provider
            # receives a complete set of pairs before this batch is abandoned.
            missing = []
            for index, result in enumerate(results):
                if result is None:
                    result = Observation(actions[index], {"error": "tool_cancelled", "message": "本批已停止，该项未取得执行结果；请核对任务记录。"})
                    results[index] = result
                    missing.append(result)
            self.last_results = results
            if self.on_completed is not None:
                for result in missing:
                    await self.on_completed(result)
            raise
        self.last_results = results
        return results
