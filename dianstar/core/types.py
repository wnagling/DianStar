"""Provider-independent tool actions and observations."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolAction:
    name: str
    arguments: dict[str, object]
    call_id: str | None = None
    raw_arguments: str | None = None


@dataclass(frozen=True)
class Observation:
    action: ToolAction
    result: dict[str, object]
