"""Real Git acceptance; disposable workspaces/remotes, no public network or credentials."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock
from textual.containers import VerticalScroll
from dianstar.sessions.conversations import Conversation
from dianstar.ui.tui.screens.permission_approval import PermissionApproval
from dianstar.ui.tui.app import DianStarApp

from dianstar.tools.mcp.servers.git.operations import GitWorkspace
from dianstar.core.types import ToolAction
from dianstar.tools.mcp.client import MCPTools
from dianstar.config.mcp import MCPSettings
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway


class GitFixture:
    def setup(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.com")
        (self.repo / "one.txt").write_text("original one\n", encoding="utf-8")
        (self.repo / "two.txt").write_text("original two\n", encoding="utf-8")
        self.git("add", "one.txt", "two.txt")
        self.git("-c", "core.hooksPath=" + os.devnull, "commit", "-m", "initial fixture")
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(self.remote))
        self.workspace = GitWorkspace([self.repo], allow_writes=True)

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, check=True)
        return result.stdout.decode().strip()

    def prepare(self, name, **args):
        plan = self.workspace.prepare(name, str(self.repo), **args)
        self.assertNotIn("error", plan)
        self.assertTrue(plan["prepared"])
        return plan

    def apply(self, name, **args):
        plan = self.prepare(name, **args)
        result = self.workspace.apply(plan["token"])
        self.assertNotIn("error", result)
        self.assertTrue(result["success"])
        return result


class GitOperationTests(GitFixture, unittest.TestCase):
    def setUp(self):
        self.setup()

    def tearDown(self):
        self.temporary.cleanup()

    def test_precise_stage_unstage_commit_preserves_unrelated_changes(self):
        (self.repo / "one.txt").write_text("changed one\n", encoding="utf-8")
        (self.repo / "two.txt").write_text("unrelated two\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("new feature\n", encoding="utf-8")
        self.apply("git_stage", files=["one.txt", "new.txt"])
        self.assertEqual(set(self.git("diff", "--cached", "--name-only").splitlines()), {"one.txt", "new.txt"})
        self.apply("git_unstage", files=["new.txt"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "one.txt")
        plan = self.prepare("git_commit", message="fix: one file")
        self.assertIn("changed one", plan["description"])
        result = self.workspace.apply(plan["token"])
        self.assertTrue(result["success"])
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "one.txt")
        self.assertEqual((self.repo / "two.txt").read_text(), "unrelated two\n")
        self.assertEqual((self.repo / "new.txt").read_text(), "new feature\n")

    def test_branch_create_with_changes_and_clean_switch(self):
        (self.repo / "one.txt").write_text("pending edit\n", encoding="utf-8")
        self.apply("git_create_branch", branch="feature/topic", checkout=True)
        self.assertEqual(self.git("branch", "--show-current"), "feature/topic")
        self.assertEqual((self.repo / "one.txt").read_text(), "pending edit\n")
        denied = self.workspace.prepare("git_switch_branch", str(self.repo), branch="main")
        self.assertEqual(denied["error"], "git_dirty_worktree")
        self.apply("git_stage", files=["one.txt"])
        self.apply("git_commit", message="feature topic")
        self.apply("git_switch_branch", branch="main")
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual((self.repo / "one.txt").read_text(), "original one\n")

    def test_push_verify_set_upstream_and_fetch_without_merging(self):
        first = self.apply("git_push", remote="origin", branch="main", set_upstream=True)
        self.assertTrue(first["remote_verified"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.repo), self.git("rev-parse", "refs/heads/main", cwd=self.remote))
        self.assertEqual(self.git("config", "branch.main.remote"), "origin")
        self.assertEqual(self.git("config", "branch.main.merge"), "refs/heads/main")
        other = self.root / "other"
        subprocess.run(["git", "clone", "--branch", "main", str(self.remote), str(other)], check=True, capture_output=True)
        (other / "remote.txt").write_text("from remote\n")
        self.git("add", "remote.txt", cwd=other)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "core.hooksPath=" + os.devnull, "commit", "-m", "remote change", cwd=other)
        self.git("push", "origin", "main", cwd=other)
        previous = self.git("rev-parse", "HEAD")
        self.apply("git_fetch", remote="origin")
        self.assertEqual(self.git("rev-parse", "HEAD"), previous)
        self.assertNotEqual(self.git("rev-parse", "refs/remotes/origin/main"), previous)
        self.assertFalse((self.repo / "remote.txt").exists())
        branches = self.workspace.read("git_branches", str(self.repo))
        self.assertIn("behind 1", branches["content"])

    def test_snapshot_changes_and_replay_are_rejected(self):
        (self.repo / "one.txt").write_text("approved\n")
        plan = self.prepare("git_stage", files=["one.txt"])
        (self.repo / "one.txt").write_text("changed after approval\n")
        self.assertEqual(self.workspace.apply(plan["token"])["error"], "git_state_changed")
        self.assertEqual(self.workspace.apply(plan["token"])["error"], "git_plan_expired")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        plan = self.prepare("git_push", remote="origin", branch="main")
        self.git("config", "remote.origin.url", str(self.root / "other.git"))
        self.assertEqual(self.workspace.apply(plan["token"])["error"], "git_state_changed")

    def test_untracked_file_content_changes_invalidate_plan(self):
        (self.repo / "new.txt").write_text("approved\n")
        plan = self.prepare("git_stage", files=["new.txt"])
        (self.repo / "new.txt").write_text("different\n")
        self.assertEqual(self.workspace.apply(plan["token"])["error"], "git_state_changed")

    def test_unsafe_paths_refs_and_remotes_never_execute(self):
        for files in (["../outside"], ["."], [".env"], ["*.txt"], ["one.txt", "one.txt"]):
            self.assertIn("error", self.workspace.prepare("git_stage", str(self.repo), files=files))
        for branch in ("--force", "main~1", "@{-1}", "../other", "refs/heads/../../other"):
            self.assertIn("error", self.workspace.prepare("git_create_branch", str(self.repo), branch=branch))
        self.git("config", "remote.origin.url", "https://secret:password@example.com/repo.git")
        result = self.workspace.prepare("git_push", str(self.repo), remote="origin", branch="main")
        self.assertEqual(result["error"], "git_remote_denied")
        self.assertNotIn("password", json.dumps(self.workspace.read("git_remotes", str(self.repo))))

    def test_sensitive_staged_files_and_in_progress_states_block_commit(self):
        (self.repo / ".env").write_text("fixture-only-secret")
        self.git("add", "-f", ".env")
        self.assertEqual(self.workspace.prepare("git_commit", str(self.repo), message="unsafe")["error"], "git_commit_denied")
        (self.repo / ".git/MERGE_HEAD").write_text(self.git("rev-parse", "HEAD"))
        self.assertEqual(self.workspace.prepare("git_fetch", str(self.repo), remote="origin")["error"], "git_integration_in_progress")

    def test_hooks_are_not_run_and_tokens_expire(self):
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        (self.repo / "one.txt").write_text("pending\n")
        self.apply("git_stage", files=["one.txt"])
        self.apply("git_commit", message="without hooks")
        plan = self.prepare("git_push", remote="origin", branch="main")
        self.workspace.plans[plan["token"]]["expires"] = 0
        self.assertEqual(self.workspace.apply(plan["token"])["error"], "git_plan_expired")

    def test_non_fast_forward_is_rejected_without_overwriting_remote(self):
        self.apply("git_push", remote="origin", branch="main")
        other = self.root / "other"
        subprocess.run(["git", "clone", "--branch", "main", str(self.remote), str(other)], check=True, capture_output=True)
        (other / "other.txt").write_text("remote work\n")
        self.git("add", "other.txt", cwd=other)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "-c", "core.hooksPath=" + os.devnull, "commit", "-m", "remote work", cwd=other)
        self.git("push", "origin", "main", cwd=other)
        remote_before = self.git("rev-parse", "refs/heads/main", cwd=self.remote)
        (self.repo / "one.txt").write_text("local divergent work\n")
        self.apply("git_stage", files=["one.txt"])
        self.apply("git_commit", message="local work")
        plan = self.prepare("git_push", remote="origin", branch="main")
        result = self.workspace.apply(plan["token"])
        self.assertEqual(result["error"], "git_command_failed")
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.remote), remote_before)

    def test_shared_metadata_and_detached_head_are_not_write_targets(self):
        (self.repo / ".git/commondir").write_text("../elsewhere")
        result = self.workspace.prepare("git_stage", str(self.repo), files=["one.txt"])
        self.assertIn("error", result)
        (self.repo / ".git/commondir").unlink()
        self.git("-c", "core.hooksPath=" + os.devnull, "checkout", "--detach")
        result = self.workspace.prepare("git_stage", str(self.repo), files=["one.txt"])
        self.assertEqual(result["error"], "git_detached_head")

    def test_initial_commit_and_unstage_leave_worktree_intact(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.git("init", "-b", "main", cwd=empty)
        self.git("config", "user.name", "Fixture", cwd=empty)
        self.git("config", "user.email", "fixture@example.com", cwd=empty)
        workspace = GitWorkspace([empty], allow_writes=True)
        (empty / "new.txt").write_text("initial file\n")
        for name in ("git_stage", "git_unstage", "git_stage"):
            plan = workspace.prepare(name, str(empty), files=["new.txt"])
            self.assertNotIn("error", plan)
            result = workspace.apply(plan["token"])
            self.assertNotIn("error", result)
            self.assertTrue((empty / "new.txt").exists())
        plan = workspace.prepare("git_commit", str(empty), message="initial")
        self.assertNotIn("error", plan)
        result = workspace.apply(plan["token"])
        self.assertTrue(result["success"])
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD", cwd=empty))


class GitGatewayAcceptance(GitFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup()

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_real_mcp_approval_commit_push_and_hidden_apply(self):
        (self.repo / "one.txt").write_text("new implementation\n")
        (self.repo / "two.txt").write_text("unrelated modification\n")
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.repo),), git_write_enabled=True)
        auth = Authorization()
        approve = AsyncMock(return_value=True)
        async with MCPTools(settings, self.root / ".env", auth) as tools:
            self.assertNotIn("mcp_git__git_apply", tools.registry)
            gateway = ToolGateway(auth, tools.registry, approve)
            for name, args in (("git_create_branch", {"branch": "feature/test", "checkout": True}),
                               ("git_stage", {"files": ["one.txt"]}),
                               ("git_commit", {"message": "feat: selected file"}),
                               ("git_push", {"remote": "origin", "branch": "feature/test", "set_upstream": True})):
                outcome = await gateway.execute(ToolAction("mcp_git__" + name, {"repository": str(self.repo), **args}))
                self.assertNotIn("error", outcome.result)
                self.assertTrue(outcome.result["success"])
                self.assertNotIn("token", outcome.result)
            self.assertEqual(approve.await_count, 4)
            self.assertIn("new implementation", approve.await_args_list[2].args[0]["description"])
            self.assertIn(str(self.remote), approve.await_args.args[0]["description"])
            self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "refs/heads/feature/test", cwd=self.remote))
            self.assertEqual((self.repo / "two.txt").read_text(), "unrelated modification\n")

    async def test_decline_missing_callback_and_revocation_never_write(self):
        (self.repo / "one.txt").write_text("pending\n")
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.repo),), git_write_enabled=True)
        auth = Authorization()
        async with MCPTools(settings, self.root / ".env", auth) as tools:
            action = ToolAction("mcp_git__git_stage", {"repository": str(self.repo), "files": ["one.txt"]})
            outcome = await ToolGateway(auth, tools.registry).execute(action)
            self.assertEqual(outcome.result["error"], "authorization_required")
            refuse = AsyncMock(return_value=False)
            gateway = ToolGateway(auth, tools.registry, refuse)
            for _ in range(2):
                outcome = await gateway.execute(action)
                self.assertEqual(outcome.result["error"], "authorization_required")
            self.assertEqual(refuse.await_count, 1)
            self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
            async def revoke(request):
                auth.deny("mcp.git.git_stage")
                return True
            outcome = await ToolGateway(auth, tools.registry, revoke).execute(action)
            self.assertEqual(outcome.result["error"], "authorization_changed")
            self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    async def test_state_changed_while_dialog_open_prevents_execution(self):
        (self.repo / "one.txt").write_text("before approval\n")
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.repo),), git_write_enabled=True)
        auth = Authorization()
        async def approve(request):
            (self.repo / "one.txt").write_text("after approval\n")
            return True
        async with MCPTools(settings, self.root / ".env", auth) as tools:
            result = await ToolGateway(auth, tools.registry, approve).execute(ToolAction("mcp_git__git_stage",
                {"repository": str(self.repo), "files": ["one.txt"]}))
            self.assertEqual(result.result["error"], "git_state_changed")
            self.assertEqual(self.git("diff", "--cached", "--name-only"), "")


class GitApprovalLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_preview_scrolls_and_cancel_stays_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = DianStarApp(Conversation.create(root / "conversations"),
                config_file=root / ".local/config.json", env_file=root / ".env")
            async with app.run_test(size=(80, 24)) as pilot:
                result = []
                async def approve():
                    result.append(await app._approve_permission({"description": "提交差异\n" + "修改内容\n" * 400}))
                worker = app.run_worker(approve())
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, PermissionApproval)
                scroll = app.screen.query_one("#permission-scroll", VerticalScroll)
                self.assertGreater(scroll.max_scroll_y, 0)
                for name in ("permission-confirm", "permission-cancel"):
                    button = app.screen.query_one("#" + name)
                    self.assertLessEqual(button.region.bottom, 24)
                    self.assertLessEqual(button.region.right, 80)
                await pilot.press("escape")
                await worker.wait()
                self.assertEqual(result, [False])
