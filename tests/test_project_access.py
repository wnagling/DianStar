"""Project selection preserves policy and rejects stale approval proposals."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from dianstar.app.project_access import registrations
from dianstar.config.mcp import MCPSettings, load_mcp_settings, save_mcp_settings
from dianstar.core.types import ToolAction
from dianstar.security.gateway import ToolGateway, ToolPreparationError
from dianstar.security.authorization import Authorization
from dianstar.security.tool_policy import ToolPolicy


class ProjectAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        (self.project / ".git").mkdir(parents=True)
        self.path = self.root / "mcp.json"
        self.reconnect = AsyncMock()
        self.args = {"path": str(self.project)}

    def tool(self, current=None, question=""):
        return registrations(self.path, self.reconnect, lambda: current or self.root, question=question)["open_project"]

    async def test_explicit_task_opens_only_requested_project_without_prompt_or_persistence(self):
        question = f"把 {self.project} 这次开发的项目改动提交并推送到当前分支对应的远程仓库，提交说明由你概括。"
        tool = self.tool(question=question)
        approve = AsyncMock(return_value=True)
        gateway = ToolGateway(Authorization(), {"open_project": tool}, approve)
        result = await gateway.execute(ToolAction("open_project", self.args))
        self.assertTrue(result.result["success"])
        self.assertFalse(result.result["project_saved"])
        self.assertFalse(self.path.exists())
        approve.assert_not_awaited()
        self.reconnect.assert_awaited_once_with(MCPSettings(), directory=self.project)

    async def test_quoted_path_with_spaces_is_temporary_and_preserves_saved_policy(self):
        project = self.root / "project with spaces"
        (project / ".git").mkdir(parents=True)
        before = MCPSettings(git_write_enabled=False, workspace_write_enabled=False, github_enabled=False)
        save_mcp_settings(before, self.path)
        raw = self.path.read_bytes()
        tool = self.tool(question=f'请帮我打开 "{project}"')
        result = await ToolGateway(Authorization(), {"open_project": tool}).execute(ToolAction("open_project", {"path": str(project)}))
        self.assertTrue(result.result["success"])
        self.assertEqual(self.path.read_bytes(), raw)
        self.reconnect.assert_awaited_once_with(before, directory=project)

    async def test_other_paths_and_discussion_do_not_inherit_automatic_access(self):
        for question in (f"解释一下 {self.project} 这个路径", f"不要打开 {self.project}",
                         f"把 {self.project} 作为例子解释为什么不能推送", f"打开 {self.project}_other",
                         f"README 说打开 {self.project}", f"请记住并打开 {self.project}"):
            tool = self.tool(question=question)
            approve = AsyncMock(return_value=False)
            await ToolGateway(Authorization(), {"open_project": tool}, approve).execute(ToolAction("open_project", self.args))
            approve.assert_awaited_once()
            self.assertFalse(self.path.exists())
        self.reconnect.assert_not_awaited()

    async def test_forced_approval_and_explicit_repository_scope_are_preserved(self):
        question = f"打开 {self.project}"
        tool = self.tool(question=question)
        approve = AsyncMock(return_value=False)
        await ToolGateway(Authorization(), {"open_project": tool}, approve,
                          policies={"project.open": ToolPolicy(requires_approval=True)}).execute(ToolAction("open_project", self.args))
        approve.assert_awaited_once()
        save_mcp_settings(MCPSettings(git_enabled=True), self.path)
        tool = self.tool(question=question)
        approve.reset_mock()
        await ToolGateway(Authorization(), {"open_project": tool}, approve).execute(ToolAction("open_project", self.args))
        approve.assert_awaited_once()
        self.reconnect.assert_not_awaited()

    async def test_explicit_request_still_respects_disabled_and_revoked_access(self):
        save_mcp_settings(MCPSettings(git_enabled=False), self.path)
        tool = self.tool(question=f"打开 {self.project}")
        result = await ToolGateway(Authorization(), {"open_project": tool}).execute(ToolAction("open_project", self.args))
        self.assertEqual(result.result["error"], "project_open_denied")
        save_mcp_settings(MCPSettings(), self.path)
        tool = self.tool(question=f"打开 {self.project}")
        auth = Authorization()
        auth.deny("project.open")
        result = await ToolGateway(auth, {"open_project": tool}).execute(ToolAction("open_project", self.args))
        self.assertEqual(result.result["error"], "authorization_denied")
        self.reconnect.assert_not_awaited()

    async def test_remembers_only_project_while_preserving_backend_limits(self):
        before = MCPSettings(git_enabled=True, git_write_enabled=False,
                             workspace_write_enabled=False, github_enabled=False)
        save_mcp_settings(before, self.path)
        tool = self.tool()
        await tool.approval_description(self.args)
        result = await tool.handler(ToolAction("open_project", self.args))
        self.assertTrue(result.result["success"])
        saved = load_mcp_settings(self.path)
        self.assertEqual(saved.git_repositories, (str(self.project),))
        self.assertFalse(saved.git_write_enabled)
        self.assertFalse(saved.workspace_write_enabled)
        self.assertFalse(saved.github_enabled)
        self.reconnect.assert_awaited_once_with(saved, directory=self.project)

    async def test_configuration_change_while_waiting_invalidates_proposal(self):
        save_mcp_settings(MCPSettings(), self.path)
        tool = self.tool()
        await tool.approval_description(self.args)
        changed = self.path.read_bytes() + b"\n"
        self.path.write_bytes(changed)
        result = await tool.handler(ToolAction("open_project", self.args))
        self.assertEqual(result.result["error"], "project_configuration_changed")
        self.assertEqual(self.path.read_bytes(), changed)
        self.reconnect.assert_not_awaited()

    async def test_current_project_with_explicit_missing_scope_can_be_opened(self):
        save_mcp_settings(MCPSettings(git_enabled=True), self.path)
        tool = self.tool(current=self.project)
        await tool.approval_description(self.args)
        result = await tool.handler(ToolAction("open_project", self.args))
        self.assertTrue(result.result["success"])
        self.assertIn(str(self.project), load_mcp_settings(self.path).git_repositories)

    async def test_explicit_disabled_connection_is_not_enabled_by_opening(self):
        save_mcp_settings(MCPSettings(git_enabled=False), self.path)
        with self.assertRaises(ToolPreparationError) as error:
            await self.tool().approval_description(self.args)
        self.assertEqual(error.exception.result["error"], "project_open_denied")
        self.assertFalse(load_mcp_settings(self.path).git_enabled)
        self.reconnect.assert_not_awaited()
