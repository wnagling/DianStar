"""Bounded provider-independent decision and tool loop."""
from typing import Awaitable, Callable
from dianstar.core.types import Observation, ToolAction


DEFAULT_MAX_STEPS = 16


async def run_loop(
    think: Callable[[list[Observation]], Awaitable[str | ToolAction | list[ToolAction]]],
    act: Callable[[ToolAction], Awaitable[Observation]],
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> str:
    """Count each model decision, including the final answer, as one step."""
    observations: list[Observation] = []
    for _ in range(max_steps):
        decision = await think(observations)
        if isinstance(decision, str):
            return decision
        actions = decision if isinstance(decision, list) else [decision]
        if not actions or len(actions) > 8:
            raise ValueError("模型一次请求的工具调用数量无效。")
        for action in actions:
            # Identical arguments may observe new state, retry a temporary failure,
            # or verify a fix. Execute each call through act and its normal policy.
            outcome = await act(action)
            observations.append(outcome)
    raise ValueError("Agent did not complete the task within the loop limit.")
