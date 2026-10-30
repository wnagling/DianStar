"""Validated, immutable event bindings for trusted application callbacks."""
from dataclasses import dataclass
import inspect
from types import MappingProxyType
from typing import Callable

from dianstar.security.content_guard import ContentScanner, Finding

EVENTS = frozenset({"before_write", "before_stage", "before_commit", "before_push", "after_command", "after_clone"})


@dataclass(frozen=True)
class HookContext:
    event: str
    scanner: ContentScanner
    local_only: bool = False


@dataclass(frozen=True)
class HookRegistration:
    name: str
    events: tuple[str, ...]
    handler: Callable[[HookContext, str, bytes], list[Finding]]
    priority: int = 100


class HookRegistry:
    def __init__(self, registrations):
        bindings = {event: [] for event in EVENTS}
        names = set()
        for entry in registrations:
            if (not isinstance(entry, HookRegistration) or not isinstance(entry.name, str)
                    or not entry.name.strip() or entry.name in names
                    or not isinstance(entry.events, tuple) or not entry.events
                    or any(not isinstance(event, str) or event not in EVENTS for event in entry.events)
                    or len(set(entry.events)) != len(entry.events)
                    or type(entry.priority) is not int or not callable(entry.handler)
                    or inspect.iscoroutinefunction(entry.handler)
                    or inspect.iscoroutinefunction(getattr(entry.handler, "__call__", None))):
                raise ValueError("Invalid or duplicate hook registration")
            names.add(entry.name)
            for event in entry.events:
                bindings[event].append(entry)
        if any(not entries for entries in bindings.values()):
            raise ValueError("Every supported event requires a hook")
        self._bindings = MappingProxyType({event: tuple(sorted(entries, key=lambda entry: entry.priority))
                                          for event, entries in bindings.items()})

    def for_event(self, event):
        if event not in self._bindings:
            raise ValueError("Unknown hook event")
        return self._bindings[event]


def application_registry(additional_hooks=()):
    # Lazy imports keep event definitions independent from adapters and runner.
    from .workspace import registrations as workspace_hooks
    from .git import registrations as git_hooks
    return HookRegistry((*workspace_hooks(), *git_hooks(), *additional_hooks))
