"""Engineering acceptance against disposable real repositories and MCP stdio."""
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from tests.test_git_operations import GitFixture
from dianstar.tools.mcp.servers.git.workspace import GitWorkspace
from dianstar.config.mcp import MCPSettings, normalize, save_mcp_settings, load_mcp_settings
from dianstar.tools.mcp.client import MCPTools
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.core.types import ToolAction


class RepositoryLifecycleTests(GitFixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.workspace = GitWorkspace([self.repo], True, [self.root])

    def tearDown(self):
        self.temporary.cleanup()

    def operate(self, name, repository=None, **args):
        plan = self.workspace.prepare(name, str(repository or self.repo), **args)
        self.assertNotIn("error", plan, plan)
        result = self.workspace.apply(plan["token"])
        self.assertNotIn("error", result, result)
        return result

    def test_clone_registers_repository_and_preserves_source_and_existing_destinations(self):
        target = self.root / "clone"
        result = self.operate("git_clone", target, url=str(self.repo))
        self.assertEqual(result["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual((target / "one.txt").read_text(), "original one\n")
        self.assertNotIn("error", self.workspace.read("git_status", str(target)))
        self.assertEqual(self.workspace.prepare("git_clone", str(target), url=str(self.repo))["error"], "git_destination_exists")
        self.assertIn("error", self.workspace.prepare("git_clone", str(self.root.parent / "outside"), url=str(self.repo)))

    def test_init_configure_identity_and_remote_then_commit(self):
        target = self.root / "new"
        self.operate("git_init", target, branch="main")
        self.operate("git_config_set", target, key="user.name", value="Engineering Fixture")
        self.operate("git_config_set", target, key="user.email", value="engineering@example.com")
        self.operate("git_remote_set", target, remote="origin", url=str(self.remote))
        (target / "new.txt").write_text("new project\n")
        self.operate("git_stage", target, files=["new.txt"])
        self.operate("git_commit", target, message="first commit")
        self.assertEqual(self.git("log", "-1", "--format=%an", cwd=target), "Engineering Fixture")
        config = json.loads(self.workspace.read("git_config_read", str(target))["content"])
        self.assertIn({"key": "user.email", "value": "engineering@example.com", "scope": "local"}, config)
        self.assertEqual(json.loads(self.workspace.read("git_remotes", str(target))["content"])[0]["fetch"], str(self.remote))

    def test_configuration_is_state_bound_local_and_does_not_accept_command_keys(self):
        for key in ("core.hooksPath", "credential.helper", "include.path", "filter.test.clean", "url.test.insteadOf"):
            self.assertIn("error", self.workspace.prepare("git_config_set", str(self.repo), key=key, value="unsafe"))
        plan = self.workspace.prepare("git_config_set", str(self.repo), key="core.autocrlf", value="false")
        self.git("config", "user.name", "Changed elsewhere")
        self.assertEqual(self.workspace.apply(plan["token"])["error"], "git_state_changed")
        self.assertIn("error", self.workspace.apply(plan["token"]))

    def test_remote_branch_checkout_and_upstream_then_history_queries(self):
        self.operate("git_push", branch="main", set_upstream=True)
        self.git("update-ref", "refs/remotes/origin/topic", self.git("rev-parse", "HEAD"))
        self.operate("git_checkout_remote", branch="topic", remote_branch="topic")
        self.assertEqual(self.git("branch", "--show-current"), "topic")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{upstream}"), "origin/topic")
        self.operate("git_set_upstream", branch="topic", remote_branch="main")
        (self.repo / "one.txt").write_text("topic change\n")
        self.operate("git_stage", files=["one.txt"])
        self.operate("git_commit", message="topic change")
        self.assertIn("topic change", self.workspace.read("git_diff_refs", str(self.repo), base="main")["content"])
        self.assertIn("original one", self.workspace.read("git_show", str(self.repo), ref="main", path="one.txt")["content"])
        self.assertIn("error", self.workspace.read("git_show", str(self.repo), ref="--output=outside"))
        self.assertIn("error", self.workspace.read("git_show", str(self.repo), path=".env"))


class GitLifecycleGatewayTests(GitFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup()

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_clone_through_real_stdio_and_same_turn_followup(self):
        settings = MCPSettings(git_enabled=True, git_write_enabled=True, git_repositories=(str(self.repo),), git_workspace_roots=(str(self.root),))
        auth = Authorization()
        async with MCPTools(settings, self.root / ".env", auth) as tools:
            self.assertEqual(tools.status["git"]["state"], "connected")
            gateway = ToolGateway(auth, tools.registry, AsyncMock(return_value=True))
            target = self.root / "gateway-clone"
            result = await gateway.execute(ToolAction("mcp_git__git_clone", {"repository": str(target), "url": str(self.repo), "branch": ""}))
            self.assertNotIn("error", result.result, result.result)
            self.assertEqual(result.result["created_repository"], str(target))
            status = await gateway.execute(ToolAction("mcp_git__git_status", {"repository": str(target)}))
            self.assertNotIn("error", status.result)
            self.assertIn(str(target), tools.information()["git_repositories"])
        auth.close()

    async def test_declined_clone_does_not_create_directory(self):
        settings = MCPSettings(git_enabled=True, git_write_enabled=True, git_repositories=(str(self.repo),), git_workspace_roots=(str(self.root),))
        async with MCPTools(settings, self.root / ".env", Authorization()) as tools:
            target = self.root / "declined-clone"
            gateway = ToolGateway(tools.authorization, tools.registry, AsyncMock(return_value=False))
            value = await gateway.execute(ToolAction("mcp_git__git_clone", {"repository": str(target), "url": str(self.repo)}))
            self.assertEqual(value.result["error"], "authorization_required")
            self.assertFalse(target.exists())


class GitConfigurationTests(unittest.TestCase):
    def test_new_settings_round_trip_and_admin_scope_must_be_allowed(self):
        import tempfile
        from dataclasses import asdict
        with tempfile.TemporaryDirectory() as tmp:
            settings = normalize({"git_enabled": True, "git_workspace_roots": [tmp], "github_use_git_credentials": True,
                "github_repositories": ["owner/repo"], "github_admin_repositories": ["owner/repo"]})
            path = Path(tmp) / "mcp.json"
            self.assertEqual(save_mcp_settings(settings, path), load_mcp_settings(path))
            with self.assertRaises(ValueError):
                normalize({**asdict(settings), "github_admin_repositories": ["other/repo"]})


class GitRecoveryTests(GitFixture, unittest.TestCase):
    setUp = RepositoryLifecycleTests.setUp
    tearDown = RepositoryLifecycleTests.tearDown
    operate = RepositoryLifecycleTests.operate
    def test_fast_forward_pull_updates_files_and_divergence_preserves_local_files(self):
        self.operate("git_push", branch="main", set_upstream=True)
        other = self.root / "other"
        self.operate("git_clone", other, url=str(self.repo))
        self.git("remote", "set-url", "origin", str(self.remote), cwd=other)
        (other / "remote.txt").write_text("remote update\n")
        self.git("add", "remote.txt", cwd=other)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-m", "remote", cwd=other)
        self.git("push", "origin", "main", cwd=other)
        self.operate("git_pull", branch="main")
        self.assertEqual((self.repo / "remote.txt").read_text(), "remote update\n")
        (self.repo / "one.txt").write_text("local change\n")
        self.operate("git_stage", files=["one.txt"])
        self.operate("git_commit", message="local")
        (other / "next.txt").write_text("next\n")
        self.git("add", "next.txt", cwd=other)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-m", "next", cwd=other)
        self.git("push", "origin", "main", cwd=other)
        before = self.git("rev-parse", "HEAD")
        plan = self.workspace.prepare("git_pull", str(self.repo), branch="main")
        self.assertNotIn("error", plan)
        result = self.workspace.apply(plan["token"])
        self.assertIn("error", result)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / "one.txt").read_text(), "local change\n")

    def test_checkpoint_restores_selected_tracked_and_untracked_files_without_clearing_original_changes(self):
        (self.repo / "one.txt").write_text("user baseline\n")
        (self.repo / "new.txt").write_text("untracked baseline\n")
        self.operate("git_stage", files=["one.txt"])
        before_index = self.git("diff", "--cached")
        checkpoint = self.operate("git_checkpoint", message="before agent")
        self.assertEqual(self.git("diff", "--cached"), before_index)
        self.assertEqual((self.repo / "one.txt").read_text(), "user baseline\n")
        (self.repo / "one.txt").write_text("agent modification\n")
        (self.repo / "new.txt").write_text("agent new change\n")
        (self.repo / "two.txt").write_text("unrelated change\n")
        self.operate("git_restore", files=["one.txt", "new.txt"], source=checkpoint["checkpoint"])
        self.assertEqual((self.repo / "one.txt").read_text(), "user baseline\n")
        self.assertEqual((self.repo / "new.txt").read_text(), "untracked baseline\n")
        self.assertEqual((self.repo / "two.txt").read_text(), "unrelated change\n")
        self.assertEqual(self.git("diff", "--cached"), before_index)

    def test_stash_restores_index_and_untracked_files_and_explicit_drop(self):
        (self.repo / "one.txt").write_text("stashed change\n")
        (self.repo / "untracked.txt").write_text("new file\n")
        self.operate("git_stage", files=["one.txt"])
        self.operate("git_stash_save", message="temporary work")
        self.assertFalse((self.repo / "untracked.txt").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.operate("git_stash_apply", stash="stash@{0}")
        self.assertEqual((self.repo / "untracked.txt").read_text(), "new file\n")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "one.txt")
        self.operate("git_stash_drop", stash="stash@{0}")
        self.assertEqual(self.git("stash", "list"), "")

    def test_worktree_is_usable_after_restart_and_dirty_worktree_cannot_be_removed(self):
        target = self.root / "isolated"
        self.operate("git_worktree_add", path=str(target), branch="feature/isolated")
        restarted = GitWorkspace([self.repo, target], True, [self.root])
        self.assertNotIn("error", restarted.read("git_status", str(target)))
        self.workspace = restarted
        (target / "one.txt").write_text("isolated change\n")
        self.assertEqual(self.workspace.prepare("git_worktree_remove", str(self.repo), path=str(target))["error"], "git_dirty_worktree")
        self.operate("git_restore", target, files=["one.txt"])
        self.operate("git_worktree_remove", path=str(target))
        self.assertFalse(target.exists())
        self.assertEqual((self.repo / "one.txt").read_text(), "original one\n")


class GitIntegrationTests(GitFixture, unittest.TestCase):
    setUp = RepositoryLifecycleTests.setUp
    tearDown = RepositoryLifecycleTests.tearDown
    operate = RepositoryLifecycleTests.operate

    def divergent(self):
        self.operate("git_create_branch", branch="topic", checkout=True)
        (self.repo / "one.txt").write_text("topic version\n")
        self.operate("git_stage", files=["one.txt"])
        self.operate("git_commit", message="topic")
        self.operate("git_switch_branch", branch="main")
        (self.repo / "one.txt").write_text("main version\n")
        self.operate("git_stage", files=["one.txt"])
        self.operate("git_commit", message="main")
        return self.git("rev-parse", "HEAD")

    def test_merge_conflict_can_be_inspected_resolved_staged_and_continued(self):
        self.divergent()
        result = self.operate("git_merge", ref="topic")
        self.assertEqual(result["state"], "conflicts")
        self.assertFalse(result["success"])
        self.assertIn("one.txt", self.workspace.read("git_conflicts", str(self.repo))["content"])
        self.assertEqual(self.workspace.prepare("git_conflict_continue", str(self.repo))["error"], "git_unresolved_conflicts")
        (self.repo / "one.txt").write_text("resolved merge\n")
        self.operate("git_stage", files=["one.txt"])
        self.assertEqual(self.operate("git_conflict_continue")["state"], "completed")
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1", "HEAD").split()), 3)

    def test_merge_abort_restores_head_and_working_files(self):
        before = self.divergent()
        self.operate("git_merge", ref="topic")
        self.assertEqual(self.operate("git_conflict_abort")["state"], "completed")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / "one.txt").read_text(), "main version\n")

    def test_rebase_conflict_allows_staging_detached_head_then_continue(self):
        self.divergent()
        self.operate("git_switch_branch", branch="topic")
        result = self.operate("git_rebase", ref="main")
        self.assertEqual(result["state"], "conflicts")
        (self.repo / "one.txt").write_text("resolved rebase\n")
        self.operate("git_stage", files=["one.txt"])
        self.assertEqual(self.operate("git_conflict_continue")["state"], "completed")
        self.assertEqual(self.git("branch", "--show-current"), "topic")
        self.assertEqual(self.git("merge-base", "main", "topic"), self.git("rev-parse", "main"))

    def test_rebase_abort_keeps_original_commits(self):
        self.divergent()
        self.operate("git_switch_branch", branch="topic")
        before = self.git("rev-parse", "HEAD")
        self.operate("git_rebase", ref="main")
        self.operate("git_conflict_abort")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / "one.txt").read_text(), "topic version\n")

    def test_cherry_pick_and_revert_preserve_unrelated_history(self):
        self.operate("git_create_branch", branch="topic", checkout=True)
        (self.repo / "feature.txt").write_text("feature\n")
        self.operate("git_stage", files=["feature.txt"])
        commit = self.operate("git_commit", message="feature")["commit"]
        self.operate("git_switch_branch", branch="main")
        self.operate("git_cherry_pick", ref=commit)
        self.assertEqual((self.repo / "feature.txt").read_text(), "feature\n")
        self.operate("git_revert", ref="HEAD")
        self.assertFalse((self.repo / "feature.txt").exists())
        self.assertEqual((self.repo / "one.txt").read_text(), "original one\n")

    def test_branch_rename_delete_and_unmerged_rejection(self):
        self.operate("git_create_branch", branch="temporary")
        self.operate("git_branch_rename", branch="temporary", new_branch="renamed")
        self.operate("git_branch_delete", branch="renamed")
        self.assertNotIn("renamed", self.git("branch", "--list"))
        self.divergent()
        plan = self.workspace.prepare("git_branch_delete", str(self.repo), branch="topic")
        self.assertIn("error", self.workspace.apply(plan["token"]))
        self.assertIn("topic", self.git("branch", "--list"))

    def test_external_merge_driver_is_never_executed(self):
        marker = self.root / "driver-marker"
        self.git("config", "merge.custom.driver", "echo unsafe > " + str(marker))
        result = self.workspace.prepare("git_merge", str(self.repo), ref="main")
        self.assertEqual(result["error"], "git_configuration_denied")
        self.assertFalse(marker.exists())

    def test_remote_branch_deletion_verifies_target_and_preserves_main(self):
        self.operate("git_push", branch="main")
        self.operate("git_push", branch="temporary")
        before = self.git("rev-parse", "refs/heads/main", cwd=self.remote)
        self.operate("git_remote_branch_delete", branch="temporary")
        self.assertEqual(self.git("branch", "--list", "temporary", cwd=self.remote), "")
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.remote), before)

    def test_force_with_lease_requires_exact_remote_head_and_rejects_concurrent_updates(self):
        local = self.divergent()
        topic = self.git("rev-parse", "topic")
        self.git("push", "origin", "topic:main")
        self.operate("git_push", branch="main", force_with_lease=True)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), local)
        plan = self.workspace.prepare("git_push", str(self.repo), branch="main", force_with_lease=True)
        self.assertNotIn("error", plan)
        self.git("update-ref", "refs/heads/main", topic, cwd=self.remote)
        self.assertIn("error", self.workspace.apply(plan["token"]))
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), topic)
