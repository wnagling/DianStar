"""Offline semantic review and preflight ordering; never execute commands."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from dianstar.security.command_audit import CommandAuditor
from dianstar.security.content_guard import ContentScanner
from dianstar.security.system_paths import HostSafetyError
from dianstar.security.gateway import ToolPreparationError
from dianstar.tools.local.commands import CommandTools
from dianstar.providers.command_audit import review_command


class CommandAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.review = AsyncMock(return_value={"decision": "deny", "reason": "删除与任务无关"})
        self.record = AsyncMock()
        self.audit = CommandAuditor(self.review, self.record, question="修复登录页面",
            scanner=ContentScanner())
        self.args = {"repository": "fixture-project", "command": "Remove-Item -Recurse ./src"}

    async def test_observation_records_denial_without_granting_or_raising(self):
        await self.audit("shell", self.args, {})
        event = self.record.await_args.args[0]
        self.assertEqual(event["decision"], "deny")
        self.assertFalse(event["affects_authorization"])
        self.assertNotIn("command", event)
        payload = json.loads(self.review.await_args.args[0])
        self.assertEqual(payload["user_task"], "修复登录页面")
        self.assertIn("unknown", payload["target_inventory"])

    async def test_readonly_command_does_not_spend_a_review(self):
        await self.audit("shell", {**self.args, "command": "Get-Location"}, {})
        self.review.assert_not_awaited()
        self.record.assert_not_awaited()

    async def test_observation_log_failure_does_not_become_execution_policy(self):
        self.record.side_effect = OSError("fixture log unavailable")
        await self.audit("shell", self.args, {})


    async def test_budget_is_reserved_before_concurrent_requests(self):
        self.audit.limit = 1
        await asyncio.gather(*(self.audit("shell", self.args, {}) for _ in range(3)))
        self.assertEqual(self.review.await_count, 1)
        self.assertEqual([c.args[0]["status"] for c in self.record.await_args_list].count("budget_exhausted"), 2)

    async def test_malformed_and_failed_reviews_are_never_recorded_as_allow(self):
        for result in ({"decision": "allow"}, {"decision": "yes", "reason": "fixture"},
                       {"decision": "allow", "reason": "fixture", "extra": True}):
            self.review.return_value = result
            await self.audit("shell", self.args, {})
            self.assertEqual(self.record.await_args.args[0]["status"], "unavailable")
        self.review.side_effect = RuntimeError("fixture-private-error")
        await self.audit("shell", self.args, {})
        self.assertNotIn("fixture-private-error", str(self.record.await_args))

    async def test_timeout_and_cancellation(self):
        async def slow(_):
            await asyncio.sleep(10)
        self.review.side_effect = slow
        self.audit.timeout = .01
        await self.audit("shell", self.args, {})
        self.assertEqual(self.record.await_args.args[0]["status"], "unavailable")
        self.review.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.audit("shell", self.args, {})

    async def test_known_credentials_never_leave_in_task_or_command(self):
        secret = "fixture-private-value-123456789"
        self.audit.scanner = ContentScanner(known=(secret,))
        self.audit.question = "删除 " + secret
        await self.audit("shell", self.args, {})
        self.review.assert_not_awaited()
        self.assertEqual(self.record.await_args.args[0]["status"], "sensitive_input_skipped")

    async def test_oversized_input_is_skipped_without_partial_review(self):
        self.audit.question = "fixture " * 3000
        await self.audit("shell", self.args, {})
        self.review.assert_not_awaited()
        self.assertEqual(self.record.await_args.args[0]["status"], "input_too_large")

    async def test_hard_rejection_precedes_reviewer_and_observer_does_not_change_plan(self):
        context = SimpleNamespace(command_audit=self.audit)
        commands = CommandTools(context)
        commands.prepare = Mock(side_effect=HostSafetyError("fixture denied", "dangerous_command_denied"))
        with self.assertRaises(ToolPreparationError):
            await commands.preview(self.args, "shell")
        self.review.assert_not_awaited()
        plan = {"argv": ["fixture"], "project_environment_configured": False,
                "project_environment_revision": 0}
        commands.prepare = Mock(return_value=plan)
        await commands.preview(self.args, "shell")
        self.assertEqual(next(iter(commands.prepared.values()))[1], plan)
        self.assertEqual(self.record.await_args.args[0]["decision"], "deny")

    async def test_provider_is_separate_flash_request_without_tools(self):
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content='{"decision":"confirm","reason":"范围未知"}'))]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = await review_command(client, "fixture-data")
        self.assertEqual(result["decision"], "confirm")
        request = create.await_args.kwargs
        self.assertEqual(request["model"], "deepseek-flash")
        self.assertNotIn("tools", request)
        self.assertEqual(len(request["messages"]), 2)
