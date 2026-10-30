import asyncio
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, Static
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation, ToolAction
from dianstar.security.approval_summary import approval_summary, approval_details
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.sessions.conversations import Conversation
from dianstar.ui.tui.app import DianStarApp
from dianstar.ui.tui.screens.permission_approval import PermissionApproval
from dianstar.tools.mcp.git_contract import WRITE_TOOLS
from dianstar.security.approval_summary import GIT_ACTIONS


ARGS = {"url": "https://github.com/useopendesk/opendesk.git", "repository": r"C:\Users\wang\Desktop\opendesk", "branch": "", "depth": 0}
RAW = json.dumps({"操作": "git_clone", "仓库": ARGS["repository"], "完整参数": ARGS}, ensure_ascii=False, indent=2)


class ApprovalSummaryTests(unittest.TestCase):
    def test_settings_change_names_exact_project_and_omits_unchanged_machine_fields(self):
        raw = json.dumps({"修改前": {"git_enabled": None, "git_repositories": ["existing"], "github_executable": ""},
                          "修改后": {"git_enabled": None, "git_repositories": ["existing", r"C:\Users\wang\Desktop\agentTry"], "github_executable": ""}})
        summary = approval_summary("configure_mcp", {}, raw)
        details = approval_details(raw)
        self.assertIn("新增可访问项目", summary)
        self.assertIn("agentTry", summary)
        self.assertIn("不会提交或推送", summary)
        self.assertNotIn("git_enabled", details)
        self.assertNotIn("existing", details)
        self.assertNotIn("{", details)
    def test_every_git_write_has_a_human_action_label(self):
        self.assertEqual(set(GIT_ACTIONS) | {"git_clone"}, WRITE_TOOLS)

    def test_clone_shows_project_destination_source_and_history_without_json(self):
        summary = approval_summary("mcp_git__git_clone", ARGS, RAW)
        self.assertIn("是否将 opendesk 项目克隆", summary)
        self.assertIn(ARGS["repository"], summary)
        self.assertIn(ARGS["url"], summary)
        self.assertIn("完整提交历史", summary)
        self.assertNotIn("git_clone", summary)
        self.assertNotIn("完整参数", summary)
        self.assertNotIn("{", summary)

    def test_clone_selected_branch_and_shallow_history_are_explicit(self):
        summary = approval_summary("mcp_git__git_clone", {**ARGS, "branch": "release", "depth": 1}, RAW)
        self.assertIn("选择分支：release", summary)
        self.assertIn("最近 1 层", summary)

    def test_force_push_and_destructive_actions_explain_impact(self):
        summary = approval_summary("mcp_git__git_push", {"repository": "project", "branch": "main", "force_with_lease": True}, "地址：https://github.com/owner/repo.git")
        self.assertIn("改写远程分支历史", summary)
        self.assertIn("https://github.com/owner/repo.git", summary)
        self.assertIn("main", summary)
        self.assertIn("可能无法恢复", approval_summary("workspace_delete", {"repository": "project", "path": "notes.md"}, RAW))

    def test_github_public_visibility_and_post_content_are_shown(self):
        summary = approval_summary("github_repository_settings_update", {"owner": "owner", "repo": "repo", "changes": {"visibility": "public"}}, RAW)
        self.assertIn("owner/repo", summary)
        self.assertIn("仓库可见性：公开", summary)
        summary = approval_summary("mcp_github__add_issue_comment", {"owner": "owner", "repo": "repo", "issue_number": 4, "body": "Please check this change"}, RAW)
        self.assertIn("议题编号：4", summary)
        self.assertIn("Please check this change", summary)

    def test_unknown_plain_permission_keeps_its_original_meaning(self):
        self.assertEqual(approval_summary("unknown", {}, "允许本次付费搜索？"), "允许本次付费搜索？")

    def test_review_delete_and_close_are_not_hidden_behind_generic_update(self):
        summary = approval_summary("mcp_github__pull_request_review_write", {"owner": "owner", "repo": "repo", "method": "delete_pending", "pullNumber": 3}, RAW)
        self.assertIn("删除未提交的代码审查", summary)
        summary = approval_summary("mcp_github__issue_write", {"owner": "owner", "repo": "repo", "method": "update", "state": "closed", "issue_number": 2}, RAW)
        self.assertIn("状态：关闭", summary)


