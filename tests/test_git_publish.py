"""One approval publishes an explicit file set; all Git fixtures are local."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.mcp.client import MCPTools
from dianstar.tools.mcp.servers.git.operations import GitError
from dianstar.tools.mcp.servers.git.workspace import GitWorkspace
from tests.test_git_operations import GitFixture
from tests.test_permissions import call, reply
from dianstar.app.runtime import run_turn


class PublishTests(GitFixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.workspace = GitWorkspace([self.repo], allow_writes=True)
        (self.repo / "one.txt").write_text("selected change\n")

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, files=None, **arguments):
        return self.workspace.prepare("git_publish", str(self.repo), files=files or ["one.txt"],
                                      message="publish selected files", branch="main", **arguments)

    def test_preview_preserves_index_then_publishes_selected_new_and_deleted_files(self):
        (self.repo / "new.txt").write_text("new implementation\n")
        (self.repo / "two.txt").write_text("keep unrelated working change\n")
        (self.repo / "removed.txt").write_text("old content")
        self.git("add", "removed.txt")
        self.git("commit", "-m", "fixture deletion baseline")
        (self.repo / "removed.txt").unlink()
        before = self.git("rev-parse", "HEAD")
        index_before = (self.repo / ".git/index").read_bytes()
        plan = self.publish(["one.txt", "new.txt", "removed.txt"], set_upstream=True)
        self.assertNotIn("error", plan)
        self.assertIn(str(self.remote), plan["description"])
        self.assertIn("fixture deletion baseline", plan["description"])
        self.assertIn("作者：", plan["description"])
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("for-each-ref", cwd=self.remote), "")
        result = self.workspace.apply(plan["token"])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["completed_steps"], ["stage", "commit", "push"])
        self.assertTrue(result["remote_verified"])
        self.assertEqual(result["commit"], self.git("rev-parse", "refs/heads/main", cwd=self.remote))
        self.assertEqual(self.git("show", "HEAD:new.txt"), "new implementation")
        self.assertEqual(self.git("show", "HEAD:two.txt"), "original two")
        self.assertEqual((self.repo / "two.txt").read_text(), "keep unrelated working change\n")
        self.assertNotIn("removed.txt", self.git("ls-tree", "--name-only", "HEAD"))
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertIn("error", self.workspace.apply(plan["token"]))

    def test_unselected_staged_files_are_not_silently_included_or_unstaged(self):
        (self.repo / "two.txt").write_text("unselected staged change")
        self.git("add", "two.txt")
        before = (self.repo / ".git/index").read_bytes()
        result = self.publish()
        self.assertEqual(result["error"], "git_publish_scope_mismatch")
        self.assertEqual((self.repo / ".git/index").read_bytes(), before)

    def test_selected_secret_and_env_paths_are_denied_before_approval(self):
        token = "ghp_" + "C3d4E5f6" * 5
        (self.repo / "one.txt").write_text(token)
        result = self.publish()
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertNotIn(token, str(result))
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        (self.repo / ".env").write_text("local configuration")
        self.assertIn("error", self.publish([".env"]))

    def test_selected_file_changed_after_approval_is_not_published(self):
        (self.repo / "new.txt").write_text("approved content")
        plan = self.publish(["new.txt"])
        head = self.git("rev-parse", "HEAD")
        (self.repo / "new.txt").write_text("changed after preview")
        result = self.workspace.apply(plan["token"])
        self.assertEqual(result["error"], "git_state_changed")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_changed_remote_is_rejected_before_staging(self):
        plan = self.publish()
        before = self.git("rev-parse", "HEAD")
        with patch.object(self.workspace.security, "remote_head", return_value="1" * 40):
            result = self.workspace.apply(plan["token"])
        self.assertEqual(result["error"], "git_state_changed")
        self.assertEqual(result["completed_steps"], [])
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_push_failure_retains_commit_and_reports_partial_progress(self):
        plan = self.publish()
        apply = self.workspace._apply
        def fail_push(context, inner):
            if inner["name"] == "git_push":
                raise GitError("fixture_push_failure", "fixture remote unavailable")
            return apply(context, inner)
        with patch.object(self.workspace, "_apply", side_effect=fail_push):
            result = self.workspace.apply(plan["token"])
        self.assertEqual(result["error"], "fixture_push_failure")
        self.assertEqual(result["completed_steps"], ["stage", "commit"])
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("for-each-ref", cwd=self.remote), "")
        self.assertFalse(result["remote_verified"])

    def test_force_and_scope_changes_are_not_part_of_publish(self):
        result = self.publish(force_with_lease=True)
        self.assertEqual(result["error"], "git_invalid_arguments")
        plan = self.publish()
        self.git("remote", "set-url", "origin", str(self.root / "different.git"))
        self.assertEqual(self.workspace.apply(plan["token"])["error"], "git_state_changed")

    def test_commit_result_lost_is_reported_as_uncertain_without_retry(self):
        plan = self.publish()
        apply = self.workspace._apply
        before = self.git("rev-parse", "HEAD")
        def lose_commit_result(context, inner):
            result = apply(context, inner)
            if inner["name"] == "git_commit":
                raise GitError("git_outcome_unknown", "fixture commit acknowledgement lost")
            return result
        with patch.object(self.workspace, "_apply", side_effect=lose_commit_result):
            result = self.workspace.apply(plan["token"])
        self.assertEqual(result["completed_steps"], ["stage"])
        self.assertEqual(result["failed_step"], "commit")
        self.assertTrue(result["outcome_unknown"])
        self.assertNotEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "2")
        self.assertEqual(self.git("for-each-ref", cwd=self.remote), "")

    def test_diverged_remote_is_rejected_without_mutating_index(self):
        self.apply("git_push", branch="main")
        old = self.git("rev-parse", "HEAD")
        (self.repo / "two.txt").write_text("remote fixture change")
        self.git("add", "two.txt")
        self.git("commit", "-m", "fixture remote advance")
        self.git("push", "origin", "main")
        self.git("reset", "--soft", old)
        self.git("restore", "--staged", "two.txt")
        before = (self.repo / ".git/index").read_bytes()
        result = self.publish()
        self.assertEqual(result["error"], "git_publish_not_fast_forward")
        self.assertEqual((self.repo / ".git/index").read_bytes(), before)


class PublishMCPTests(GitFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup()
        (self.repo / "one.txt").write_text("selected change\n")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_real_mcp_publish_needs_exactly_one_confirmation(self):
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.repo),), git_write_enabled=True)
        auth = Authorization()
        approve = AsyncMock(return_value=True)
        async with MCPTools(settings, self.root / ".env", auth) as tools:
            gateway = ToolGateway(auth, tools.registry, approve)
            result = await gateway.execute(ToolAction("mcp_git__git_publish", {
                "repository": str(self.repo), "files": ["one.txt"],
                "message": "publish selected files", "branch": "main", "remote": "origin"}))
        self.assertTrue(result.result.get("success"), result.result)
        self.assertTrue(result.result["remote_verified"])
        approve.assert_awaited_once()
        summary = approve.await_args.args[0]["summary"]
        self.assertIn(str(self.remote), summary)
        self.assertIn("one.txt", summary)
        self.assertNotIn("token", result.result)

    async def test_decline_missing_callback_and_revoke_do_not_stage_or_commit(self):
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.repo),), git_write_enabled=True)
        action = ToolAction("mcp_git__git_publish", {"repository": str(self.repo), "files": ["one.txt"],
                           "message": "publish selected files", "branch": "main"})
        before = self.git("rev-parse", "HEAD")
        for mode in ("no_callback", "decline", "revoke"):
            auth = Authorization()
            async def callback(request):
                if mode == "revoke":
                    auth.deny("mcp.git.git_publish")
                    return True
                return False
            async with MCPTools(settings, self.root / ".env", auth) as tools:
                result = await ToolGateway(auth, tools.registry, None if mode == "no_callback" else callback).execute(action)
            self.assertIn("error", result.result)
            self.assertEqual(self.git("rev-parse", "HEAD"), before)
            self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
            self.assertEqual(self.git("for-each-ref", cwd=self.remote), "")

    async def test_real_remote_rejection_preserves_partial_progress_through_adapter(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.repo),), git_write_enabled=True)
        auth = Authorization()
        approve = AsyncMock(return_value=True)
        async with MCPTools(settings, self.root / ".env", auth) as tools:
            result = await ToolGateway(auth, tools.registry, approve).execute(ToolAction("mcp_git__git_publish", {
                "repository": str(self.repo), "files": ["one.txt"], "message": "publish selected files", "branch": "main"}))
        self.assertIn("error", result.result)
        self.assertEqual(result.result["completed_steps"], ["stage", "commit"])
        self.assertEqual(result.result["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("for-each-ref", cwd=self.remote), "")
        approve.assert_awaited_once()

    async def test_component_policy_and_revocation_cannot_be_bypassed(self):
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.repo),), git_write_enabled=True)
        action = ToolAction("mcp_git__git_publish", {"repository": str(self.repo), "files": ["one.txt"],
                           "message": "publish selected files", "branch": "main"})
        auth = Authorization()
        before = self.git("rev-parse", "HEAD")
        async with MCPTools(settings, self.root / ".env", auth) as tools:
            for operation in ("git_stage", "git_commit", "git_push"):
                approve = AsyncMock(return_value=True)
                gateway = ToolGateway(auth, tools.registry, approve,
                                      policies={"mcp_git__" + operation: ToolPolicy(enabled=False)})
                self.assertNotIn("mcp_git__git_publish", {s["function"]["name"] for s in gateway.available_schemas()})
                result = await gateway.execute(action)
                self.assertEqual(result.result["error"], "tool_disabled")
                approve.assert_not_awaited()
            auth.deny("mcp.git.git_push")
            result = await ToolGateway(auth, tools.registry, AsyncMock(return_value=True)).execute(action)
            self.assertEqual(result.result["error"], "authorization_denied")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    async def test_component_revoked_during_confirmation_prevents_all_changes(self):
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.repo),), git_write_enabled=True)
        auth = Authorization()
        async def revoke(request):
            auth.revoke("mcp.git.git_push")
            return True
        before = self.git("rev-parse", "HEAD")
        async with MCPTools(settings, self.root / ".env", auth) as tools:
            result = await ToolGateway(auth, tools.registry, revoke).execute(ToolAction("mcp_git__git_publish", {
                "repository": str(self.repo), "files": ["one.txt"], "message": "publish selected files", "branch": "main"}))
        self.assertEqual(result.result["error"], "authorization_changed")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    async def test_index_findings_keep_their_source_through_mcp(self):
        value = "unregistered-credential-value"
        (self.repo / "ordinary.json").write_text(json.dumps({"access_token": value}))
        self.git("add", "ordinary.json")
        self.git("commit", "-m", "external fixture baseline")
        self.git("add", "one.txt")
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.repo),), git_write_enabled=True)
        auth = Authorization()
        approve = AsyncMock(return_value=True)
        async with MCPTools(settings, self.root / ".env", auth) as tools:
            result = await ToolGateway(auth, tools.registry, approve).execute(ToolAction("mcp_git__git_commit", {
                "repository": str(self.repo), "message": "attempt"}))
        self.assertEqual(result.result["error"], "sensitive_content_denied")
        self.assertEqual(result.result["source"], "git_index")
        self.assertNotIn(value, str(result.result))
        approve.assert_not_awaited()

    async def test_runtime_uses_publish_with_one_confirmation_and_reports_verified_result(self):
        env = self.root / ".env"
        env.write_text("DEEPSEEK_API_KEY=fixture-model-key\n")
        approve = AsyncMock(return_value=True)
        history = []
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("mcp_git__git_status", {"repository": str(self.repo)}),
                call("mcp_git__git_publish", {"repository": str(self.repo), "files": ["one.txt"],
                     "message": "publish selected files", "branch": "main", "remote": "origin"}),
                reply("已提交并推送，远程核验通过。"),
            ])
            await run_turn("把当前项目的改动提交并推送到远程仓库", env_file=env,
                           project_directory=self.repo, on_permission=approve, history=history)
        approve.assert_awaited_once()
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertTrue(results[-1]["remote_verified"])
        self.assertEqual(results[-1]["commit"], self.git("rev-parse", "refs/heads/main", cwd=self.remote))
