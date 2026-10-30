"""Default tool availability never supplies execution grants or account-wide scope."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from dianstar.app.mcp_startup import resolve_connections
from dianstar.app.runtime import run_turn
from dianstar.config.mcp import MCPSettings, normalize, save_mcp_settings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.github_admin import GitHubAdmin
from dianstar.tools.mcp.client import MCPTools
from tests.test_mcp_startup import git
from tests.test_permissions import call, reply


class DefaultLocalOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_named_project_then_commit_without_internal_configuration_tools(self):
        (self.repo / "README.md").write_text("after\n")
        approve = AsyncMock(return_value=True)
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("open_project", {"path": str(self.repo)}),
                call("mcp_git__git_status", {"repository": str(self.repo)}),
                call("mcp_git__git_stage", {"repository": str(self.repo), "files": ["README.md"]}),
                call("mcp_git__git_commit", {"repository": str(self.repo), "message": "fixture named project"}),
                call("mcp_git__git_push", {"repository": str(self.repo), "branch": "main"}), reply("已完成。")])
            history = []
            await run_turn("提交指定项目", env_file=self.env, project_directory=self.root, on_permission=approve, history=history)
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertTrue(all(not r.get("error") for r in results), results)
        first_request = approve.await_args_list[0].args[0]
        self.assertIn("是否打开 project 项目", first_request["summary"])
        self.assertIn(str(self.repo), first_request["summary"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), git(self.remote, "rev-parse", "refs/heads/main"))
        for request in create.await_args_list:
            names = {s["function"]["name"] for s in request.kwargs["tools"]}
            self.assertNotIn("configure_mcp", names)
            self.assertNotIn("get_mcp_configuration", names)

    async def test_current_project_does_not_request_redundant_access_approval(self):
        approve = AsyncMock(return_value=True)
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("open_project", {"path": str(self.repo)}),
                call("mcp_git__git_status", {"repository": str(self.repo)}), reply("可直接使用。")])
            history = []
            await run_turn("检查当前项目", env_file=self.env, project_directory=self.repo, on_permission=approve, history=history)
        approve.assert_not_awaited()
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertEqual(results[0]["error"], "project_already_open")
        self.assertNotIn("error", results[1])
        self.assertFalse((self.root / ".local/mcp.json").exists())

    async def test_declining_named_project_access_keeps_scope_and_configuration(self):
        approve = AsyncMock(return_value=False)
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("open_project", {"path": str(self.repo)}),
                call("mcp_git__git_status", {"repository": str(self.repo)}), reply("未打开。")])
            history = []
            await run_turn("打开指定项目", env_file=self.env, project_directory=self.root, on_permission=approve, history=history)
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertEqual(results[0]["error"], "authorization_required")
        self.assertEqual(results[1]["error"], "mcp_repository_denied")
        self.assertFalse((self.root / ".local/mcp.json").exists())

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "project"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.test")
        (self.repo / "README.md").write_text("before\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "fixture")
        self.remote = self.root / "remote.git"
        git(self.root, "init", "--bare", str(self.remote))
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "origin", "main")
        self.env = self.root / ".env"
        self.env.write_text("DEEPSEEK_API_KEY=fixture-model-key\n")

    async def test_edit_test_commit_push_without_configuration_requires_each_confirmation(self):
        approval = AsyncMock(return_value=True)
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("workspace_edit", {"repository": str(self.repo), "path": "README.md", "mode": "replace", "old_text": "before", "new_text": "after"}),
                call("workspace_run", {"repository": str(self.repo), "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('README.md').read_text().strip() == 'after'; print('checks passed')"]}),
                call("mcp_git__git_stage", {"repository": str(self.repo), "files": ["README.md"]}),
                call("mcp_git__git_commit", {"repository": str(self.repo), "message": "fixture approved workflow"}),
                call("mcp_git__git_push", {"repository": str(self.repo), "branch": "main"}),
                reply("修改、测试、提交和推送已完成。")])
            history = []
            await run_turn("修改 README，测试后提交并推送当前项目", env_file=self.env,
                           project_directory=self.repo, on_permission=approval, history=history)
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertTrue(all(not r.get("error") for r in results), results)
        self.assertEqual(approval.await_count, 5)
        self.assertIn("checks passed", results[1]["content"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), git(self.remote, "rev-parse", "refs/heads/main"))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertFalse((self.root / ".local/mcp.json").exists())
        self.assertNotIn("configure_mcp", [c["function"]["name"] for m in history if m["role"] == "assistant" for c in m.get("tool_calls", [])])

    async def test_decline_or_missing_callback_cannot_edit_execute_or_push(self):
        head = git(self.remote, "rev-parse", "refs/heads/main")
        for approve in (None, AsyncMock(return_value=False)):
            with self.subTest(approval=approve is not None), patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
                marker = self.repo / "must-not-run.txt"
                factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                    call("workspace_edit", {"repository": str(self.repo), "path": "README.md", "mode": "overwrite", "new_text": "must not save"}),
                    call("workspace_run", {"repository": str(self.repo), "argv": [sys.executable, "-c", "from pathlib import Path; Path('must-not-run.txt').touch()"]}),
                    call("mcp_git__git_push", {"repository": str(self.repo), "branch": "main"}), reply("未批准。")])
                history = []
                await run_turn("修改并推送", env_file=self.env, project_directory=self.repo, on_permission=approve, history=history)
            results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
            self.assertEqual([r.get("error") for r in results], ["authorization_required"] * 3)
            self.assertEqual((self.repo / "README.md").read_text(), "before\n")
            self.assertFalse(marker.exists())
            self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), head)
            self.assertFalse((self.root / ".local/mcp.json").exists())

    async def test_explicit_prohibitions_still_hide_mutating_tools(self):
        path = self.root / ".local/mcp.json"
        save_mcp_settings(MCPSettings(git_write_enabled=False, workspace_write_enabled=False,
                                     github_write_repositories=(), github_admin_repositories=()), path)
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            create = factory.return_value.chat.completions.create = AsyncMock(return_value=reply("只读。"))
            await run_turn("检查工具", env_file=self.env, project_directory=self.repo)
        names = {t["function"]["name"] for t in create.await_args.kwargs["tools"]}
        self.assertIn("mcp_git__git_status", names)
        self.assertFalse({"mcp_git__git_commit", "mcp_git__git_push", "workspace_edit", "workspace_delete", "workspace_run"} & names)

    async def test_init_and_worktree_creation_confirm_only_the_selected_destination(self):
        new = self.root / "new-project"
        linked = self.root / "linked"
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("mcp_git__git_init", {"repository": str(new), "branch": "main"}),
                call("mcp_git__git_worktree_add", {"repository": str(self.repo), "path": str(linked), "branch": "fixture-linked"}),
                call("mcp_git__git_status", {"repository": str(linked)}), reply("两个目标已创建。")])
            history = []
            approve = AsyncMock(return_value=True)
            await run_turn("初始化新项目并为当前项目创建 worktree", env_file=self.env,
                           project_directory=self.repo, on_permission=approve, history=history)
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        self.assertTrue(all(not r.get("error") for r in results), results)
        self.assertTrue((new / ".git").is_dir())
        self.assertTrue((linked / ".git").is_file())
        self.assertEqual(approve.await_count, 2)


class DefaultGitHubOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "project"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "remote", "add", "origin", "https://github.com/owner/project.git")
        self.env = self.root / ".env"
        self.env.write_text("GITHUB_PERSONAL_ACCESS_TOKEN=fixture-only-token\n")
        exe = self.root / "server.exe"
        exe.touch()
        self.settings, _ = resolve_connections(MCPSettings(git_enabled=False, github_executable=str(exe),
            github_repositories=("other/read-only",)), self.env, self.repo)

    async def test_default_scope_is_current_origin_and_old_empty_lists_deny(self):
        self.assertEqual(self.settings.github_write_repositories, ("owner/project",))
        self.assertEqual(self.settings.github_admin_repositories, ("owner/project",))
        self.assertIn("other/read-only", self.settings.github_repositories)
        old = normalize({"git_write_enabled": False, "workspace_write_enabled": False,
                         "github_write_repositories": [], "github_admin_repositories": []})
        effective, _ = resolve_connections(old, self.env, self.repo)
        self.assertFalse(effective.git_write_enabled)
        self.assertFalse(effective.workspace_write_enabled)
        self.assertEqual(effective.github_write_repositories, ())
        self.assertEqual(effective.github_admin_repositories, ())

    async def test_issue_creation_requires_confirmation_and_does_not_expand_to_extra_read_repo(self):
        from tests.test_github_mcp import official_tools, result
        auth = Authorization()
        connection = SimpleNamespace(tools=official_tools(), changed=False, task=SimpleNamespace(done=lambda: False),
            start=AsyncMock(), close=AsyncMock(), call=AsyncMock(return_value=result({"number": 1, "html_url": "https://github.com/owner/project/issues/1"})))
        with patch("dianstar.tools.mcp.client.StdioConnection", return_value=connection), \
                patch.object(MCPTools, "_public_repository", AsyncMock(return_value=True)):
            async with MCPTools(self.settings, self.env, auth) as tools:
                args = {"owner": "owner", "repo": "project", "method": "create", "title": "fixture", "body": "fixture"}
                deny = AsyncMock(return_value=False)
                denied = await ToolGateway(auth, tools.registry, deny).execute(ToolAction("mcp_github__issue_write", args))
                self.assertEqual(denied.result["error"], "authorization_required")
                deny.assert_awaited_once()
                connection.call.assert_not_awaited()
                approve = AsyncMock(return_value=True)
                gateway = ToolGateway(auth, tools.registry, approve)
                accepted = await gateway.execute(ToolAction("mcp_github__issue_write", args))
                self.assertNotIn("error", accepted.result)
                connection.call.assert_awaited_once()
                denied = await gateway.execute(ToolAction("mcp_github__issue_write", {**args, "owner": "other", "repo": "read-only"}))
                self.assertEqual(denied.result["error"], "github_write_disabled")
                self.assertEqual(approve.await_count, 1)

    async def test_administration_checks_actual_admin_permission_and_each_confirmation(self):
        metadata = {"full_name": "owner/project", "private": False, "visibility": "public",
                    "permissions": {"admin": True}, "description": "before"}
        mutations = []
        def respond(request):
            if request.method == "PATCH":
                mutations.append(json.loads(request.content))
                metadata.update(mutations[-1])
            return httpx.Response(200, json=metadata)
        client = httpx.AsyncClient
        auth = Authorization()
        admin = GitHubAdmin(self.settings, self.env, auth)
        action = ToolAction("github_repository_settings_update", {"owner": "owner", "repo": "project", "changes": {"description": "after"}})
        with patch("httpx.AsyncClient", side_effect=lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs)):
            deny = AsyncMock(return_value=False)
            result = await ToolGateway(auth, admin.registrations(), deny).execute(action)
            self.assertEqual(result.result["error"], "authorization_required")
            self.assertEqual(mutations, [])
            metadata["permissions"]["admin"] = False
            approve = AsyncMock(return_value=True)
            result = await ToolGateway(auth, admin.registrations(), approve).execute(action)
            self.assertEqual(result.result["error"], "github_admin_preparation_failed")
            approve.assert_not_awaited()
            metadata["permissions"]["admin"] = True
            result = await ToolGateway(auth, admin.registrations(), approve).execute(action)
            self.assertTrue(result.result["success"], result.result)
            self.assertEqual(mutations, [{"description": "after"}])
            approve.assert_awaited_once()
