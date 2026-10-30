"""Trusted hook registration and dispatch must preserve mandatory tool checks."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.hooks import HookRunner
from dianstar.hooks.registry import HookRegistration, HookRegistry, application_registry
from dianstar.security.authorization import Authorization
from dianstar.security.content_guard import ContentScanner, Finding, SecurityViolation
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.workspace import WorkspaceTools


class HookRegistryTests(unittest.TestCase):
    def runner(self, *extra):
        return HookRunner(ContentScanner(fingerprints=[]), additional_hooks=extra)

    def test_registration_order_is_stable_and_specific_to_each_event(self):
        observed = []
        def callback(name):
            def record(context, path, data):
                observed.append((name, context.event, path, data))
                return []
            return record
        runner = self.runner(
            HookRegistration("late", ("before_write",), callback("late"), 30),
            HookRegistration("early", ("before_write",), callback("early"), 10),
            HookRegistration("peer", ("before_write",), callback("peer"), 10))
        runner.run("before_write", [("config.json", b"{}")])
        self.assertEqual([entry[0] for entry in observed], ["early", "peer", "late"])
        self.assertEqual(observed[0][1:], ("before_write", "config.json", b"{}"))
        observed.clear()
        runner.run("after_clone", [("config.json", b"{}")])
        self.assertEqual(observed, [])

    def test_trusted_additions_cannot_replace_or_omit_builtin_checks(self):
        with self.assertRaises(ValueError):
            self.runner(HookRegistration("workspace.content", ("before_write",), lambda *_: []))
        with self.assertRaises(ValueError):
            HookRegistry([])
        runner = self.runner(HookRegistration("observer", ("before_stage",), lambda *_: []))
        with self.assertRaises(SecurityViolation):
            runner.run("before_stage", [(".env", b"safe text")])

    def test_invalid_and_async_registrations_are_rejected_before_dispatch(self):
        async def asynchronous(*_):
            return []
        class AsyncCallable:
            async def __call__(self, *_):
                return []
        for entry in (HookRegistration("unknown", ("before_unknown",), lambda *_: []),
                      HookRegistration("duplicate", ("before_write", "before_write"), lambda *_: []),
                      HookRegistration("invalid", ("before_write",), None),
                      HookRegistration("async", ("before_write",), asynchronous),
                      HookRegistration("async-object", ("before_write",), AsyncCallable())):
            with self.subTest(name=entry.name), self.assertRaises(ValueError):
                application_registry((entry,))

    def test_unknown_event_does_not_consume_input_or_succeed_on_empty_files(self):
        consumed = []
        def files():
            consumed.append(True)
            yield "a.txt", b"a"
        for data in (files(), []):
            with self.assertRaises(SecurityViolation) as error:
                self.runner().run("before_unknown", data)
            self.assertEqual(error.exception.code, "security_scan_incomplete")
        self.assertEqual(consumed, [])

    def test_hook_exception_stops_later_callbacks_and_masks_exception_details(self):
        observed = []
        def broken(*_):
            raise RuntimeError("synthetic-private-debug-value")
        runner = self.runner(HookRegistration("broken", ("before_write",), broken, 1),
                             HookRegistration("later", ("before_write",), lambda *_: observed.append(True) or [], 2))
        with self.assertRaises(SecurityViolation) as error:
            runner.run("before_write", [("a.txt", b"a")])
        self.assertEqual(error.exception.code, "security_scan_incomplete")
        self.assertNotIn("synthetic-private-debug-value", str(error.exception))
        self.assertEqual(observed, [])

    def test_deadline_stops_later_callbacks_after_a_slow_callback_returns(self):
        now, observed = [0], []
        def slow(*_):
            now[0] = 21
            return []
        runner = self.runner(HookRegistration("slow", ("before_write",), slow, 1),
                             HookRegistration("later", ("before_write",), lambda *_: observed.append(True) or [], 2))
        with patch("dianstar.hooks.runner.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(SecurityViolation) as error:
                runner.run("before_write", [("a.txt", b"a")])
        self.assertEqual(error.exception.code, "security_scan_incomplete")
        self.assertEqual(observed, [])

    def test_invalid_callback_result_is_not_silently_accepted(self):
        for handler in (lambda *_: None, lambda *_: ["not a finding"]):
            with self.assertRaises(SecurityViolation):
                self.runner(HookRegistration("bad-result", ("before_write",), handler)).run("before_write", [("a.txt", b"a")])

    def test_registry_entries_are_immutable_and_repeated_runs_remain_independent(self):
        runner = self.runner()
        with self.assertRaises(FrozenInstanceError):
            runner.registry.for_event("before_write")[0].priority = 20
        with self.assertRaises(SecurityViolation):
            runner.run("before_stage", [(".env", b"a")])
        self.assertEqual(runner.run("before_stage", [("a.txt", b"a")])["findings"], [])

    def test_after_hook_returns_findings_without_pretending_to_undo_operation(self):
        hook = HookRegistration("audit", ("after_command",), lambda *_: [Finding("a.txt", 1, "审查提示")])
        report = self.runner(hook).run("after_command", [("a.txt", b"a")])
        self.assertEqual(report["findings"][0]["kind"], "审查提示")
        self.assertTrue(report["complete"])


class HookToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_deny_hook_stops_write_even_when_approval_is_waived(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            auth = Authorization()
            mcp = SimpleNamespace(settings=MCPSettings(git_enabled=True, git_repositories=(str(root),), workspace_write_enabled=True),
                                  authorization=auth, git=SimpleNamespace(repositories={str(root)}))
            tools = WorkspaceTools(mcp, root / ".env")
            tools.security = HookRunner(additional_hooks=(HookRegistration("project.rule", ("before_write",),
                lambda _, path, data: [Finding(path, 1, "项目写入规则拒绝")]),))
            approve = AsyncMock(return_value=True)
            gateway = ToolGateway(auth, tools.registrations(), approve,
                                  policies={"workspace_edit": ToolPolicy(requires_approval=False)})
            result = await gateway.execute(ToolAction("workspace_edit", {"repository": str(root),
                "path": "a.txt", "mode": "create", "new_text": "a"}))
            self.assertEqual(result.result["error"], "sensitive_content_denied")
            self.assertFalse((root / "a.txt").exists())
            approve.assert_not_awaited()
