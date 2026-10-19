"""Small, bounded Think → Act → Observe loop."""

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


@dataclass(frozen=True)
class Verdict:
    status: str  # completed, needs_input, or retry
    feedback: str = ""
    answer: str = ""


async def run_loop(
    think: Callable[[list[Observation], str], Awaitable[str | ToolAction]],
    act: Callable[[ToolAction], Awaitable[Observation]],
    verify: Callable[[str, list[Observation]], Verdict],
    *,
    max_steps: int = 4,
) -> str:
    """Advance until a verified answer, a clarification, or a bounded failure."""
    observations: list[Observation] = []
    feedback = ""
    used_calls: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for _ in range(max_steps):
        decision = await think(observations, feedback)
        feedback = ""
        if isinstance(decision, str):
            verdict = verify(decision, observations)
            if verdict.status in {"completed", "needs_input"}:
                return verdict.answer or decision
            if verdict.status != "retry" or not verdict.feedback:
                raise ValueError("Agent returned an invalid completion verdict.")
            feedback = verdict.feedback
            continue
        key = (decision.name, tuple(sorted(decision.arguments.items())))
        if key in used_calls:
            raise ValueError("DeepSeek repeated the same tool call.")
        used_calls.add(key)
        observations.append(await act(decision))
    raise ValueError("DeepSeek did not complete the task within the loop limit.")
