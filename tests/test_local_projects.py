"""Local tools operate on ordinary directories with no Git or MCP service."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.project_context import create_project_context
from dianstar.app.project_access import registrations
from dianstar.config.mcp import MCPSettings, save_mcp_settings
from dianstar.config.projects import ProjectSettings, load_project_settings, save_project_settings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.project_context import ProjectContext
from dianstar.tools.local.workspace import WorkspaceTools


class LocalProjectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "plain-project"
        self.root.mkdir()
        self.env = self.base / ".env"
        self.env.write_text("DEEPSEEK_API_KEY=fixture-local-key\n")
        environment = patch.dict(os.environ, DIANSTAR_FILE_STATE_ROOT=str(self.base / "state"))
        environment.start()
        self.addCleanup(environment.stop)
        self.auth = Authorization()
        self.projects = ProjectContext(self.auth, roots=(self.root,))
        self.workspace = WorkspaceTools(self.projects, self.env, conversation_id="fixture-local")
        self.addCleanup(self.workspace.storage.manager.close)
        self.approve = AsyncMock(return_value=True)
        self.gateway = ToolGateway(self.auth, self.workspace.registrations(), self.approve)

    async def call(self, name, **args):
        return (await self.gateway.execute(ToolAction(name, {"repository": str(self.root), **args}))).result

    async def test_non_git_files_edit_move_patch_and_restore_without_program_discovery(self):
        with patch("shutil.which", return_value=None):
            created = await self.call("write", path="a.py", content="value = 1\n")
            self.assertTrue(created["success"], created)
            self.assertIn("value = 1", (await self.call("read", path="a.py"))["content"])
            edited = await self.call("edit", path="a.py", old_string="1", new_string="2")
            self.assertTrue(edited["success"], edited)
            restored = await self.call("file_restore", transaction_id=edited["transaction_id"])
            self.assertTrue(restored["success"], restored)
            await self.call("read", path="a.py")
            moved = await self.call("move", source="a.py", destination="src/a.py")
            self.assertTrue(moved["success"], moved)
            await self.call("read", path="src/a.py")
            result = await self.call("patch", patch="*** Begin Patch\n*** Update File: src/a.py\n@@\n-value = 1\n+value = 3\n*** End Patch")
            self.assertTrue(result["success"], result)
        self.assertEqual((self.root / "src/a.py").read_text(), "value = 3\n")
        self.assertFalse((self.root / ".git").exists())

    async def test_project_ignore_rules_work_without_git_metadata_or_program(self):
        (self.root / ".gitignore").write_text("*.tmp\n!keep.tmp\nignored/\n/root-only.txt\n\\#literal.txt\n")
        for name in ("keep.tmp", "skip.tmp", "root-only.txt", "#literal.txt", "plain.py"):
            (self.root / name).write_text("needle")
        (self.root / "sub").mkdir()
        (self.root / "sub/.gitignore").write_text("*.py\n!keep.py\n")
        for name in ("keep.py", "skip.py", "root-only.txt"):
            (self.root / "sub" / name).write_text("needle")
        (self.root / "ignored").mkdir()
        (self.root / "ignored/private.txt").write_text("needle")
        with patch("shutil.which", return_value=None):
            result = await self.call("glob", pattern="**/*")
            search = await self.call("grep", pattern="needle", literal=True)
        self.assertNotIn("error", result, result)
        for output in (result["content"], search["content"]):
            for included in ("keep.tmp", "sub/keep.py", "sub/root-only.txt", "plain.py"):
                self.assertIn(included, output)
            for excluded in ("skip.tmp", "sub/skip.py", "#literal.txt", "private.txt"):
                self.assertNotIn(excluded, output)

    async def test_ignore_failure_is_reported_and_linked_rules_are_not_followed(self):
        outside = self.base / "outside-ignore"
        outside.write_text("*.py\n")
        os.link(outside, self.root / ".gitignore")
        result = await self.call("glob", pattern="**/*")
        self.assertEqual(result["error"], "file_scan_incomplete")
        (self.root / ".gitignore").unlink()
        (self.root / ".gitignore").write_bytes(b"\xff\xfe\x00")
        result = await self.call("grep", pattern="fixture", literal=True)
        self.assertEqual(result["error"], "file_scan_incomplete")

    async def test_inaccessible_directory_is_not_reported_as_no_matches(self):
        def denied_walk(path, **kwargs):
            kwargs["onerror"](PermissionError("fixture directory denied"))
            return iter(())
        with patch("dianstar.tools.local.files.catalog.os.walk", side_effect=denied_walk):
            result = await self.call("glob", pattern="**/*")
        self.assertEqual(result["error"], "file_scan_incomplete")

    async def test_native_program_and_powershell_run_in_plain_project_without_git(self):
        if os.name != "nt":
            self.skipTest("Windows isolated execution")
        (self.root / "main.py").write_text("print('fixture-independent-local')\n")
        with patch("shutil.which", return_value=None):
            result = await self.call("workspace_run", argv=[sys.executable, "main.py"])
            shell = await self.call("shell", command="Get-Location | Select-Object -ExpandProperty ProviderPath")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["isolation_profile"], "lpac")
        self.assertIn("fixture-independent-local", result["content"])
        self.assertTrue(shell["success"], shell)
        self.assertIn(str(self.root), shell["content"])

    async def test_revocation_readonly_and_disable_still_block_execution(self):
        async def readonly(request):
            self.projects.settings = replace(self.projects.settings, write_enabled=False)
            return True
        self.gateway.request_approval = readonly
        result = await self.call("write", path="blocked.py", content="fixture")
        self.assertEqual(result["error"], "local_project_readonly")
        self.assertFalse((self.root / "blocked.py").exists())
        registry = self.workspace.registrations()
        self.assertIn("read", registry)
        self.assertNotIn("write", registry)
        self.assertNotIn("workspace_run", registry)
        self.projects.settings = replace(self.projects.settings, enabled=False)
        self.assertEqual(self.workspace.registrations(), {})

    async def test_policy_and_remembered_directories_are_separate_from_mcp(self):
        config = self.base / "projects.json"
        legacy = MCPSettings(git_enabled=False, github_enabled=False, workspace_write_enabled=False)
        projects, status = create_project_context(self.auth, self.root, config, legacy=legacy)
        self.assertTrue(projects.settings.enabled)
        self.assertFalse(projects.settings.write_enabled)
        self.assertIn(str(self.root), projects.roots)
        save_project_settings(ProjectSettings(write_enabled=True), config)
        projects, _ = create_project_context(self.auth, self.root, config, legacy=legacy)
        self.assertTrue(projects.settings.write_enabled)
        self.assertFalse((self.root / ".git").exists())

    async def test_open_and_create_plain_directories_without_git_initialization(self):
        current = self.root
        target = self.base / "created-project"
        async def opened(root):
            nonlocal current
            current = root
        scope = registrations(self.base / "projects.json", self.projects, opened, lambda: current)
        gateway = ToolGateway(self.auth, scope, self.approve)
        result = await gateway.execute(ToolAction("open_project", {"path": str(target), "create": True}))
        self.assertTrue(result.result["success"], result.result)
        self.assertTrue(target.is_dir())
        self.assertFalse((target / ".git").exists())
        self.assertIn(str(target), self.projects.roots)
        self.assertEqual(load_project_settings(self.base / "projects.json").roots, (str(target),))

    async def test_optional_connection_failure_does_not_revoke_opened_project(self):
        target = self.base / "other"
        target.mkdir()
        opened = AsyncMock(side_effect=RuntimeError("fixture transport unavailable"))
        scope = registrations(self.base / "projects.json", self.projects, opened, lambda: self.root,
            question=f"打开 {target}")
        result = await ToolGateway(self.auth, scope).execute(ToolAction("open_project", {"path": str(target)}))
        self.assertTrue(result.result["success"], result.result)
        self.assertIn("warning", result.result)
        self.assertIn(str(target), self.projects.roots)

    async def test_corrupt_local_policy_stops_tools_instead_of_using_mcp_defaults(self):
        config = self.base / "projects.json"
        config.write_text("{fixture-broken-json")
        projects, status = create_project_context(self.auth, self.root, config, legacy=MCPSettings())
        self.assertFalse(projects.settings.enabled)
        self.assertEqual(status["state"], "invalid")

    async def test_runtime_mcp_disabled_and_reconnect_preserve_local_read_snapshots(self):
        from dianstar.app.runtime import run_turn
        from tests.test_permissions import call, reply, with_review, workflow_results
        save_mcp_settings(MCPSettings(git_enabled=False, github_enabled=False), self.base / ".local/mcp.json")
        (self.root / "main.py").write_text("value = 1\n")
        history = []
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=with_review([
                call("read", {"repository": str(self.root), "path": "main.py"}),
                call("configure_mcp", {"changes": {"github_enabled": False}}),
                call("edit", {"repository": str(self.root), "path": "main.py", "old_string": "1", "new_string": "2"}),
                call("tool_status", {}), reply("已修改。")]))
            await run_turn("修改当前项目", env_file=self.env, project_directory=self.root, history=history,
                on_permission=self.approve, allow_backend_configuration=True, conversation_id="fixture-local")
        results = workflow_results(history)
        self.assertTrue(results[1]["reconnected"], results)
        self.assertTrue(results[2]["success"], results)
        self.assertEqual((self.root / "main.py").read_text(), "value = 2\n")
        self.assertFalse((self.root / ".git").exists())
        status = results[3]
        self.assertEqual(status["tool_groups"]["git_mcp"], [])
        self.assertTrue({"read", "write", "shell", "workspace_run"} <= set(status["tools"]))
        for request in create.await_args_list:
            names = {item["function"]["name"] for item in request.kwargs["tools"]}
            self.assertIn("read", names)
            self.assertFalse(any(name.startswith("mcp_git__") for name in names))
