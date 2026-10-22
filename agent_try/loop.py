# 通用任务循环：在限定次数内推进模型决策、工具执行与结果观察。

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class ToolAction:
    name: str
    arguments: dict[str, str]
    call_id: str | None = None
    raw_arguments: str | None = None


@dataclass(frozen=True)
class Observation:
    action: ToolAction
    result: dict[str, object]


async def run_loop(
    think: Callable[[list[Observation]], Awaitable[str | ToolAction | list[ToolAction]]],
    act: Callable[[ToolAction], Awaitable[Observation]],
    *,
    max_steps: int = 4,
) -> str:
    """Advance until the model answers, asks for input, or reaches the step limit."""
    observations: list[Observation] = []
    used_calls: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for _ in range(max_steps):
        decision = await think(observations)
        if isinstance(decision, str):
            return decision
        actions = decision if isinstance(decision, list) else [decision]
        if not actions or len(actions) > 8:
            raise ValueError("模型一次请求的工具调用数量无效。")
        for action in actions:
            key = (action.name, tuple(sorted(action.arguments.items())))
            if key in used_calls:
                raise ValueError("DeepSeek repeated the same tool call.")
            outcome = await act(action)
            used_calls.add(key)
            observations.append(outcome)
    raise ValueError("DeepSeek did not complete the task within the loop limit.")
