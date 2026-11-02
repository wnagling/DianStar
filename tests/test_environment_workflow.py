"""Simple project intent, bounded discovery and readable command approvals."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.task_scope import TaskScope, task_git_defaults
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import ToolAction, Observation
from dianstar.platform.toolchains import located
from dianstar.security.approval_summary import approval_summary, approval_details
from dianstar.security.command_summary import activity
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.workspace import WorkspaceTools
from tests import test_file_tools as fixtures


class TaskScopeTests(unittest.TestCase):
    def test_explicit_task_binds_one_project_but_questions_examples_and_negatives_do_not(self):
        base = Path.cwd()
        scope = TaskScope.from_question("在桌面新建 hello-go 项目，用 Go 写并运行 Hello World，告诉我运行结果。", base, desktop=base)
        self.assertEqual(scope.root, base / "hello-go")
        self.assertTrue(scope.run_go)
        self.assertFalse(scope.contains(base))
        for text in ("例如，在桌面新建 hello-go 项目，用 Go 写并运行 Hello World", "在桌面新建 hello-go 项目是什么意思？",
                     "在桌面新建 hello-go 项目，不要运行", "解释怎么在桌面新建 hello-go 项目", "在桌面新建 ../other 项目"):
            self.assertIsNone(TaskScope.from_question(text, base, desktop=base).root, text)

    def test_install_summary_names_go_account_and_download_without_shell_text(self):
        args = {"repository": "project", "command": "scoop install go 2>&1 | tail -20"}
        summary = approval_summary("bash", args, "raw")
        self.assertIn("通过 Scoop 为当前账号安装 Go", summary)
        self.assertIn("下载", summary)
        self.assertNotIn("2>&1", summary)
        self.assertNotIn("tail -20", summary)
        self.assertNotIn("运行内容", summary)
        # Added operations must not disappear behind a benign install label.
        summary = approval_summary("bash", {**args, "command": "scoop install go; rm important"}, "raw")
        self.assertNotIn("是否通过 Scoop", summary)
        self.assertIn("项目脚本", summary)

    def test_raw_details_remain_available_and_temporary_path_not_dumped_in_summary(self):
        args = {"repository": "project", "argv": ["go", "run", "main.go"], "environment": {"PATH": "fixture-long-path"}}
        raw = json.dumps({"参数数组": args["argv"], "本次构建环境": args["environment"]})
        summary = approval_summary("workspace_run", args, raw)
        self.assertIn("Go 项目", summary)
        self.assertNotIn("fixture-long-path", summary)
        self.assertIn("fixture-long-path", approval_details(raw))
        self.assertIn("main.go", approval_details(raw))


class EnvironmentWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.FileToolTests.setUp(self)
        environment = patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.base / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.bind(TaskScope(self.root, True))

    def bind(self, scope):
        self.tools = WorkspaceTools(self.mcp, self.env, task_scope=scope)
        self.registry = self.tools.registrations()
        self.gateway = ToolGateway(self.auth, self.registry, self.approve)

    async def call(self, name, **args):
        return (await self.gateway.execute(ToolAction(name, {"repository": str(self.root), **args}))).result

    async def test_requested_new_project_files_do_not_reconfirm_but_overwrite_and_delete_do(self):
        self.assertTrue((await self.call("write", path="main.go", content="package main\n"))["success"])
        self.approve.assert_not_awaited()
        await self.call("read", path="main.go")
        self.assertTrue((await self.call("write", path="main.go", content="package main // changed\n"))["success"])
        self.assertEqual(self.approve.await_count, 1)
        await self.call("workspace_delete", path="main.go")
        self.assertEqual(self.approve.await_count, 2)

    async def test_other_project_unrecognized_request_and_explicit_backend_approval_still_ask(self):
        self.bind(TaskScope(self.base / "other", True))
        await self.call("write", path="a.txt", content="fixture")
        self.approve.assert_awaited_once()
        self.approve.reset_mock()
        self.bind(TaskScope(self.root, True))
        self.gateway = ToolGateway(self.auth, self.registry, self.approve,
            policies={"write": ToolPolicy(requires_approval=True)})
        await self.call("write", path="b.txt", content="fixture")
        self.approve.assert_awaited_once()

    async def test_automatic_create_does_not_skip_sensitive_content_hook_or_backend_denial(self):
        result = await self.call("write", path="settings.json", content='{"api_key":"fixture-application-key"}')
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertFalse((self.root / "settings.json").exists())
        self.approve.assert_not_awaited()
        self.gateway = ToolGateway(self.auth, self.registry, self.approve, policies={"workspace.write": ToolPolicy(enabled=False)})
        self.assertEqual((await self.call("write", path="main.go", content="package main"))["error"], "tool_disabled")

    async def test_one_structured_missing_program_query_does_not_run_shell_or_installer(self):
        with patch("dianstar.tools.local.environment_inspection.located", return_value=(["fixture-checked-location"], [])), \
                patch("dianstar.tools.local.environment_inspection.run_process", new_callable=AsyncMock) as execute:
            result = await self.call("workspace_environment_inspect", programs=["go"])
        self.assertEqual(result["programs"][0]["state"], "not_found_in_checked_locations")
        self.assertIn("WSL", result["scope"])
        execute.assert_not_awaited()
        self.approve.assert_not_awaited()

    async def test_inspection_runs_only_fixed_version_arguments_and_no_go_download(self):
        candidate = Path(sys.executable).resolve()
        with patch("dianstar.tools.local.environment_inspection.located", return_value=([str(candidate)], [candidate])), \
                patch("dianstar.tools.local.environment_inspection.run_process", new_callable=AsyncMock,
                    return_value={"success": True, "output": b"go version fixture", "timed_out": False}) as execute:
            result = await self.call("workspace_environment_inspect", programs=["go"])
        self.assertEqual(execute.await_args.args[0], [str(candidate), "version"])
        self.assertEqual(execute.await_args.args[2]["GOTOOLCHAIN"], "local")
        self.assertLessEqual(execute.await_args.args[3], 4)
        self.assertEqual(result["programs"][0]["state"], "available")
        self.approve.assert_not_awaited()

    async def test_explicit_approval_disabled_dependencies_and_revocation_apply_to_inspection(self):
        for policies in ({"workspace_run": ToolPolicy(enabled=False)}, {"workspace.write": ToolPolicy(enabled=False)}):
            self.gateway = ToolGateway(self.auth, self.registry, self.approve, policies=policies)
            self.assertEqual((await self.call("workspace_environment_inspect", programs=["go"]))["error"], "tool_disabled")
        self.approve.return_value = False
        self.gateway = ToolGateway(self.auth, self.registry, self.approve,
            policies={"workspace_run": ToolPolicy(requires_approval=True)})
        self.assertEqual((await self.call("workspace_environment_inspect", programs=["go"]))["error"], "authorization_required")
        self.auth.deny("workspace.write")
        self.gateway = ToolGateway(self.auth, self.registry, self.approve)
        self.assertEqual((await self.call("workspace_environment_inspect", programs=["go"]))["error"], "authorization_denied")

    async def test_arbitrary_shell_scripts_are_not_automatically_approved(self):
        self.approve.return_value = False
        result = await self.call("shell" if os.name == "nt" else "bash", command="Write-Output hello > should-not-exist" if os.name == "nt" else "printf hello > should-not-exist")
        self.assertEqual(result["error"], "authorization_required")
        self.assertFalse((self.root / "should-not-exist").exists())

    async def test_project_local_go_named_executable_is_not_a_routine_version_probe(self):
        path = self.root / ("go.exe" if os.name == "nt" else "go")
        path.write_bytes(b"fixture-not-a-real-executable")
        self.approve.return_value = False
        result = await self.call("workspace_run", argv=[str(path), "version"])
        self.assertEqual(result["error"], "authorization_required")

    async def test_go_scope_rejects_module_installs_external_files_and_execution_injection(self):
        scope = self.tools.task_scope
        go = str(self.base / "go.exe")
        (self.root / "main.go").write_text("package main")
        (self.root / ".local").mkdir()
        (self.root / ".local/private.go").write_text("package main")
        self.assertTrue(scope.go_command([go, "run", "."], self.root))
        self.assertTrue(scope.go_command([go, "build", "-o", "hello.exe", "."], self.root))
        for argv in ([go, "install", "example.org/cmd@latest"], [go, "run", "../external.go"],
                     [go, "run", "-exec", "danger", "main.go"], [go, "build", "-toolexec", "danger", "main.go"],
                     [go, "build", "-o", "../outside", "main.go"], [go, "run", ".local/private.go"],
                     [str(self.root / "go.exe"), "run", "main.go"]):
            self.assertFalse(scope.go_command(argv, self.root), argv)

    async def test_real_native_discovery_create_and_run_hello_have_no_redundant_confirmations(self):
        result = await self.call("workspace_environment_inspect", programs=["go"])
        program = result["programs"][0]
        if program["state"] != "available":
            self.skipTest("Actual native Go unavailable")
        executable = program["versions"][0]["executable"]
        source = 'package main\nimport "fmt"\nfunc main() { fmt.Println("Hello World") }\n'
        self.assertTrue((await self.call("write", path="main.go", content=source))["success"])
        ran = await self.call("workspace_run", argv=[executable, "run", "main.go"])
        self.assertTrue(ran.get("success"), ran)
        self.assertEqual(ran["content"].strip(), "Hello World")
        self.assertEqual(ran["execution_kind"], "go_run")
        self.approve.assert_not_awaited()
        await self.call("write", path="go.mod", content="module fixture.test/hello\n")
        ran = await self.call("workspace_run", argv=[executable, "run", "."])
        self.assertTrue(ran.get("success"), ran)
        self.approve.assert_not_awaited()
        # Plain Bash entry also binds the checked executable directly.
        command = shlex.quote(executable.replace("\\", "/")) + " run main.go"
        ran = await self.call("shell" if os.name == "nt" else "bash", command=command)
        self.assertTrue(ran.get("success"), ran)
        self.assertEqual(ran["execution_mode"], "direct_command")
        self.approve.assert_not_awaited()

    async def test_go_run_is_actual_execution_evidence_without_claiming_unit_tests(self):
        from types import SimpleNamespace
        from dianstar.app.change_review import ChangeReview
        review = ChangeReview(lambda: self.tools, SimpleNamespace(id="fixture-task"))
        action = ToolAction("workspace_run", {"repository": str(self.root), "argv": ["go", "run", "."]}, "fixture-run")
        review.observe(Observation(action, {"success": True, "exit_code": 0, "execution_kind": "go_run"}), self.registry["workspace_run"])
        self.assertEqual(review.evidence["fixture-run"]["kind"], "go_run")
        review.receipts.append({"repository": str(self.root), "transaction_id": "fixture-record"})
        self.assertIn("程序运行命令退出成功", review.summary())
        self.assertNotIn("测试命令退出成功", review.summary())


class GitTaskDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_project_init_keeps_preflight_and_backend_forced_approval(self):
        import tempfile
        from dianstar.security.authorization import Authorization
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "hello-go"
            schema = {"type": "function", "function": {"name": "mcp_git__git_init", "parameters": {
                "type": "object", "properties": {"repository": {"type": "string"}}, "required": ["repository"], "additionalProperties": False}}}
            preview = AsyncMock(return_value="初始化新项目")
            async def execute(action):
                return Observation(action, {"success": True})
            registry = task_git_defaults({"mcp_git__git_init": ToolRegistration(schema, "git.write", execute, preview)}, TaskScope(root, True))
            approve = AsyncMock(return_value=False)
            action = ToolAction("mcp_git__git_init", {"repository": str(root)})
            self.assertTrue((await ToolGateway(Authorization(), registry, approve).execute(action)).result["success"])
            preview.assert_awaited_once()
            approve.assert_not_awaited()
            result = await ToolGateway(Authorization(), registry, approve,
                policies={"git.write": ToolPolicy(requires_approval=True)}).execute(action)
            self.assertEqual(result.result["error"], "authorization_required")
            root.mkdir()
            (root / "existing.txt").touch()
            self.assertFalse(registry["mcp_git__git_init"].auto_approve(action.arguments))


class WorkflowUITests(unittest.IsolatedAsyncioTestCase):
    async def test_install_card_is_readable_and_keeps_raw_script_in_details(self):
        import tempfile
        from dianstar.sessions.conversations import Conversation
        from dianstar.ui.tui.app import DianStarApp
        from textual.widgets import Static
        args = {"repository": r"C:\Users\wang\Desktop\hello-go", "command": "scoop install go 2>&1 | tail -20"}
        raw = json.dumps({"操作": "运行项目命令", "参数数组": ["bash", "-c", args["command"]]})
        request = {"description": raw, "summary": approval_summary("bash", args, raw)}
        for size in ((80, 24), (60, 20)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
                async with app.run_test(size=size) as pilot:
                    app.run_worker(app._approve_permission(request))
                    await pilot.pause(.15)
                    screen = app.screen
                    visible = str(screen.query_one("#permission-description", Static).render())
                    self.assertIn("安装 Go", visible)
                    self.assertNotIn("2>&1", visible)
                    self.assertEqual(screen.query_one("#permission-scroll").max_scroll_y, 0)
                    self.assertLessEqual(screen.query_one("#permission-confirm").region.bottom, size[1])
                    await pilot.click("#permission-details-toggle")
                    await pilot.pause()
                    self.assertIn(args["command"], str(screen.query_one("#permission-raw-details", Static).render()))

    async def test_install_progress_remains_visible_while_independent_tool_completes(self):
        import tempfile
        from dianstar.sessions.conversations import Conversation
        from dianstar.ui.tui.app import DianStarApp
        from textual.widgets import Static
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
            async with app.run_test(size=(80, 24)):
                app._tool_progress({"phase": "started", "tool": "bash", "call_id": "install", "activity": "通过 Scoop 为当前账号安装 Go"})
                app._tool_progress({"phase": "started", "tool": "read", "call_id": "read"})
                app._tool_progress({"phase": "completed", "tool": "read", "call_id": "read"})
                self.assertIn("安装 Go", str(app.query_one("#status", Static).render()))
                app._tool_progress({"phase": "completed", "tool": "bash", "call_id": "install"})
                self.assertFalse(app._tool_activities)
