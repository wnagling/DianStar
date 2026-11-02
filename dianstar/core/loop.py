"""Bounded provider-independent decision and tool loop."""
from typing import Awaitable, Callable
from dianstar.core.types import Observation, ToolAction


DEFAULT_MAX_STEPS = 16


async def run_loop(
    think: Callable[[list[Observation]], Awaitable[str | ToolAction | list[ToolAction]]],
    act: Callable[[ToolAction], Awaitable[Observation]],
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    execute_batch=None,
    before_finish=None,
    on_exhausted=None,
) -> str:
    """Count each model decision, including the final answer, as one step."""
    observations: list[Observation] = []
    for step in range(max_steps):
        decision = await think(observations)
        if isinstance(decision, str):
            if before_finish is None or await before_finish(decision, max_steps - step - 1):
                return decision
            continue
        actions = decision if isinstance(decision, list) else [decision]
        if not actions or len(actions) > 8:
            raise ValueError("模型一次请求的工具调用数量无效。")
        if execute_batch is not None:
            outcomes = await execute_batch(actions)
            observations.extend(outcomes)
            continue
        for action in actions:
            # Identical arguments may observe new state, retry a temporary failure,
            # or verify a fix. Execute each call through act and its normal policy.
            outcome = await act(action)
            observations.append(outcome)
    if on_exhausted is not None:
        return await on_exhausted(observations)
    raise ValueError("Agent did not complete the task within the loop limit.")
