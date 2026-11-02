"""Outgoing GitHub content is checked before approval and again before mutation."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp.types import CallToolResult, TextContent
from dianstar.hooks.github import GitHubWriteHooks
from dianstar.hooks.registry import HookRegistration
from dianstar.security.authorization import Authorization
from dianstar.security.content_guard import Finding, SecurityViolation
from dianstar.security.gateway import ToolPreparationError
from dianstar.tools.mcp.github_writes import GitHubWrites


ARGS = {"owner": "owner", "repo": "repo", "issue_number": 1, "body": "Acceptance content"}


class GitHubHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.env = Path(temporary.name) / "missing.env"
        self.adapter = SimpleNamespace(env_file=self.env, token=lambda: "fixture-known-key",
                                       validate_write=AsyncMock(return_value=None))
        self.call = AsyncMock(return_value=CallToolResult(content=[TextContent(type="text", text=json.dumps({
            "id": 1, "html_url": "https://github.com/owner/repo/issues/1#issuecomment-1"}))]))
        self.writes = GitHubWrites(self.adapter, Authorization(), {"github": SimpleNamespace(call=self.call)}, AsyncMock())
        self.writes.snapshot = AsyncMock(return_value={"number": 1, "state": "open"})

    async def test_normal_write_reports_complete_check(self):
        preview = json.loads(await self.writes.prepare("add_issue_comment", ARGS))
        self.assertTrue(preview["内容检查"]["complete"])
        result = await self.writes.apply("add_issue_comment", ARGS)
        self.assertTrue(result["content_check"]["complete"])
        self.call.assert_awaited_once_with("add_issue_comment", ARGS)

    async def test_known_credential_never_reaches_preview_or_network(self):
        args = {**ARGS, "body": "Do not publish fixture-known-key"}
        with self.assertRaises(ToolPreparationError) as caught:
            await self.writes.prepare("add_issue_comment", args)
        self.assertEqual(caught.exception.result["error"], "sensitive_content_denied")
        self.assertNotIn("fixture-known-key", str(caught.exception.result))
        self.writes.snapshot.assert_not_awaited()
        self.call.assert_not_awaited()

    async def test_policy_change_between_preview_and_apply_prevents_mutation(self):
        await self.writes.prepare("add_issue_comment", ARGS)
        def reject(context, path, data):
            return [Finding(path, 1, "测试拒绝")] if b"Acceptance" in data else []
        self.writes.hooks = GitHubWriteHooks(self.env, self.adapter.token, additional_hooks=(
            HookRegistration("fixture.reject", ("before_github_write",), reject),))
        result = await self.writes.apply("add_issue_comment", ARGS)
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.call.assert_not_awaited()

    async def test_broken_hook_never_sends_mutation(self):
        def broken(*_):
            raise RuntimeError("fixture failure")
        self.writes.hooks = GitHubWriteHooks(self.env, self.adapter.token, additional_hooks=(
            HookRegistration("fixture.broken", ("before_github_write",), broken),))
        with self.assertRaises(ToolPreparationError) as caught:
            await self.writes.prepare("add_issue_comment", ARGS)
        self.assertEqual(caught.exception.result["error"], "security_scan_incomplete")
        self.call.assert_not_awaited()

    def test_nested_and_multiline_text_is_checked_in_each_write_tool(self):
        hooks = GitHubWriteHooks(self.env, self.adapter.token)
        for name in ("create_pull_request", "update_pull_request", "issue_write", "add_issue_comment",
                     "pull_request_review_write", "add_comment_to_pending_review", "add_reply_to_pull_request_comment"):
            with self.subTest(name=name), self.assertRaises(SecurityViolation):
                hooks.check(name, {"comments": [{"body": "line one\napi_key = fixture-known-key\nline three"}]})

    async def test_no_prepared_operation_never_mutates(self):
        self.assertEqual((await self.writes.apply("add_issue_comment", ARGS))["error"], "github_not_prepared")
        self.call.assert_not_awaited()
