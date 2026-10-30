"""Project selection preserves policy and rejects stale approval proposals."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from dianstar.app.project_access import registrations
from dianstar.config.mcp import MCPSettings, load_mcp_settings, save_mcp_settings
from dianstar.core.types import ToolAction
from dianstar.security.gateway import ToolPreparationError


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

    def tool(self, current=None):
        return registrations(self.path, self.reconnect, lambda: current or self.root)["open_project"]

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
