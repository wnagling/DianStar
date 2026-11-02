"""Tool schema, capability and handler contracts."""
from dataclasses import dataclass
from typing import Callable, Awaitable
from .types import ToolAction, Observation


@dataclass(frozen=True)
class ToolRegistration:
    schema: dict
    capability: str
    handler: Callable[[ToolAction], Awaitable[Observation]]
    # Only trusted registrations opt into asking. Missing policies fail closed.
    approval_description: Callable[[dict], str | Awaitable[str]] | None = None
    # Flat, application-owned dependencies for a composite operation.
    required_tools: tuple[str, ...] = ()
    # A trusted tool can classify its already-prepared operation as routine.
    # Explicit backend policies override this default, and preflight still runs.
    auto_approve: Callable[[dict], bool] | None = None
    # Query timeout classification; resource claims control concurrency.
    parallel_safe: bool = False
    effects: tuple[str, ...] = ()
    # Trusted claims are application code, never model-supplied lock metadata.
    resources: Callable[[ToolAction], tuple] | None = None
    # Renamed/replacement tools retain explicit backend restrictions.
    policy_aliases: tuple[str, ...] = ()
