"""Public build environment, scoped overrides, activation and PATH diagnosis."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.platform.command_environment import command_environment
from dianstar.security.content_guard import ContentScanner
from dianstar.core.types import ToolAction
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.workspace import WorkspaceTools
from tests import test_bash_tools as fixtures


class PublicEnvironmentTests(unittest.TestCase):
    def test_public_build_variables_inherit_but_credentials_and_startup_hooks_do_not(self):
        with patch.dict(os.environ, {"JAVA_HOME": "/fixture/jdk", "RUSTUP_HOME": "/fixture/rust", "CC": "fixture-cc",
                "ACCESS_TOKEN": "fixture-sensitive-env", "BASH_ENV": "startup.sh", "PYTHONPATH": "injected", "NODE_OPTIONS": "--require injected"}, clear=True):
            env = command_environment(ContentScanner())
        self.assertEqual(env["JAVA_HOME"], "/fixture/jdk")
        self.assertEqual(env["CC"], "fixture-cc")
        for key in ("ACCESS_TOKEN", "BASH_ENV", "PYTHONPATH", "NODE_OPTIONS"):
            self.assertNotIn(key, env)

    def test_known_secret_hidden_in_public_variable_is_omitted(self):
        with patch.dict(os.environ, {"JAVA_HOME": "fixture-known-private-value"}, clear=True):
            env = command_environment(ContentScanner(known=("fixture-known-private-value",)))
        self.assertNotIn("JAVA_HOME", env)

    def test_overrides_are_bounded_and_do_not_mutate_host_environment(self):
        with patch.dict(os.environ, {"JAVA_HOME": "/host"}, clear=True):
            first = command_environment(ContentScanner(), {"JAVA_HOME": "/command"})
            second = command_environment(ContentScanner())
            self.assertEqual(os.environ["JAVA_HOME"], "/host")
        self.assertEqual(first["JAVA_HOME"], "/command")
        self.assertEqual(second["JAVA_HOME"], "/host")
        for values in ({"BASH_ENV": "startup.sh"}, {"TOKEN": "value"}, {"JAVA_HOME": "x\0y"}, {"JAVA_HOME": "x\ny"}, {"JAVA_HOME": "x" * 4097}):
            with self.subTest(values=list(values)):
                with self.assertRaises(ValueError):
                    command_environment(ContentScanner(), values)
        with self.assertRaises(ValueError):
            command_environment(ContentScanner(), [])


class CommandEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.BashToolTests.setUp

    async def native(self, argv, **options):
        return (await self.gateway.execute(ToolAction("workspace_run", {"repository": str(self.root), "argv": argv, **options}))).result

    async def bash(self, command, **options):
        return (await self.gateway.execute(ToolAction("shell" if os.name == "nt" else "bash", {"repository": str(self.root), "command": command, **options}))).result

    def python_script(self, program):
        if os.name == "nt":
            return "& '" + str(Path(sys.executable).resolve()).replace("'", "''") + "' -c '" + program.replace("'", "''") + "'"
        return fixtures.python_command(program)

    async def test_both_entrypoints_inherit_host_jdk_without_leaking_secret_environment(self):
        with patch.dict(os.environ, {"JAVA_HOME": str(self.base / "jdk"), "UNRELATED_SECRET": "fixture-private-env"}):
            program = "import os;print(os.environ['JAVA_HOME']);print(os.environ.get('UNRELATED_SECRET','not inherited'))"
            for result in (await self.native([sys.executable, "-c", program]), await self.bash(self.python_script(program))):
                self.assertTrue(result.get("success"), result)
                self.assertIn(str(self.base / "jdk"), result["content"])
                self.assertIn("not inherited", result["content"])
                self.assertIn("JAVA_HOME", result["build_environment_names"])
                self.assertEqual(result["environment_scope"], "command")

    async def test_explicit_environment_is_command_local_for_bash_and_argv(self):
        with patch.dict(os.environ, {"JAVA_HOME": "fixture-host-jdk"}):
            program = "import os;print(os.environ['JAVA_HOME'])"
            first = await self.native([sys.executable, "-c", program], environment={"JAVA_HOME": "fixture-first-jdk"})
            second = await self.bash("Write-Output $env:JAVA_HOME" if os.name == "nt" else "printf '%s' \"$JAVA_HOME\"", environment={"JAVA_HOME": "fixture-second-jdk"})
            last = await self.native([sys.executable, "-c", program])
            self.assertEqual(os.environ["JAVA_HOME"], "fixture-host-jdk")
        self.assertEqual(first["content"].strip(), "fixture-first-jdk")
        self.assertEqual(second["content"].strip(), "fixture-second-jdk")
        self.assertEqual(last["content"].strip(), "fixture-host-jdk")
        self.assertEqual(first["environment_override_names"], ["JAVA_HOME"])
        self.assertIn("仅这条命令", self.approve.call_args_list[0].args[0]["summary"])

    async def test_export_state_does_not_persist_to_next_bash(self):
        with patch.dict(os.environ, {"JAVA_HOME": "fixture-host-jdk"}):
            first = await self.bash("$env:JAVA_HOME='fixture-shell-jdk'; Write-Output $env:JAVA_HOME" if os.name == "nt" else "export JAVA_HOME=fixture-shell-jdk; printf '%s' \"$JAVA_HOME\"")
            second = await self.bash("Write-Output $env:JAVA_HOME" if os.name == "nt" else "printf '%s' \"$JAVA_HOME\"")
        self.assertEqual(first["content"].strip(), "fixture-shell-jdk")
        self.assertEqual(second["content"].strip(), "fixture-host-jdk")

    async def test_missing_command_can_be_located_through_explicit_path_then_build_runs(self):
        # The real installed compiler is outside the initial command PATH;
        # no download or simulated executable is used for this Java build.
        javac = shutil.which("javac")
        if not javac:
            self.skipTest("Real JDK unavailable")
        jdk_bin = Path(javac).resolve().parent
        java = jdk_bin / ("java.exe" if os.name == "nt" else "java")
        if not java.exists():
            self.skipTest("Matching Java runtime unavailable")
        (self.root / "src").mkdir()
        (self.root / "src/Main.java").write_text('public class Main { public static void main(String[] a) { System.out.println(6*7); }}', encoding="utf-8")
        missing = await self.native(["javac", "-version"], environment={"PATH": str(self.root)})
        self.assertEqual(missing["error"], "executable_not_found", missing)
        self.approve.assert_not_awaited()
        environment = {"PATH": str(jdk_bin), "JAVA_HOME": str(jdk_bin.parent)}
        compiled = await self.native(["javac", "-d", "build", "src/Main.java"], environment=environment)
        if os.name == "nt" and compiled.get("error") == "command_runtime_inaccessible":
            from dianstar.platform.windows_acl import WindowsSecurity
            self.assertFalse(WindowsSecurity().can_edit(jdk_bin.parent))
            self.assertIn("未执行", compiled["message"])
            self.assertFalse((self.root / "build/Main.class").exists())
            return  # System-owned SDK refusal is part of the isolation contract.
        self.assertTrue(compiled.get("success"), compiled)
        self.assertEqual(Path(compiled["executable"]), Path(javac).resolve())
        ran = await self.native([str(java), "-cp", "build", "Main"], environment=environment)
        self.assertTrue(ran.get("success"), ran)
        self.assertEqual(ran["content"].strip(), "42")

    async def test_environment_overrides_do_not_inherit_routine_approval(self):
        command = shlex.quote(str(Path(sys.executable).resolve()).replace("\\", "/")) + " -m unittest -q"
        approval = AsyncMock(return_value=False)
        gateway = ToolGateway(self.auth, self.registry, approval)
        result = await gateway.execute(ToolAction("shell" if os.name == "nt" else "bash", {"repository": str(self.root), "command": command,
            "environment": {"JAVA_HOME": "fixture-jdk"}}))
        self.assertEqual(result.result["error"], "authorization_required")
        approval.assert_awaited_once()

    async def test_unknown_or_sensitive_environment_cannot_bypass_hooks(self):
        for values in ({"BASH_ENV": "evil.sh"}, {"TOKEN": "fixture-value"}, {"JAVA_HOME": "fixture-application-key"}):
            with self.subTest(keys=list(values)):
                result = await self.native([sys.executable, "-c", "print('must not run')"], environment=values)
                self.assertIn("error", result)
                self.assertNotIn("fixture-application-key", json.dumps(result))
        self.approve.assert_not_awaited()

    async def test_environment_change_while_awaiting_confirmation_stops_execution(self):
        program = "from pathlib import Path;Path('marker.txt').touch()"
        async def change(request):
            os.environ["JAVA_HOME"] = "fixture-changed-jdk"
            return True
        gateway = ToolGateway(self.auth, self.registry, change)
        with patch.dict(os.environ, {"JAVA_HOME": "fixture-original-jdk"}):
            result = await gateway.execute(ToolAction("workspace_run", {"repository": str(self.root), "argv": [sys.executable, "-c", program]}))
        self.assertEqual(result.result["error"], "workspace_command_changed")
        self.assertFalse((self.root / "marker.txt").exists())

    async def test_parallel_environments_remain_independent(self):
        other = self.base / "other-project"
        other.mkdir()
        self.mcp.git.repositories.add(str(other))
        program = "import os,time;time.sleep(.1);print(os.environ['JAVA_HOME'])"
        with patch.dict(os.environ, {"JAVA_HOME": "fixture-host"}):
            results = await asyncio.gather(*[self.gateway.execute(ToolAction("workspace_run", {"repository": str(root),
                "argv": [sys.executable, "-c", program], "environment": {"JAVA_HOME": value}}))
                for root,value in ((self.root,"fixture-first"),(other,"fixture-second"))], return_exceptions=True)
            self.assertEqual(os.environ["JAVA_HOME"], "fixture-host")
        self.assertTrue(all(not isinstance(result, BaseException) for result in results), results)
        self.assertEqual([r.result["content"].strip() for r in results], ["fixture-first", "fixture-second"])
