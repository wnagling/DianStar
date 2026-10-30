"""Automatic discovery and per-operation clone approval with real local Git/MCP."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.mcp_startup import github_repository, project_repositories, resolve_connections
from dianstar.app.runtime import run_turn
from dianstar.config.mcp import MCPSettings, load_mcp_settings, normalize, save_mcp_settings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.mcp.client import MCPTools
from dianstar.tools.mcp.servers.git.workspace import GitWorkspace
from tests.test_permissions import call, reply


def git(root, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, *args], cwd=root,
                          capture_output=True, check=True, text=True).stdout.strip()


class AutomaticConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "project"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "remote", "add", "origin", "https://github.com/Owner/Project.git")
        self.env = self.root / ".env"

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_configuration_autoconnects_without_persisting_grants(self):
        path = self.root / ".local/mcp.json"
        raw = load_mcp_settings(path)
        settings, status = resolve_connections(raw, self.env, self.repo)
        self.assertTrue(settings.git_enabled)
        self.assertTrue(settings.git_write_enabled)
        self.assertTrue(settings.workspace_write_enabled)
        self.assertEqual(settings.git_repositories, (str(self.repo),))
        self.assertEqual(settings.github_repositories, ("owner/project",))
        self.assertEqual(settings.github_private_repositories, ())
        self.assertEqual(settings.github_write_repositories, ("owner/project",))
        self.assertEqual(settings.github_admin_repositories, ("owner/project",))
        self.assertEqual(status["github"]["state"], "not_configured")
        self.assertFalse(path.exists())
        self.assertEqual(normalize({}), raw)

    def test_old_disable_readonly_and_repository_scopes_survive(self):
        raw = normalize({"git_enabled": False, "github_enabled": False, "git_write_enabled": False})
        settings, status = resolve_connections(raw, self.env, self.repo)
        self.assertFalse(settings.git_enabled)
        self.assertFalse(settings.github_enabled)
        self.assertFalse(settings.git_write_enabled)
        self.assertEqual(status["git"]["state"], "disabled")
        raw = replace(raw, git_enabled=True, git_repositories=(str(self.root),))
        settings, _ = resolve_connections(raw, self.env, self.repo)
        self.assertEqual(settings.git_repositories, (str(self.root),))
        self.assertFalse(settings.git_write_enabled)

    def test_git_missing_is_actionable_and_does_not_launch_or_change_settings(self):
        with patch("dianstar.app.mcp_startup.shutil.which", return_value=None):
            settings, status = resolve_connections(MCPSettings(), self.env, self.repo)
        self.assertFalse(settings.git_enabled)
        self.assertEqual(status["git"]["state"], "not_installed")
        self.assertIn("安装", status["git"]["message"])

    def test_github_uses_configured_credentials_automatically_but_never_implicitly_uses_git_login(self):
        raw = MCPSettings(github_executable=str(self.root / "server.exe"))
        with patch("dianstar.tools.mcp.credentials.git_credential") as credential, \
                patch.dict(os.environ, {"GITHUB_PERSONAL_ACCESS_TOKEN": ""}):
            settings, status = resolve_connections(raw, self.env, self.repo)
            self.assertFalse(settings.github_enabled)
            self.assertIn("GitHub 登录凭据", status["github"]["missing"])
            credential.assert_not_called()
            self.env.write_text("GITHUB_PERSONAL_ACCESS_TOKEN=fixture-only-token\n")
            settings, _ = resolve_connections(raw, self.env, self.repo)
            self.assertTrue(settings.github_enabled)
            self.assertTrue(settings.git_write_enabled)
            self.assertEqual(settings.github_write_repositories, ("owner/project",))
            credential.assert_not_called()

    def test_origin_parsing_rejects_credentials_foreign_hosts_and_path_tricks(self):
        for url in ("git@github.com:Owner/Project.git", "ssh://git@github.com/Owner/Project.git", "https://github.com/Owner/Project"):
            git(self.repo, "remote", "set-url", "origin", url)
            self.assertEqual(github_repository(self.repo), "owner/project")
        for url in ("https://token@github.com/Owner/Project", "https://github.com.evil.test/Owner/Project", "https://github.com/../Project", "https://github.com/Owner/Project?token=secret"):
            git(self.repo, "remote", "set-url", "origin", url)
            self.assertIsNone(github_repository(self.repo))
        child = self.repo / "src"
        child.mkdir()
        self.assertEqual(project_repositories(child), (str(self.repo),))
        self.assertEqual(project_repositories(self.root), ())

    def test_worktree_discovery_requires_reciprocal_metadata(self):
        (self.repo / "README.md").write_text("fixture project\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture")
        target = self.root / "linked"
        git(self.repo, "worktree", "add", "-b", "fixture-linked", str(target))
        self.assertEqual(project_repositories(target), (str(self.repo), str(target)))
        metadata = self.repo / ".git/worktrees/linked/gitdir"
        metadata.write_text(str(self.root / "other/.git"))
        self.assertEqual(project_repositories(target), ())

    def test_github_inference_is_independent_of_local_git_service(self):
        self.env.write_text("GITHUB_PERSONAL_ACCESS_TOKEN=fixture-only-token\n")
        settings, _ = resolve_connections(MCPSettings(git_enabled=False,
            github_executable=str(self.root / "server.exe")), self.env, self.repo)
        self.assertFalse(settings.git_enabled)
        self.assertTrue(settings.github_enabled)
        self.assertEqual(settings.git_repositories, ())
        self.assertEqual(settings.github_repositories, ("owner/project",))

    def test_auto_mode_round_trips_and_preserves_confirmation_policy(self):
        path = self.root / "mcp.json"
        raw = normalize({"git_enabled": None, "github_enabled": None})
        self.assertEqual(save_mcp_settings(raw, path), load_mcp_settings(path))
        self.assertIsNone(load_mcp_settings(path).git_enabled)
        self.assertIsNone(load_mcp_settings(path).git_write_enabled)
        self.assertIsNone(load_mcp_settings(path).workspace_write_enabled)


class AutomaticRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        (self.repo / "README.md").write_text("local clone acceptance fixture\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture")
        self.env = self.root / ".env"
        self.env.write_text("DEEPSEEK_API_KEY=fixture-model-key\n")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_first_question_has_real_git_without_configuration_and_advertises_approval_tools(self):
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("mcp_status", {}), call("mcp_git__git_status", {"repository": str(self.repo)}), reply("读取完成。")])
            history = []
            await run_turn("检查当前项目", env_file=self.env, project_directory=self.repo, history=history)
            names = {t["function"]["name"] for t in create.await_args_list[0].kwargs["tools"]}
        self.assertTrue({"mcp_git__git_status", "mcp_git__git_clone", "workspace_read"} <= names)
        self.assertTrue({"mcp_git__git_commit", "mcp_git__git_push", "workspace_run", "workspace_edit"} <= names)
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertEqual(results[0]["connection_modes"]["git"], "automatic")
        self.assertEqual(results[0]["servers"]["git"]["state"], "connected")
        self.assertNotIn("error", results[1])
        self.assertFalse((self.root / ".local/mcp.json").exists())

    async def test_clone_approval_creates_only_target_and_persists_no_write_grants(self):
        target = self.root / "cloned"
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("mcp_git__git_clone", {"repository": str(target), "url": str(self.repo)}),
                call("workspace_read", {"repository": str(target), "path": "README.md"}), reply("克隆完成。")])
            approval = AsyncMock(return_value=True)
            history = []
            await run_turn("克隆到指定目录并读取 README", env_file=self.env, project_directory=self.repo,
                           on_permission=approval, history=history)
        approval.assert_awaited_once()
        self.assertEqual(json.loads(approval.await_args.args[0]["description"])["仓库"], str(target))
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertTrue(results[0]["success"])
        self.assertTrue(results[0]["configuration_saved"])
        self.assertIn("local clone acceptance fixture", results[1]["content"])
        saved = load_mcp_settings(self.root / ".local/mcp.json")
        self.assertIsNone(saved.git_enabled)
        self.assertIsNone(saved.git_write_enabled)
        self.assertIsNone(saved.workspace_write_enabled)
        self.assertFalse(saved.github_use_git_credentials)
        self.assertEqual(saved.git_workspace_roots, ())
        self.assertEqual(saved.git_repositories, (str(target),))
        self.assertEqual(git(target, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "HEAD"))
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("mcp_git__git_status", {"repository": str(target)}), reply("下轮仍可读取。")])
            history = []
            await run_turn("读取上轮项目", env_file=self.env, project_directory=self.root, history=history)
        result = next(json.loads(m["content"]) for m in history if m["role"] == "tool")
        self.assertNotIn("error", result)

    async def test_declined_clone_cannot_create_register_or_save(self):
        target = self.root / "declined"
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("mcp_git__git_clone", {"repository": str(target), "url": str(self.repo)}), reply("已取消。")])
            approval = AsyncMock(return_value=False)
            history = []
            await run_turn("克隆项目", env_file=self.env, project_directory=self.repo, on_permission=approval, history=history)
        approval.assert_awaited_once()
        self.assertFalse(target.exists())
        self.assertFalse((self.root / ".local/mcp.json").exists())
        result = next(json.loads(m["content"]) for m in history if m["role"] == "tool")
        self.assertEqual(result["error"], "authorization_required")

    async def test_explicit_disabled_configuration_is_not_overridden(self):
        save_mcp_settings(MCPSettings(git_enabled=False, github_enabled=False), self.root / ".local/mcp.json")
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[call("mcp_status", {}), reply("已停用。")])
            history = []
            await run_turn("检查连接", env_file=self.env, project_directory=self.repo, history=history)
            names = {t["function"]["name"] for t in create.await_args_list[0].kwargs["tools"]}
        self.assertFalse(any(n.startswith(("mcp_git__", "workspace_")) for n in names))
        result = next(json.loads(m["content"]) for m in history if m["role"] == "tool")
        self.assertEqual(result["servers"]["git"]["state"], "disabled")

    async def test_clone_only_server_rejects_other_writes_and_configured_root_escape(self):
        settings, _ = resolve_connections(MCPSettings(git_write_enabled=False, workspace_write_enabled=False,
                                                      git_workspace_roots=(str(self.repo),)), self.env, self.repo)
        auth = Authorization()
        async with MCPTools(settings, self.env, auth, allow_clone=True) as tools:
            approve = AsyncMock(return_value=True)
            gateway = ToolGateway(auth, tools.registry, approve)
            result = await gateway.execute(ToolAction("mcp_git__git_clone", {"repository": str(self.root / "outside"), "url": str(self.repo)}))
            self.assertEqual(result.result["error"], "mcp_repository_denied")
            approve.assert_not_awaited()
            denied = await tools.connections["git"].call("git_init", {"repository": str(self.repo / "new")})
            self.assertTrue(denied.isError)
        workspace = GitWorkspace([self.repo], allow_clone=True)
        self.assertIn("error", workspace.prepare("git_stage", str(self.repo), files=["README.md"]))
        self.assertIn("error", workspace.prepare("git_init", str(self.root / "new")))
        self.assertIn("error", workspace.prepare("git_clone", str(self.repo), url=str(self.repo)))

    async def test_clone_destination_state_and_single_use_token_still_checked(self):
        workspace = GitWorkspace([self.repo], allow_clone=True)
        target = self.root / "changed-target"
        preview = workspace.prepare("git_clone", str(target), url=str(self.repo))
        self.assertTrue(preview["prepared"])
        target.mkdir()
        (target / "user.txt").write_text("keep my work")
        self.assertEqual(workspace.apply(preview["token"])["error"], "git_state_changed")
        self.assertIn("error", workspace.apply(preview["token"]))
        self.assertEqual((target / "user.txt").read_text(), "keep my work")
        self.assertNotIn(target, workspace.repositories)

    async def test_invalid_configuration_is_fail_closed(self):
        path = self.root / ".local/mcp.json"
        path.parent.mkdir()
        path.write_text("{broken")
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[call("mcp_status", {}), reply("配置无效。")])
            history = []
            await run_turn("检查连接", env_file=self.env, project_directory=self.repo, history=history)
            names = {t["function"]["name"] for t in create.await_args_list[0].kwargs["tools"]}
        self.assertFalse(any(n.startswith(("mcp_git__", "workspace_")) for n in names))
        result = next(json.loads(m["content"]) for m in history if m["role"] == "tool")
        self.assertEqual(result["servers"]["configuration"]["state"], "invalid")
        self.assertEqual(path.read_text(), "{broken")

    async def test_switch_from_disabled_to_auto_reconnects_in_same_turn(self):
        path = self.root / ".local/mcp.json"
        save_mcp_settings(MCPSettings(git_enabled=False, github_enabled=False), path)
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("configure_mcp", {"changes": {"git_enabled": None}}),
                call("mcp_git__git_status", {"repository": str(self.repo)}), reply("已自动连接。")])
            history = []
            approve = AsyncMock(return_value=True)
            await run_turn("把本地 Git 连接方式改为自动", env_file=self.env, project_directory=self.repo,
                           history=history, on_permission=approve, allow_backend_configuration=True)
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertTrue(results[0]["reconnected"])
        self.assertNotIn("error", results[1])
        self.assertIsNone(load_mcp_settings(path).git_enabled)
        self.assertIsNone(load_mcp_settings(path).git_write_enabled)
        self.assertEqual(load_mcp_settings(path).git_repositories, ())

    async def test_github_auto_connects_using_saved_program_and_token_without_enable_flag(self):
        from tests.test_github_mcp import official_tools, result
        executable = self.root / "fixture-server.exe"
        executable.touch()
        git(self.repo, "remote", "add", "origin", "git@github.com:Owner/Project.git")
        self.env.write_text("DEEPSEEK_API_KEY=fixture-model-key\nGITHUB_PERSONAL_ACCESS_TOKEN=fixture-only-token\n")
        path = self.root / ".local/mcp.json"
        save_mcp_settings(MCPSettings(git_enabled=False, github_executable=str(executable)), path)
        before = path.read_bytes()
        connection = SimpleNamespace(tools=official_tools(), changed=False, task=SimpleNamespace(done=lambda: False),
            start=AsyncMock(), close=AsyncMock(), call=AsyncMock(return_value=result({"content": "fixture read"})))
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory, \
                patch("dianstar.tools.mcp.client.StdioConnection", return_value=connection), \
                patch.object(MCPTools, "_public_repository", AsyncMock(return_value=True)):
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("mcp_status", {}),
                call("mcp_github__get_file_contents", {"owner": "owner", "repo": "project", "path": "README.md"}), reply("已读取。")])
            history = []
            await run_turn("读取当前项目的 GitHub README", env_file=self.env, project_directory=self.repo, history=history)
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertEqual(results[0]["servers"]["github"]["state"], "connected")
        self.assertEqual(results[0]["github_repositories"], ["owner/project"])
        self.assertNotIn("error", results[1])
        names = {t["function"]["name"] for t in create.await_args_list[0].kwargs["tools"]}
        self.assertIn("mcp_github__create_pull_request", names)
        self.assertIn("github_repository_settings_update", names)
        connection.start.assert_awaited_once()
        self.assertEqual(path.read_bytes(), before)
