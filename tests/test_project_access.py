"""Project selection preserves policy and rejects stale approval proposals."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from dianstar.app.project_access import registrations
from dianstar.config.projects import ProjectSettings, load_project_settings, save_project_settings
from dianstar.tools.local.project_context import ProjectContext
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
        self.path = self.root / "projects.json"
        self.projects = ProjectContext(Authorization())
        self.reconnect = AsyncMock()
        self.args = {"path": str(self.project)}

    def tool(self, current=None, question=""):
        return registrations(self.path, self.projects, self.reconnect, lambda: current or self.root, question=question)["open_project"]

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
        self.reconnect.assert_awaited_once_with(self.project)

    async def test_quoted_path_with_spaces_is_temporary_and_preserves_saved_policy(self):
        project = self.root / "project with spaces"
        (project / ".git").mkdir(parents=True)
        before = ProjectSettings(write_enabled=False)
        save_project_settings(before, self.path)
        raw = self.path.read_bytes()
        tool = self.tool(question=f'请帮我打开 "{project}"')
        result = await ToolGateway(Authorization(), {"open_project": tool}).execute(ToolAction("open_project", {"path": str(project)}))
        self.assertTrue(result.result["success"])
        self.assertEqual(self.path.read_bytes(), raw)
        self.reconnect.assert_awaited_once_with(project)

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

    async def test_forced_approval_is_preserved(self):
        question = f"打开 {self.project}"
        tool = self.tool(question=question)
        approve = AsyncMock(return_value=False)
        await ToolGateway(Authorization(), {"open_project": tool}, approve,
                          policies={"project.open": ToolPolicy(requires_approval=True)}).execute(ToolAction("open_project", self.args))
        approve.assert_awaited_once()

    async def test_explicit_request_still_respects_disabled_and_revoked_access(self):
        save_project_settings(ProjectSettings(enabled=False), self.path)
        tool = self.tool(question=f"打开 {self.project}")
        result = await ToolGateway(Authorization(), {"open_project": tool}).execute(ToolAction("open_project", self.args))
        self.assertEqual(result.result["error"], "project_open_denied")
        save_project_settings(ProjectSettings(), self.path)
        tool = self.tool(question=f"打开 {self.project}")
        auth = Authorization()
        auth.deny("project.open")
        result = await ToolGateway(auth, {"open_project": tool}).execute(ToolAction("open_project", self.args))
        self.assertEqual(result.result["error"], "authorization_denied")
        self.reconnect.assert_not_awaited()

    async def test_remembers_only_project_while_preserving_backend_limits(self):
        before = ProjectSettings(write_enabled=False)
        save_project_settings(before, self.path)
        tool = self.tool()
        await tool.approval_description(self.args)
        result = await tool.handler(ToolAction("open_project", self.args))
        self.assertTrue(result.result["success"])
        saved = load_project_settings(self.path)
        self.assertEqual(saved.roots, (str(self.project),))
        self.assertFalse(saved.write_enabled)
        self.reconnect.assert_awaited_once_with(self.project)

    async def test_configuration_change_while_waiting_invalidates_proposal(self):
        save_project_settings(ProjectSettings(), self.path)
        tool = self.tool()
        await tool.approval_description(self.args)
        changed = self.path.read_bytes() + b"\n"
        self.path.write_bytes(changed)
        result = await tool.handler(ToolAction("open_project", self.args))
        self.assertEqual(result.result["error"], "project_configuration_changed")
        self.assertEqual(self.path.read_bytes(), changed)
        self.reconnect.assert_not_awaited()

    async def test_current_project_with_explicit_missing_scope_can_be_opened(self):
        save_project_settings(ProjectSettings(), self.path)
        tool = self.tool(current=self.project)
        await tool.approval_description(self.args)
        result = await tool.handler(ToolAction("open_project", self.args))
        self.assertTrue(result.result["success"])
        self.assertIn(str(self.project), load_project_settings(self.path).roots)

    async def test_explicit_disabled_local_policy_is_not_enabled_by_opening(self):
        save_project_settings(ProjectSettings(enabled=False), self.path)
        with self.assertRaises(ToolPreparationError) as error:
            await self.tool().approval_description(self.args)
        self.assertEqual(error.exception.result["error"], "project_open_denied")
        self.assertFalse(load_project_settings(self.path).enabled)
        self.reconnect.assert_not_awaited()

    async def test_creation_does_not_overwrite_target_created_during_approval(self):
        target = self.root / "new-project"
        args = {"path": str(target), "create": True}
        tool = self.tool()
        await tool.approval_description(args)
        target.mkdir()
        (target / "user.txt").write_text("keep")
        result = await tool.handler(ToolAction("open_project", args))
        self.assertIn("error", result.result)
        self.assertEqual((target / "user.txt").read_text(), "keep")
        self.reconnect.assert_not_awaited()

    async def test_creation_respects_local_readonly_and_creation_scope(self):
        args = {"path": str(self.root / "new-project"), "create": True}
        for settings in (ProjectSettings(write_enabled=False),
                         ProjectSettings(workspace_roots=(str(self.project),))):
            save_project_settings(settings, self.path)
            with self.assertRaises(ToolPreparationError):
                await self.tool().approval_description(args)
        self.assertFalse(Path(args["path"]).exists())

    async def test_temporary_open_applies_saved_readonly_without_lifting_live_restriction(self):
        for saved, live in ((False, True), (True, False)):
            save_project_settings(ProjectSettings(write_enabled=saved), self.path)
            self.projects.settings = ProjectSettings(write_enabled=live)
            tool = self.tool(question=f"打开 {self.project}")
            result = await ToolGateway(Authorization(), {"open_project": tool}).execute(ToolAction("open_project", self.args))
            self.assertTrue(result.result["success"], result.result)
            self.assertFalse(self.projects.settings.write_enabled)
