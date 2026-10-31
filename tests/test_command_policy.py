"""Routine tests use exact automatic grants, not a blanket command permission."""
import json
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.runtime import run_turn
from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.command_policy import command_plan, routine_validation
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.workspace import WorkspaceTools
from tests.test_permissions import call, reply
from tests.test_mcp_startup import git


class CommandPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "tests").mkdir()
        (self.root / "tests/example.py").write_text("VALUE = 1\n")
        self.python = str(Path(sys.executable).resolve())

    def tearDown(self):
        self.temporary.cleanup()

    def test_common_python_validation_forms_are_routine(self):
        for args in (["unittest", "discover", "-s", "tests", "-q"], ["unittest", "-q"],
                     ["unittest", "tests.test_example", "-v"], ["unittest", "-k", "discover", "tests.test_example"],
                     ["pytest", "-q"], ["pytest", "tests/test_example.py::TestCase::test_example", "-x"],
                     ["pytest", "-k", "not slow", "--maxfail=1", "--tb", "short"]):
            with self.subTest(args=args):
                self.assertTrue(routine_validation([self.python, "-m", *args], self.root))

    def test_shell_scripts_installation_plugins_and_extra_paths_are_not_automatic(self):
        for argv in ([self.python, "-c", "print('hello')"], [self.python, "setup.py", "test"],
                     [self.python, "-m", "pip", "install", "pytest"], [self.python, "-m", "pytest", "--basetemp", str(self.root.parent)],
                     [self.python, "-m", "pytest", "-p", "arbitrary_plugin"], [self.python, "-m", "pytest", "-o", "addopts=--help"],
                     [self.python, "-m", "pytest", "../other"], [self.python, "-m", "unittest", "discover", "-s", "../other"],
                     [self.python, "-m", "pytest", ".local/private_test.py"], [self.python, "-m", "pytest", ".env"],
                     [self.python, "-m", "pytest", "--junitxml=../report.xml"], [self.python, "-m", "unittest", "installed_other.tests"],
                     ["bash", "-c", "python -m pytest -q"], [str(self.root / "python.exe"), "-m", "pytest", "-q"]):
            with self.subTest(argv=argv):
                self.assertFalse(routine_validation(argv, self.root))

    def test_executable_is_resolved_and_fingerprinted_before_execution(self):
        result = command_plan([sys.executable, "-m", "unittest", "-q"], self.root)
        self.assertEqual(result["argv"][0], self.python)
        self.assertTrue(result["routine_validation"])
        self.assertTrue(result["executable_state"])

    def test_the_application_python_in_a_project_virtual_environment_is_supported(self):
        executable = self.root / ".venv/Scripts/python.exe"
        with patch("dianstar.security.command_policy.sys.executable", str(executable)):
            self.assertTrue(routine_validation([str(executable), "-m", "unittest", "-q"], self.root))
        self.assertFalse(routine_validation([str(executable), "-m", "unittest", "-q"], self.root))

    def test_compilation_and_dependency_checks_share_the_validation_policy(self):
        for args in (["compileall", "-q", "tests"], ["compileall", "-qq", "-r", "2", "-j", "2", "tests"],
                     ["compileall", "--invalidation-mode", "checked-hash", "tests"],
                     ["py_compile", "-q", "tests/example.py"], ["pip", "check"]):
            with self.subTest(args=args):
                self.assertTrue(routine_validation([self.python, "-m", *args], self.root))
        plan = command_plan([self.python, "-m", "compileall", "-q", "tests"], self.root)
        self.assertEqual(plan["argv"][-2:], ["-e", str(self.root)])

    def test_compilation_does_not_automatically_read_other_environments_or_file_lists(self):
        for args in (["compileall", "-q"], ["compileall", "-i", "files.txt"], ["compileall", "../other"],
                     ["compileall", "missing-directory"], ["compileall", "-j", "0", "tests"],
                     ["compileall", "-e", "../other", "tests"], ["py_compile", "-"], ["pip", "install", "example"]):
            with self.subTest(args=args):
                self.assertFalse(routine_validation([self.python, "-m", *args], self.root))

    def test_git_whitespace_checks_are_distinct_from_git_modifications(self):
        executable = str(Path(self.python).parent / ("git.exe" if sys.platform == "win32" else "git"))
        for args in (["diff", "--check"], ["diff", "--cached", "--check"]):
            self.assertTrue(routine_validation([executable, *args], self.root))
        for args in (["push"], ["reset", "--hard"], ["diff", "--check", "--output=report.txt"],
                     ["-C", "../other", "diff", "--check"]):
            self.assertFalse(routine_validation([executable, *args], self.root))


class AutomaticValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.authorization = Authorization()
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.root),), workspace_write_enabled=True)
        mcp = SimpleNamespace(settings=settings, git=SimpleNamespace(repositories={str(self.root)}),
                              authorization=self.authorization)
        self.tools = WorkspaceTools(mcp, self.root / ".application-env")
        self.registry = self.tools.registrations()
        self.action = ToolAction("workspace_run", {"repository": str(self.root),
            "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]})
        (self.root / "tests").mkdir()
        (self.root / "tests/test_example.py").write_text(
            "import unittest\nfrom pathlib import Path\n"
            "class Example(unittest.TestCase):\n"
            "    def test_example(self):\n        Path('executed.txt').write_text('validation ran')\n",
            encoding="utf-8")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_real_tests_run_without_dialog_or_callback_and_still_audit(self):
        for approval in (None, AsyncMock(return_value=False)):
            result = await ToolGateway(self.authorization, self.registry, approval).execute(self.action)
            self.assertTrue(result.result["success"], result.result)
            self.assertIn("Ran 1 test", result.result["content"])
            self.assertIn("security_review", result.result)
            self.assertFalse(self.authorization.has_grant("workspace.write"))
            if approval is not None:
                approval.assert_not_awaited()
        self.assertEqual((self.root / "executed.txt").read_text(), "validation ran")

    async def test_screenshot_pytest_command_does_not_request_confirmation(self):
        action = ToolAction("workspace_run", {"repository": str(self.root), "argv": ["python", "-m", "pytest", "-q"]})
        approval = AsyncMock(return_value=False)
        with patch("dianstar.security.command_policy.shutil.which", return_value=sys.executable), \
                patch.object(self.tools, "run", new_callable=AsyncMock, return_value={"success": True}) as run:
            result = await ToolGateway(self.authorization, self.registry, approval).execute(action)
        self.assertTrue(result.result["success"])
        self.assertEqual(run.await_args.args[0]["argv"], [str(Path(sys.executable).resolve()), "-m", "pytest", "-q"])
        approval.assert_not_awaited()

    async def test_screenshot_compileall_runs_without_dialog_and_does_not_execute_source(self):
        (self.root / "dianstar").mkdir()
        source = self.root / "dianstar/example.py"
        source.write_text("raise RuntimeError('compilation must not execute this module')\n")
        approval = AsyncMock(return_value=False)
        action = ToolAction("workspace_run", {"repository": str(self.root),
                           "argv": ["python", "-m", "compileall", "-q", "dianstar", "tests"]})
        with patch("dianstar.security.command_policy.shutil.which", return_value=sys.executable):
            result = await ToolGateway(self.authorization, self.registry, approval).execute(action)
        self.assertTrue(result.result["success"], result.result)
        self.assertTrue(Path(importlib.util.cache_from_source(str(source))).exists())
        self.assertFalse((self.root / "executed.txt").exists())
        self.assertIn("security_review", result.result)
        approval.assert_not_awaited()

    async def test_compilation_reports_syntax_errors_without_prompting(self):
        source = self.root / "tests/broken.py"
        source.write_text("def broken(:\n")
        approval = AsyncMock(return_value=False)
        action = ToolAction("workspace_run", {"repository": str(self.root),
                           "argv": [sys.executable, "-m", "py_compile", "tests/broken.py"]})
        result = await ToolGateway(self.authorization, self.registry, approval).execute(action)
        self.assertFalse(result.result["success"])
        self.assertIn("SyntaxError", result.result["content"])
        approval.assert_not_awaited()

    async def test_git_whitespace_check_runs_with_git_tool_configuration_guards(self):
        git(self.root, "init")
        git(self.root, "add", "tests/test_example.py")
        git(self.root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-m", "fixture baseline")
        git(self.root, "config", "diff.external", "fixture-must-not-run")
        git(self.root, "config", "core.fsmonitor", "fixture-must-not-run")
        source = self.root / "tests/test_example.py"
        source.write_text(source.read_text() + "# trailing whitespace   \n")
        approval = AsyncMock(return_value=False)
        action = ToolAction("workspace_run", {"repository": str(self.root), "argv": ["git", "diff", "--check"]})
        result = await ToolGateway(self.authorization, self.registry, approval).execute(action)
        self.assertFalse(result.result["success"])
        self.assertIn("trailing whitespace", result.result["content"])
        self.assertNotIn("fixture-must-not-run", result.result["content"])
        approval.assert_not_awaited()

    async def test_git_filter_configuration_is_not_executed_by_automatic_checks(self):
        git(self.root, "init")
        git(self.root, "config", "filter.fixture.clean", "fixture-must-not-run")
        approval = AsyncMock(return_value=True)
        action = ToolAction("workspace_run", {"repository": str(self.root), "argv": ["git", "diff", "--check"]})
        with patch.object(self.tools, "run", new_callable=AsyncMock) as run:
            result = await ToolGateway(self.authorization, self.registry, approval).execute(action)
        self.assertEqual(result.result["error"], "workspace_preparation_failed")
        run.assert_not_awaited()
        approval.assert_not_awaited()

    async def test_backend_can_force_confirmation_or_disable_tests(self):
        for key in ("workspace_run", "workspace.write"):
            approval = AsyncMock(return_value=False)
            result = await ToolGateway(self.authorization, self.registry, approval,
                policies={key: ToolPolicy(requires_approval=True)}).execute(self.action)
            self.assertEqual(result.result["error"], "authorization_required")
            approval.assert_awaited_once()
            self.assertFalse((self.root / "executed.txt").exists())
            result = await ToolGateway(self.authorization, self.registry, approval,
                policies={key: ToolPolicy(enabled=False)}).execute(self.action)
            self.assertEqual(result.result["error"], "tool_disabled")

    async def test_denied_and_closed_requests_do_not_run_automatic_tests(self):
        self.authorization.deny("workspace.write")
        result = await ToolGateway(self.authorization, self.registry).execute(self.action)
        self.assertEqual(result.result["error"], "authorization_denied")
        self.authorization.close()
        result = await ToolGateway(self.authorization, self.registry).execute(self.action)
        self.assertIn("error", result.result)
        self.assertFalse((self.root / "executed.txt").exists())

    async def test_other_commands_still_require_confirmation(self):
        action = ToolAction("workspace_run", {"repository": str(self.root),
                           "argv": [sys.executable, "-c", "print('generic command')"]})
        result = await ToolGateway(self.authorization, self.registry).execute(action)
        self.assertEqual(result.result["error"], "authorization_required")
        approval = AsyncMock(return_value=True)
        result = await ToolGateway(self.authorization, self.registry, approval).execute(action)
        self.assertTrue(result.result["success"])
        approval.assert_awaited_once()

    async def test_preflight_and_executable_change_still_stop_execution(self):
        with patch.object(self.tools, "root", side_effect=ValueError("invalid project")):
            result = await ToolGateway(self.authorization, self.registry).execute(self.action)
        self.assertEqual(result.result["error"], "workspace_preparation_failed")
        helper = self.root / "helper.exe"
        helper.write_bytes(b"fixture executable before confirmation")
        action = ToolAction("workspace_run", {"repository": str(self.root), "argv": [str(helper)]})
        async def change_executable(request):
            helper.write_bytes(b"fixture executable replaced while confirmation was open")
            return True
        with patch.object(self.tools, "run", new_callable=AsyncMock) as run:
            result = await ToolGateway(self.authorization, self.registry, change_executable).execute(action)
        self.assertEqual(result.result["error"], "workspace_command_changed")
        run.assert_not_awaited()

    async def test_model_cannot_supply_its_own_approval_decision(self):
        action = ToolAction("workspace_run", {**self.action.arguments, "auto_approve": True})
        result = await ToolGateway(self.authorization, self.registry).execute(action)
        self.assertEqual(result.result["error"], "invalid_tool_arguments")

    async def test_runtime_runs_routine_validation_without_a_confirmation_callback(self):
        git(self.root, "init")
        env = self.root / ".env"
        env.write_text("DEEPSEEK_API_KEY=fixture-model-key\n")
        history = []
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("workspace_run", self.action.arguments), call("mcp_status", {}), reply("项目测试通过。")])
            await run_turn("运行当前项目测试", env_file=env, project_directory=self.root, history=history)
        result = next(json.loads(m["content"]) for m in history if m["role"] == "tool")
        self.assertTrue(result["success"], result)
        status = [json.loads(m["content"]) for m in history if m["role"] == "tool"][-1]
        self.assertTrue(status["tool_policies"]["workspace_run"]["conditional_approval"])
