"""Ordered synchronous checks with shared limits and fail-closed errors."""
from dataclasses import asdict
import inspect
import time

from dianstar.security.content_guard import ContentScanner, Finding, MAX_FILES, MAX_TOTAL, SecurityViolation
from .registry import HookContext, application_registry


class HookRunner:
    def __init__(self, scanner=None, *, additional_hooks=()):
        self.scanner = scanner or ContentScanner()
        # Trusted additions cannot replace or omit built-in safety callbacks.
        self.registry = application_registry(additional_hooks)

    def run(self, event, files, *, local_only=False):
        findings, total, count = [], 0, 0
        deadline = time.monotonic() + 20
        try:
            registrations = self.registry.for_event(event)
            context = HookContext(event, self.scanner, local_only)
            for path, data in files:
                total += len(data)
                count += 1
                if total > MAX_TOTAL or count > MAX_FILES or time.monotonic() > deadline:
                    raise SecurityViolation(incomplete=True)
                for registration in registrations:
                    result = registration.handler(context, path, data)
                    if inspect.iscoroutine(result):
                        result.close()
                        raise SecurityViolation(incomplete=True)
                    if not isinstance(result, (list, tuple)) or any(not isinstance(item, Finding) for item in result):
                        raise SecurityViolation(incomplete=True)
                    findings.extend(result)
                    # Cooperative deadline: arbitrary Python callbacks are not isolated.
                    if time.monotonic() > deadline:
                        raise SecurityViolation(incomplete=True)
            if time.monotonic() > deadline:
                raise SecurityViolation(incomplete=True)
        except SecurityViolation:
            raise
        except Exception:
            raise SecurityViolation(incomplete=True) from None
        if findings and event.startswith("before_"):
            raise SecurityViolation(findings)
        return {"event": event, "complete": True, "checked_files": count,
                "findings": [asdict(item) for item in findings[:30]]}
