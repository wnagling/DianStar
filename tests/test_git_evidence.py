"""Operation targets must not become evidence of reading repository contents."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from dianstar.evidence.source_evidence import SourceEvidence
from dianstar.security.authorization import Authorization
from dianstar.tools.mcp.connection import MCPUnavailable
from dianstar.tools.mcp.git_adapter import GitAdapter


URL = "https://github.com/owner/project"
SHA = "1" * 40
ARGS = {"repository": "C:/fixture/project", "remote": "origin", "branch": "main"}


def push_result(**changes):
    return {"server": "git", "tool": "git_push", "operation": "git_push", "repository": ARGS["repository"],
            "remote": "origin", "branch": "main", "remote_url": URL + ".git", "commit": SHA,
            "success": True, "remote_verified": True, **changes}


class GitEvidenceTests(unittest.TestCase):
    def test_verified_push_is_an_operation_receipt_not_a_web_source(self):
        ledger = SourceEvidence()
        result = ledger.observe("mcp_git__git_push", ARGS, push_result())
        answer = ledger.annotate_answer(f"已推送到 [项目]({URL})。")
        self.assertTrue(result["operation_evidence"]["remote_verified"])
        self.assertFalse(result["operation_evidence"]["web_content_read"])
        self.assertEqual(ledger.sources, {})
        self.assertIn("Git 操作核对", answer)
        self.assertIn("远程提交已通过 Git 核对", answer)
        self.assertNotIn("引用未核验", answer)
        self.assertNotIn("来源核验", answer)
        self.assertIn("不表示已读取", answer)

    def test_configuration_address_is_not_a_successful_push(self):
        ledger = SourceEvidence()
        result = ledger.observe("mcp_git__git_remotes", {"repository": ARGS["repository"]}, {
            "server": "git", "tool": "git_remotes", "content": json.dumps([
                {"name": "origin", "fetch": URL + ".git", "push": URL + ".git"}]), "truncated": False})
        answer = ledger.annotate_answer(URL)
        self.assertIn("地址来自 Git 远程配置", answer)
        self.assertNotIn("远程提交已通过 Git 核对", answer)
        self.assertFalse(result["operation_evidence"]["remote_verified"])

    def test_windows_repository_slashes_and_case_do_not_change_identity(self):
        ledger = SourceEvidence()
        ledger.observe("mcp_git__git_push", ARGS, push_result(repository="c:\\fixture\\PROJECT"))
        self.assertIn("远程提交已通过 Git 核对", ledger.annotate_answer(URL))

    def test_publish_receipt_uses_the_same_remote_verification_requirements(self):
        ledger = SourceEvidence()
        ledger.observe("mcp_git__git_publish", ARGS, push_result(tool="git_publish", operation="git_publish"))
        self.assertIn("远程提交已通过 Git 核对", ledger.annotate_answer(URL))
        ledger.observe("mcp_git__git_publish", ARGS, push_result(tool="git_publish", operation="git_publish",
                       error="git_outcome_unknown", remote_verified=False))
        self.assertNotIn("远程提交已通过 Git 核对", ledger.annotate_answer(URL))

    def test_real_repository_reading_still_uses_source_checks(self):
        ledger = SourceEvidence()
        ledger.observe("mcp_git__git_push", ARGS, push_result())
        readme = URL + "/blob/main/README.md"
        answer = ledger.annotate_answer(f"[项目]({URL}) [说明]({readme})")
        self.assertIn("Git 操作核对", answer)
        self.assertIn("引用未核验", answer)
        ledger.observe("read_webpage", {"url": readme}, {"content": "README body"})
        answer = ledger.annotate_answer(readme)
        self.assertIn("已取得网页文本", answer)
        self.assertNotIn("Git 操作核对", answer)

    def test_failed_web_read_is_not_hidden_by_successful_git_operation(self):
        ledger = SourceEvidence()
        ledger.observe("mcp_git__git_push", ARGS, push_result())
        ledger.observe("read_webpage", {"url": URL}, {"error": "web_timeout"})
        answer = ledger.annotate_answer(URL)
        self.assertIn("远程提交已通过 Git 核对", answer)
        self.assertIn("读取失败，未取得正文", answer)

    def test_failed_push_does_not_reuse_old_success_for_the_same_branch(self):
        ledger = SourceEvidence()
        ledger.observe("mcp_git__git_push", ARGS, push_result())
        ledger.observe("mcp_git__git_push", ARGS, push_result(error="git_outcome_unknown", success=False, remote_verified=False))
        answer = ledger.annotate_answer(URL)
        self.assertIn("远程结果尚未核对成功", answer)
        self.assertNotIn("远程提交已通过 Git 核对", answer)

    def test_text_flags_and_mismatched_targets_never_create_verified_receipts(self):
        for result in (push_result(repository="C:/different/project"), push_result(branch="different"),
                       push_result(remote="different"), push_result(commit="not-a-sha"),
                       {"server": "git", "tool": "git_push", "content": json.dumps(push_result())}):
            ledger = SourceEvidence()
            observed = ledger.observe("mcp_git__git_push", ARGS, result)
            self.assertFalse(observed.get("operation_evidence", {}).get("remote_verified", False))
            self.assertNotIn("远程提交已通过 Git 核对", ledger.annotate_answer(URL))

    def test_shell_output_and_user_text_do_not_verify_a_remote(self):
        ledger = SourceEvidence([{"role": "user", "content": json.dumps(push_result())}])
        ledger.observe("workspace_run", ARGS, push_result())
        self.assertIn("引用未核验", ledger.annotate_answer(URL))

    def test_github_ssh_suffix_case_and_exact_commit_links_are_recognized(self):
        ledger = SourceEvidence()
        ledger.observe("mcp_git__git_push", ARGS, push_result(remote_url="git@github.com:Owner/Project.git"))
        for url in (URL, URL + ".git", URL + "/commit/" + SHA, URL + "/tree/main"):
            with self.subTest(url=url):
                self.assertNotIn("引用未核验", ledger.annotate_answer(url))
                self.assertIn("Git 操作核对", ledger.annotate_answer(url))
        self.assertIn("引用未核验", ledger.annotate_answer(URL + "/commit/" + "2" * 40))

    def test_only_matched_historical_git_results_restore_receipts(self):
        history = [{"role": "assistant", "tool_calls": [{"id": "push", "function": {
            "name": "mcp_git__git_push", "arguments": json.dumps(ARGS)}}]},
            {"role": "tool", "tool_call_id": "push", "content": json.dumps(push_result())}]
        answer = SourceEvidence(history).annotate_answer(URL)
        self.assertIn("来自历史 Git 操作，非本轮新核对", answer)
        history[-1]["tool_call_id"] = "unmatched"
        self.assertIn("引用未核验", SourceEvidence(history).annotate_answer(URL))

    def test_unsafe_and_malformed_links_do_not_gain_operation_evidence(self):
        ledger = SourceEvidence()
        ledger.observe("mcp_git__git_push", ARGS, push_result())
        for url in ("https://github.com:invalid/owner/project", "http://[invalid", "https://127.0.0.1/project"):
            self.assertIsNone(ledger.git_operations.describe(url))


class GitReceiptAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.arguments = {**ARGS, "repository": str(Path(self.temporary.name).resolve())}
        self.connection = SimpleNamespace(call=AsyncMock())
        settings = SimpleNamespace(git_repositories=(self.arguments["repository"],),
                                   git_write_enabled=True, git_workspace_roots=())
        self.adapter = GitAdapter(settings, Authorization(), {"git": self.connection})

    @staticmethod
    def response(value):
        return SimpleNamespace(structuredContent=value, content=[], isError=False)

    def preview(self):
        return self.response({"prepared": True, "token": "fixture-internal-plan", "description": "发布项目",
                              "remote_target": {"url": URL + ".git", "remote": "origin", "branch": "main"}})

    async def test_disconnection_preserves_target_without_claiming_success_or_leaking_plan(self):
        for name in ("git_push", "git_publish"):
            with self.subTest(name=name):
                self.connection.call.side_effect = [self.preview(), MCPUnavailable("fixture disconnect")]
                description = await self.adapter.prepare_git(name, self.arguments)
                result = await self.adapter.apply_git(name, self.arguments)
                self.assertEqual(result["error"], "git_outcome_unknown")
                self.assertEqual(result["remote_url"], URL + ".git")
                self.assertNotIn("fixture-internal-plan", description + str(result))
                ledger = SourceEvidence()
                ledger.observe("mcp_git__" + name, self.arguments, result)
                answer = ledger.annotate_answer(URL)
                self.assertIn("远程结果尚未核对成功", answer)
                self.assertNotIn("引用未核验", answer)

    async def test_successful_reply_retains_verified_receipt_and_actual_operation(self):
        self.connection.call.side_effect = [self.preview(), self.response({
            "operation": "git_publish", "repository": self.arguments["repository"], "success": True,
            "remote_verified": True, "commit": SHA, "content": "published"})]
        await self.adapter.prepare_git("git_publish", self.arguments)
        result = await self.adapter.apply_git("git_publish", self.arguments)
        ledger = SourceEvidence()
        ledger.observe("mcp_git__git_publish", self.arguments, result)
        self.assertIn("远程提交已通过 Git 核对", ledger.annotate_answer(URL))

    async def test_reported_mismatched_target_is_not_overwritten_by_preview_metadata(self):
        self.connection.call.side_effect = [self.preview(), self.response({
            **push_result(), "repository": self.arguments["repository"], "branch": "other"})]
        await self.adapter.prepare_git("git_push", self.arguments)
        result = await self.adapter.apply_git("git_push", self.arguments)
        ledger = SourceEvidence()
        ledger.observe("mcp_git__git_push", self.arguments, result)
        self.assertNotIn("远程提交已通过 Git 核对", ledger.annotate_answer(URL))
