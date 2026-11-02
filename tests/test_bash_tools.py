"""Real Bash/foreground processes and the shared approval/review boundary."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.tool_resources import ToolResources
from dianstar.core.resources import conflicts
from dianstar.core.types import ToolAction
from dianstar.hooks import HookRunner
from dianstar.hooks.registry import HookRegistration
from dianstar.platform.shells import find_bash, find_powershell
from dianstar.security.content_guard import Finding
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.workspace import WorkspaceTools
from tests import test_file_tools as fixtures


def python_command(program):
    return shlex.quote(str(Path(sys.executable).resolve()).replace("\\", "/")) + " -c " + shlex.quote(program)


@unittest.skipIf(os.name == "nt", "Windows now uses isolated PowerShell; covered by test_host_safety")
class BashToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.FileToolTests.setUp(self)
        new_root = self.base / "工程 project"
        self.root.rename(new_root)
        self.root = new_root
        self.mcp.git.repositories = {str(self.root)}
        environment = patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.base / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.tools = WorkspaceTools(self.mcp, self.env)
        self.registry = self.tools.registrations()
        self.gateway = ToolGateway(self.auth, self.registry, self.approve)
        try:
            find_powershell() if os.name == "nt" else find_bash()
        except ValueError:
            self.skipTest("Actual Bash unavailable in this environment")

    async def call(self, command, **options):
        return (await self.gateway.execute(ToolAction("bash", {"repository": str(self.root), "command": command, **options}))).result

    async def test_real_bash_pipeline_conditions_cwd_spaces_unicode_and_stderr(self):
        result = await self.call("pwd; printf 'hello world\\n' | tr a-z A-Z && printf '中文\\n'; printf 'diagnostic\\n' >&2")
        self.assertTrue(result.get("success"), result)
        self.assertIn("工程 project", result["content"])
        self.assertIn("HELLO WORLD", result["content"])
        self.assertIn("中文", result["content"])
        self.assertIn("diagnostic", result["content"])
        self.assertEqual(result["execution_mode"], "shell")
        self.assertEqual(result["shell"], "bash")
        self.assertEqual(result["working_directory"], str(self.root))
        self.approve.assert_awaited_once()

    async def test_pipefail_and_exit_codes_are_real_failures(self):
        result = await self.call("false | cat")
        self.assertFalse(result["success"], result)
        self.assertNotEqual(result["exit_code"], 0)
        result = await self.call("printf 'failure\\n' >&2; exit 7")
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("failure", result["content"])

    async def test_tests_fail_edit_and_same_command_passes_without_extra_test_approval(self):
        (self.root / "calc.py").write_bytes(b"def add(a, b): return a - b\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_calc.py").write_bytes(
            b"import unittest\nfrom calc import add\nclass Calc(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n")
        command = shlex.quote(str(Path(sys.executable).resolve()).replace("\\", "/")) + " -m unittest discover -s tests -q"
        first = await self.call(command)
        self.assertFalse(first["success"], first)
        self.assertEqual(first["validation_kind"], "unittest")
        self.assertEqual(first["execution_mode"], "direct_validation")
        self.assertIsNone(first["shell"])
        self.approve.assert_not_awaited()
        await self.gateway.execute(ToolAction("read", {"repository": str(self.root), "path": "calc.py"}))
        edited = await self.gateway.execute(ToolAction("edit", {"repository": str(self.root), "path": "calc.py",
            "old_string": "a - b", "new_string": "a + b  # fixed"}))
        self.assertTrue(edited.result.get("success"), edited.result)
        second = await self.call(command)
        self.assertTrue(second["success"], second)
        self.assertIn("Ran 1 test", second["content"])
        self.assertEqual(self.approve.await_count, 1)  # Only the actual edit.

    async def test_shell_operators_expansions_and_installs_do_not_inherit_routine_approval(self):
        from dianstar.tools.local.commands import CommandTools
        commands = CommandTools(self.tools)
        for value in ("python -m unittest -q; echo done", "python -m unittest -q && echo done", "echo $(pwd)",
                      "python -m pip install example", "python -m unittest -q > report.txt", "X=1 python -m unittest -q"):
            with self.subTest(value=value):
                self.assertFalse(commands.prepare_bash({"command": value}, self.root)["routine_validation"])
        self.gateway.request_approval = AsyncMock(return_value=False)
        result = await self.call("printf 'must not run' > marker.txt")
        self.assertIn("error", result)
        self.assertFalse((self.root / "marker.txt").exists())

    async def test_backend_denials_and_explicit_approval_are_preserved(self):
        for name in ("bash", "workspace_run", "workspace.write"):
            gateway = ToolGateway(self.auth, self.registry, self.approve, policies={name: ToolPolicy(enabled=False)})
            result = await gateway.execute(ToolAction("bash", {"repository": str(self.root), "command": "printf no"}))
            self.assertEqual(result.result["error"], "tool_disabled")
        gateway = ToolGateway(self.auth, self.registry, self.approve, policies={"bash": ToolPolicy(requires_approval=True)})
        command = shlex.quote(str(Path(sys.executable).resolve()).replace("\\", "/")) + " -m pip check"
        self.approve.return_value = False
        result = await gateway.execute(ToolAction("bash", {"repository": str(self.root), "command": command}))
        self.assertIn("error", result.result)
        self.approve.assert_awaited_once()
        for extra in ({}, {"bash": ToolPolicy(requires_approval=False)}):
            self.approve.reset_mock()
            gateway = ToolGateway(self.auth, self.registry, self.approve,
                policies={"workspace_run": ToolPolicy(requires_approval=True), **extra})
            result = await gateway.execute(ToolAction("bash", {"repository": str(self.root), "command": command}))
            self.assertIn("error", result.result)
            self.approve.assert_awaited_once()

    async def test_missing_bash_blank_and_unofficial_arguments_fail_before_execution(self):
        with patch("dianstar.tools.local.commands.find_bash", side_effect=ValueError("missing")):
            result = await self.call("printf hello")
        self.assertEqual(result["error"], "bash_unavailable")
        for value in (" ", "\0"):
            self.assertIn("error", await self.call(value))
        result = await self.call("printf hello", auto_approve=True)
        self.assertEqual(result["error"], "invalid_tool_arguments")
        self.approve.assert_not_awaited()

    async def test_known_credential_and_hook_failure_stop_before_approval(self):
        result = await self.call("printf 'fixture-application-key'")
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertNotIn("fixture-application-key", json.dumps(result))
        def broken(*args):
            raise RuntimeError("fixture hook error")
        self.tools.security = HookRunner(additional_hooks=(HookRegistration("fixture.command", ("before_command",), broken),))
        result = await self.call("printf hello")
        self.assertEqual(result["error"], "security_scan_incomplete")
        self.approve.assert_not_awaited()

    async def test_routine_classification_cannot_swallow_command_hook_denial(self):
        def deny(context, path, data):
            return [Finding("command", 0, "fixture denial")] if path == "command" else []
        self.tools.security = HookRunner(additional_hooks=(HookRegistration("fixture.argv", ("before_command",), deny),))
        command = shlex.quote(str(Path(sys.executable).resolve()).replace("\\", "/")) + " -m unittest -q"
        result = await self.call(command)
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.approve.assert_not_awaited()

    async def test_output_secrets_are_redacted_and_startup_env_is_not_inherited(self):
        startup = self.base / "startup.sh"
        startup.write_text("printf polluted; touch polluted.txt", encoding="utf-8")
        with patch.dict(os.environ, {"BASH_ENV": str(startup), "UNRELATED_SECRET": "fixture-unrelated-secret"}):
            result = await self.call(python_command("import os;print('ghp_'+'E5f6G7h8'*5);print(os.environ.get('UNRELATED_SECRET','not inherited'))"))
        self.assertTrue(result["success"], result)
        self.assertNotIn("ghp_" + "E5f6G7h8" * 5, result["content"])
        self.assertIn("not inherited", result["content"])
        self.assertNotIn("polluted", result["content"])
        self.assertFalse((self.root / "polluted.txt").exists())

    async def test_command_side_effects_are_audited_but_not_claimed_rolled_back(self):
        result = await self.call(python_command("from pathlib import Path;Path('.env').write_text('API_KEY='+'ghp_'+'D4e5F6g7'*5)"))
        self.assertTrue(result["success"], result)
        self.assertTrue((self.root / ".env").exists())
        self.assertTrue(result["security_review"]["findings"], result)
        self.assertNotIn("ghp_" + "D4e5F6g7" * 5, json.dumps(result))

    async def test_output_budget_stops_process_and_never_claims_success(self):
        result = await self.call(python_command("print('x'*(3*1024*1024))"))
        self.assertTrue(result["output_limit_exceeded"], result)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["success"])
        self.assertLessEqual(len(result["content"]), 24000)

    def child_program(self, *, parent_wait=True):
        child = "import time,pathlib;time.sleep(1.8);pathlib.Path('escaped.txt').write_text('bad');time.sleep(20)"
        return ("import subprocess,sys,pathlib,time;child=subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);"
                "pathlib.Path('ready.txt').write_text(str(child.pid));print('ready',flush=True);" + ("time.sleep(30)" if parent_wait else ""))

    async def test_timeout_terminates_descendants(self):
        result = await self.call(python_command(self.child_program()), timeout_seconds=1)
        self.assertTrue(result["timed_out"], result)
        self.assertFalse(result["success"])
        self.assertTrue((self.root / "ready.txt").exists())
        await asyncio.sleep(1.9)
        self.assertFalse((self.root / "escaped.txt").exists())

    async def test_cancel_terminates_descendants_and_releases_project_lock(self):
        task = asyncio.create_task(self.call(python_command(self.child_program())))
        try:
            for _ in range(150):
                if (self.root / "ready.txt").exists():
                    break
                await asyncio.sleep(.02)
            self.assertTrue((self.root / "ready.txt").exists())
        finally:
            task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = await self.call("printf 'lock released'")
        self.assertTrue(result["success"], result)
        await asyncio.sleep(1.9)
        self.assertFalse((self.root / "escaped.txt").exists())

    async def test_foreground_exit_cleans_children_holding_output_pipe(self):
        start = time.monotonic()
        result = await self.call(python_command(self.child_program(parent_wait=False)), timeout_seconds=5)
        self.assertTrue(result["success"], result)
        self.assertFalse(result["timed_out"])
        self.assertLess(time.monotonic() - start, 4)
        await asyncio.sleep(1.9)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_bash_reserves_entire_project_and_conflicts_with_files(self):
        resources = ToolResources(self.registry, None, None)
        claims = resources(ToolAction("bash", {"repository": str(self.root), "command": "printf hello"}))
        same = resources(ToolAction("read", {"repository": str(self.root), "path": "a.py"}))
        other = resources(ToolAction("write", {"repository": str(self.base / "other"), "path": "a.py"}))
        self.assertTrue(conflicts(claims, same))
        self.assertFalse(conflicts(claims, other))