class ApprovalCLITests(unittest.TestCase):
    def test_cli_shows_summary_and_only_prints_raw_details_on_request(self):
        from dianstar.ui.cli import _answer
        request = {"description": RAW, "summary": approval_summary("mcp_git__git_clone", ARGS, RAW)}
        for responses, has_details in ((["y"], False), (["d", "y"], True), (["n"], False)):
            async def runner(*args, **kwargs):
                await kwargs["on_permission"](request)
                return "完成"
            stream = io.StringIO()
            with patch("dianstar.ui.cli.ask_and_save", side_effect=runner), patch("dianstar.ui.cli.sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=responses), patch("sys.stdout", stream):
                _answer("克隆项目", conversation=type("ConversationStub", (), {"name": "fixture"})())
            output = stream.getvalue()
            self.assertIn("opendesk 项目克隆", output)
            self.assertEqual("完整参数" in output, has_details)


class ApprovalUITests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_plan_is_compact_with_complete_details_and_one_confirm(self):
        args = {"repository": r"C:\Users\wang\Desktop\agentTry", "files": [f"src/module_{i}.py" for i in range(80)],
                "message": "complete project changes", "branch": "main", "remote": "origin"}
        description = "目标：https://github.com/example/project.git → main\n文件：\n" + "\n".join(args["files"])
        request = {"description": description, "summary": approval_summary("mcp_git__git_publish", args, description)}
        self.assertIn("80 个文件", request["summary"])
        self.assertIn("确认一次", request["summary"])
        self.assertNotIn("module_79", request["summary"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
            choices = []
            async with app.run_test(size=(80, 24)) as pilot:
                async def show():
                    choices.append(await app._approve_permission(request))
                worker = app.run_worker(show())
                await pilot.pause(0.2)
                self.assertLessEqual(app.screen.query_one("#permission-panel").region.height, 18)
                await pilot.click("#permission-details-toggle")
                await pilot.pause()
                self.assertIn("module_79.py", str(app.screen.query_one("#permission-raw-details", Static).render()))
                self.assertLessEqual(app.screen.query_one("#permission-confirm", Button).region.bottom, 24)
                await pilot.click("#permission-confirm")
                await asyncio.wait_for(worker.wait(), 5)
            self.assertEqual(choices, [True])

    async def test_plain_summary_and_collapsed_details_fit_and_cancel_or_confirm(self):
        for size in ((80, 24), (100, 32), (140, 60)):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
                request = {"description": RAW, "summary": approval_summary("mcp_git__git_clone", ARGS, RAW)}
                choices = []
                async with app.run_test(size=size) as pilot:
                    async def show():
                        choices.append(await app._approve_permission(request))
                    for confirm in (False, True):
                        worker = app.run_worker(show())
                        await pilot.pause(0.2)
                        self.assertIsInstance(app.screen, PermissionApproval)
                        self.assertEqual(app.focused.id, "permission-cancel")
                        primary = str(app.screen.query_one("#permission-description", Static).render())
                        self.assertIn("opendesk 项目克隆", primary)
                        self.assertNotIn("完整参数", primary)
                        self.assertLessEqual(app.screen.query_one("#permission-description").region.bottom,
                                             app.screen.query_one("#permission-scroll").region.bottom)
                        panel_height = app.screen.query_one("#permission-panel").region.height
                        self.assertLessEqual(panel_height, 18)
                        self.assertFalse(app.screen.showing_details)
                        await pilot.click("#permission-details-toggle")
                        await pilot.pause()
                        self.assertTrue(app.screen.showing_details)
                        self.assertEqual(app.screen.query_one("#permission-panel").region.height, panel_height)
                        self.assertIn("完整参数", str(app.screen.query_one("#permission-raw-details", Static).render()))
                        for button in app.screen.query(Button):
                            self.assertGreaterEqual(button.region.y, 0)
                            self.assertLessEqual(button.region.bottom, size[1])
                            self.assertLessEqual(button.region.right, size[0])
                        if confirm:
                            await pilot.click("#permission-confirm")
                        else:
                            await pilot.press("escape")
                        await asyncio.wait_for(worker.wait(), 5)
                self.assertEqual(choices, [False, True])

    async def test_gateway_keeps_exact_raw_preview_and_arguments(self):
        schema = {"type": "function", "function": {"name": "mcp_git__git_clone", "parameters": {"type": "object", "properties": {key: {"type": "integer" if key == "depth" else "string"} for key in ARGS}, "required": list(ARGS), "additionalProperties": False}}}
        handler = AsyncMock(side_effect=lambda action: Observation(action, {"success": True}))
        prepare = AsyncMock(return_value=RAW)
        approve = AsyncMock(return_value=True)
        gateway = ToolGateway(Authorization(), {"mcp_git__git_clone": ToolRegistration(schema, "fixture.clone", handler, prepare)}, approve)
        await gateway.execute(ToolAction("mcp_git__git_clone", dict(ARGS)))
        request = approve.await_args.args[0]
        self.assertEqual(request["description"], RAW)
        self.assertIn("opendesk 项目克隆", request["summary"])
        self.assertEqual(handler.await_args.args[0].arguments, ARGS)
