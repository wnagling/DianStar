"""Offline semantic review and preflight ordering; never execute commands."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

from dianstar.security.command_audit import CommandAuditor
from dianstar.security.content_guard import ContentScanner
from dianstar.security.system_paths import HostSafetyError
from dianstar.security.gateway import ToolPreparationError
from dianstar.tools.local.commands import CommandTools
from dianstar.providers.command_audit import review_command
from dianstar.core.types import ToolAction, Observation
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy


class CommandAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.review = AsyncMock(return_value={"decision": "deny", "reason": "删除与任务无关"})
        self.record = AsyncMock()
        self.audit = CommandAuditor(self.review, self.record, question="修复登录页面",
            scanner=ContentScanner())
        self.args = {"repository": "fixture-project", "command": "Remove-Item -Recurse ./src"}

    async def test_reviewer_returns_denial_for_execution_gate(self):
        await self.audit("shell", self.args, {})
        event = self.record.await_args.args[0]
        self.assertEqual(event["decision"], "deny")
        self.assertTrue(event["affects_authorization"])
        self.assertNotIn("command", event)
        payload = json.loads(self.review.await_args.args[0])
        self.assertEqual(payload["user_task"], "修复登录页面")
        self.assertIn("unknown", payload["target_inventory"])

    async def test_readonly_command_does_not_spend_a_review(self):
        await self.audit("shell", {**self.args, "command": "Get-Location"}, {})
        self.review.assert_not_awaited()
        self.record.assert_not_awaited()

    async def test_destructive_aliases_dotnet_and_git_are_reviewed(self):
        for command in ("ri ./fixture -Recurse", "[IO.Directory]::Delete('./fixture', $true)",
                        "git clean -fdx", "git reset --hard", "robocopy ./empty ./fixture /MIR"):
            result = await self.audit("shell", {**self.args, "command": command}, {})
            self.assertEqual(result["status"], "reviewed", command)
            self.assertEqual(result["decision"], "deny")

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

    async def test_hard_rejection_precedes_reviewer_and_audit_denial_blocks_preparation(self):
        context = SimpleNamespace(command_audit=self.audit)
        commands = CommandTools(context)
        commands.prepare = Mock(side_effect=HostSafetyError("fixture denied", "dangerous_command_denied"))
        with self.assertRaises(ToolPreparationError):
            await commands.preview(self.args, "shell")
        self.review.assert_not_awaited()
        plan = {"argv": ["fixture"], "project_environment_configured": False,
                "project_environment_revision": 0}
        commands.prepare = Mock(return_value=plan)
        with self.assertRaises(ToolPreparationError) as caught:
            await commands.preview(self.args, "shell")
        self.assertEqual(caught.exception.result["error"], "command_audit_denied")
        self.assertFalse(commands.prepared)
        self.assertEqual(self.record.await_args.args[0]["decision"], "deny")

    async def test_a_later_denial_invalidates_an_earlier_prepared_plan(self):
        commands = CommandTools(SimpleNamespace(command_audit=self.audit))
        commands.prepare = Mock(return_value={"argv": ["fixture"],
            "project_environment_configured": False, "project_environment_revision": 0})
        self.review.return_value = {"decision": "allow", "reason": "fixture task"}
        await commands.preview(self.args, "shell")
        self.assertTrue(commands.prepared)
        self.review.return_value = {"decision": "deny", "reason": "fixture unsafe"}
        with self.assertRaises(ToolPreparationError):
            await commands.preview(self.args, "shell")
        result = await commands.apply(ToolAction("shell", self.args))
        self.assertEqual(result.result["error"], "workspace_not_prepared")

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


class AuditGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, decision="confirm", *, approve=True, policy=False, available=True,
                       pregrant=False, failure=False, revoke=False):
        auth = Authorization()
        self.addCleanup(auth.close)
        reviewer = AsyncMock(return_value={"decision": decision, "reason": "fixture 审计理由"})
        if failure:
            reviewer.side_effect = TimeoutError()
        audit = CommandAuditor(reviewer, AsyncMock(), question="fixture task", scanner=ContentScanner())
        context = SimpleNamespace(command_audit=audit,
            projects=SimpleNamespace(settings=SimpleNamespace(enabled=True, write_enabled=True)))
        commands = CommandTools(context)
        commands.prepare = Mock(return_value={"argv": ["fixture-python", "-m", "unittest"],
            "routine_validation": True, "project_environment_configured": False,
            "project_environment_revision": 0})
        args = {"repository": "fixture-project", "argv": ["python", "-m", "unittest"]}
        action = ToolAction("workspace_run", args)
        handler = AsyncMock(return_value=Observation(action, {"success": True}))
        registry = commands.registrations()
        registry["workspace_run"] = replace(registry["workspace_run"], handler=handler)
        async def confirm(request):
            if revoke:
                auth.deny("workspace.write")
            return approve
        prompt = AsyncMock(side_effect=confirm)
        gateway = ToolGateway(auth, registry, prompt if available else None,
            policies={"workspace_run": ToolPolicy(requires_approval=policy)} if policy is not None else None)
        if pregrant:
            auth.grant("workspace.write")
        result = await gateway.execute(action)
        return result.result, handler, prompt

    async def test_deny_blocks_even_with_pregrant_and_disabled_prompts(self):
        result, handler, prompt = await self.run_case("deny", pregrant=True)
        self.assertEqual(result["error"], "command_audit_denied")
        handler.assert_not_awaited()
        prompt.assert_not_awaited()

    async def test_confirm_overrides_auto_and_backend_waivers(self):
        for policy in (False, None):
            result, handler, prompt = await self.run_case(policy=policy)
            self.assertTrue(result["success"])
            prompt.assert_awaited_once()
            self.assertIn("审计要求", prompt.await_args.args[0]["summary"])
            handler.assert_awaited_once()

    async def test_confirm_without_user_consent_never_executes(self):
        for options in ({"approve": False}, {"available": False}, {"pregrant": True, "approve": False}):
            result, handler, _ = await self.run_case(**options)
            self.assertEqual(result["error"], "authorization_required")
            handler.assert_not_awaited()

    async def test_audit_failure_requires_user_consent(self):
        result, handler, prompt = await self.run_case(failure=True, approve=False)
        self.assertEqual(result["error"], "authorization_required")
        prompt.assert_awaited_once()
        handler.assert_not_awaited()

    async def test_allow_never_overrides_explicit_backend_confirmation(self):
        result, handler, prompt = await self.run_case("allow", policy=True, approve=False)
        self.assertEqual(result["error"], "authorization_required")
        handler.assert_not_awaited()
        prompt.assert_awaited_once()

    async def test_revocation_during_forced_prompt_prevents_execution(self):
        result, handler, _ = await self.run_case(revoke=True)
        self.assertEqual(result["error"], "authorization_changed")
        handler.assert_not_awaited()
