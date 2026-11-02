"""Host safety acceptance. Destructive probes touch isolated fixture data only."""
import asyncio
import os
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch

from dianstar.core.types import ToolAction
from dianstar.security.host_commands import inspect_command
from dianstar.security.system_paths import HostSafetyError, check_host_path
from dianstar.tools.local.process_runner import run_process
from dianstar.tools.local.workspace import WorkspaceTools
from dianstar.security.gateway import ToolGateway
from dianstar.security.content_guard import ContentScanner
from dianstar.platform.command_environment import command_environment
from dianstar.security.tool_policy import ToolPolicy
from tests import test_file_tools as fixtures


class HostBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.FileToolTests.setUp(self)
        environment = patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.base / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.tools = WorkspaceTools(self.mcp, self.env)
        self.registry = self.tools.registrations()
        self.gateway = ToolGateway(self.auth, self.registry, self.approve)

    def environment(self):
        return command_environment(ContentScanner())

    @unittest.skipUnless(os.name == "nt", "Native PowerShell isolation")
    async def test_native_powershell_pipeline_cwd_unicode_and_error_stream(self):
        result = await self.gateway.execute(ToolAction("shell", {"repository": str(self.root),
            "command": "Get-Location | Select-Object -ExpandProperty ProviderPath; 'hello' | ForEach-Object { $_.ToUpper() }; Write-Output '诊断'", "timeout_seconds": 10}))
        self.assertTrue(result.result.get("success"), result.result)
        self.assertIn("HELLO", result.result["content"])
        self.assertIn(str(self.root), result.result["content"])
        self.assertIn("诊断", result.result["content"])
        self.assertEqual(result.result["shell"], "powershell")
        self.assertEqual(result.result["isolation"], "windows_appcontainer")

    @unittest.skipUnless(os.name == "nt", "Windows restricted-token acceptance")
    async def test_native_startup_and_print(self):
        for argv in ([str(Path(os.environ["SYSTEMROOT"], "System32/cmd.exe").resolve()), "/c", "echo fixture-native-ok"],
                     [sys.executable, "-I", "-S", "-c", "print('fixture-native-ok')"]):
            with self.subTest(program=argv[0]):
                result = await run_process(argv, self.root, self.environment(), 10)
                self.assertTrue(result["success"], {**result, "output": result["output"].decode(errors="replace")})

    @unittest.skipUnless(os.name == "nt", "Windows restricted-token acceptance")
    async def test_isolated_python_can_write_project_but_not_sibling(self):
        outside = self.base / "outside.txt"
        outside.write_text("fixture user data")
        program = f'''from pathlib import Path
Path("result.txt").write_text("fixture project output")
try:
    Path({str(outside)!r}).unlink()
except PermissionError:
    print("outside-delete-denied")
else:
    raise RuntimeError("outside deletion was allowed")
'''
        from dianstar.platform.command_isolation import WindowsWriteScope
        launch = WindowsWriteScope.launch
        diagnostics = []
        def inspect(scope, argv, environment, **options):
            diagnostics.append(subprocess.run(["icacls", str(scope.root)], capture_output=True).stdout.decode(errors="replace"))
            return launch(scope, argv, environment, **options)
        with patch.object(WindowsWriteScope, "launch", inspect):
            result = await run_process([sys.executable, "-c", program], self.root, self.environment(), 10)
        self.assertTrue(result.get("success"), {**result, "output": result["output"].decode(errors="replace"), "acl": diagnostics})
        self.assertIn(b"outside-delete-denied", result["output"])
        self.assertEqual(outside.read_text(), "fixture user data")
        self.assertTrue((self.root / "result.txt").exists())

    async def test_dangerous_commands_are_rejected_before_approval(self):
        import json
        for argv in (["diskpart"], ["format.com", "C:"], ["pnputil.exe", "/delete-driver", "fixture.inf"],
                     ["sc.exe", "delete", "fixture-driver"], ["reg.exe", "delete", "HKLM\\fixture"],
                     ["runas.exe", "/user:Administrator", "cmd.exe"], ["cmd.exe", "/c", "rd /s /q C:\\"],
                     ["powershell.exe", "-EncodedCommand", "fixture-opaque-text"]):
            with self.subTest(program=argv[0]):
                result = await self.gateway.execute(ToolAction("workspace_run", {"repository": str(self.root), "argv": argv}))
                self.assertEqual(result.result.get("error"), "dangerous_command_denied", result.result)
        self.approve.assert_not_awaited()
        for command in ("Remove-Item -Recurse -Force C:\\", "Format-Volume -DriveLetter C", "Disable-PnpDevice fixture"):
            with self.assertRaises(HostSafetyError):
                inspect_command("powershell", command.encode())

    async def test_host_directories_are_rejected_even_if_backend_registers_them(self):
        if os.name != "nt":
            self.skipTest("Windows host paths")
        for root in (Path("C:/"), Path(os.environ["SYSTEMROOT"]), Path(os.environ["SYSTEMROOT"]) / "System32/drivers"):
            self.mcp.git.repositories.add(str(root))
            result = await self.gateway.execute(ToolAction("write", {"repository": str(root), "path": "fixture-never-created.txt", "content": "fixture"}))
            self.assertEqual(result.result.get("error"), "host_path_denied", result.result)
            result = await self.gateway.execute(ToolAction("workspace_trash_put", {"paths": [str(root)]}))
            self.assertIn("error", result.result)
        self.approve.assert_not_awaited()

    async def test_renamed_shell_honors_legacy_and_current_backend_denials(self):
        if os.name != "nt":
            self.skipTest("PowerShell replacement")
        self.assertNotIn("bash", self.registry)
        self.assertIn("shell", self.registry)
        for policy in ({"bash": ToolPolicy(enabled=False)}, {"shell": ToolPolicy(enabled=False)}, {"workspace_run": ToolPolicy(enabled=False)}):
            gateway = ToolGateway(self.auth, self.registry, self.approve, policies=policy)
            result = await gateway.execute(ToolAction("shell", {"repository": str(self.root), "command": "Write-Output fixture"}))
            self.assertEqual(result.result["error"], "tool_disabled")
        self.approve.assert_not_awaited()

    @unittest.skipUnless(os.name == "nt", "AppContainer write boundary")
    async def test_create_overwrite_rename_delete_and_permission_change_outside_are_denied(self):
        outside = self.base / "outside"
        outside.mkdir()
        victim = outside / "fixture.txt"
        victim.write_text("fixture user baseline")
        program = f'''import os
from pathlib import Path
p=Path({str(victim)!r})
actions=[lambda:p.write_text("wrong"),lambda:p.unlink(),lambda:p.rename(p.with_name("moved")),lambda:p.parent.joinpath("new").write_text("wrong"),lambda:os.chmod(p,0o777)]
for index,action in enumerate(actions):
    try: action()
    except (PermissionError,FileNotFoundError): print("denied",index)
    else: raise RuntimeError("outside action allowed: "+str(index))
'''
        result = await run_process([sys.executable, "-c", program], self.root, self.environment(), 10)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["output"].count(b"denied"), 5)
        self.assertEqual(victim.read_text(), "fixture user baseline")
        self.assertFalse((outside / "new").exists())

    @unittest.skipUnless(os.name == "nt", "AppContainer principal check")
    async def test_even_an_everyone_writable_external_fixture_is_not_writable(self):
        from dianstar.platform.windows_acl import WindowsSecurity
        outside = self.base / "public-fixture"
        outside.mkdir()
        (outside / "keep.txt").write_text("fixture public baseline")
        api = WindowsSecurity()
        before = api.capture_sid(outside, "S-1-1-0")
        try:
            api.edit_acl(outside, "S-1-1-0")
            program = f'''from pathlib import Path
try: Path({str(outside / 'keep.txt')!r}).unlink()
except (PermissionError,FileNotFoundError): print("public-delete-denied")
else: raise RuntimeError("world ACL bypassed boundary")
'''
            result = await run_process([sys.executable, "-c", program], self.root, self.environment(), 10)
            self.assertTrue(result["success"], result)
            self.assertTrue((outside / "keep.txt").exists())
        finally:
            api.restore_sid(outside, "S-1-1-0", before)

    @unittest.skipUnless(os.name == "nt", "AppContainer credential isolation")
    async def test_existing_env_and_external_private_file_are_unreadable_to_commands(self):
        secret = self.root / ".env"
        secret.write_text("FIXTURE=fixture-local-value")
        program = f'''from pathlib import Path
for value in [{str(secret)!r},{str(self.env)!r}]:
    try: Path(value).read_text()
    except (PermissionError,FileNotFoundError): print("read-denied")
    else: raise RuntimeError("protected read allowed")
'''
        from dianstar.platform.command_isolation import WindowsWriteScope
        launch = WindowsWriteScope.launch
        diagnostics = []
        def inspect(scope, argv, environment, **options):
            diagnostics.append(subprocess.run(["icacls", str(secret)], capture_output=True).stdout.decode(errors="replace"))
            diagnostics.append([(entry[0], entry[1], int.from_bytes(entry[4:8], "little")) for entry in scope.api.acl_entries(secret)])
            return launch(scope, argv, environment, **options)
        with patch.object(WindowsWriteScope, "launch", inspect):
            result = await self.gateway.execute(ToolAction("workspace_run", {"repository": str(self.root), "argv": [sys.executable, "-c", program]}))
        self.assertTrue(result.result.get("success"), {**result.result, "acl": diagnostics})
        self.assertEqual(result.result["content"].count("read-denied"), 2)
        self.assertNotIn("fixture-local-value", result.result["content"])
        self.assertEqual(secret.read_text(), "FIXTURE=fixture-local-value")

    @unittest.skipUnless(os.name == "nt", "Inherited AppContainer token")
    async def test_descendant_python_keeps_the_same_file_boundary(self):
        outside = self.base / "outside.txt"
        outside.write_text("fixture baseline")
        child = f'''from pathlib import Path
try: Path({str(outside)!r}).unlink()
except (PermissionError,FileNotFoundError): print("child-denied")
else: raise RuntimeError("escaped child")
'''
        parent = f"import subprocess,sys; p=subprocess.run([sys.executable,'-c',{child!r}],capture_output=True);print(p.stdout.decode());sys.exit(p.returncode)"
        result = await run_process([sys.executable, "-c", parent], self.root, self.environment(), 10)
        self.assertTrue(result["success"], result)
        self.assertIn(b"child-denied", result["output"])
        self.assertTrue(outside.exists())

    @unittest.skipUnless(os.name == "nt", "AppContainer network boundary")
    async def test_default_network_does_not_reach_a_local_fixture_server(self):
        import socket
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            program = f'''import socket
s=None
try:
    s=socket.socket();s.settimeout(1)
    s.connect(("127.0.0.1",{port}))
except OSError as error: print("network-denied",getattr(error,"winerror",None))
else: raise RuntimeError("unexpected network access")
finally:
    if s is not None:s.close()
'''
            result = await run_process([sys.executable, "-c", program], self.root, self.environment(), 10)
        self.assertTrue(result["success"], result)
        self.assertIn(b"network-denied", result["output"])
        self.assertFalse(result["network_allowed"])

    @unittest.skipUnless(os.name == "nt", "SDK is read-only")
    async def test_runtime_read_capability_does_not_grant_toolchain_writes(self):
        sdk = self.base / "fixture-sdk"
        sdk.mkdir()
        data = sdk / "fixture.txt"
        data.write_text("fixture SDK content")
        environment = {**self.environment(), "JAVA_HOME": str(sdk)}
        program = f'''from pathlib import Path
p=Path({str(data)!r});print(p.read_text())
try:p.write_text("wrong")
except PermissionError:print("sdk-write-denied")
else:raise RuntimeError("SDK writable")
'''
        result = await run_process([sys.executable, "-c", program], self.root, environment, 10)
        self.assertTrue(result["success"], result)
        self.assertIn(b"sdk-write-denied", result["output"])
        self.assertEqual(data.read_text(), "fixture SDK content")

    async def test_network_request_never_inherits_routine_automatic_approval(self):
        self.approve.return_value = False
        result = await self.gateway.execute(ToolAction("workspace_run", {"repository": str(self.root),
            "argv": [sys.executable, "-m", "unittest", "-q"], "network": True}))
        self.assertEqual(result.result["error"], "authorization_required")
        self.approve.assert_awaited_once()
        self.assertIn("互联网", self.approve.call_args.args[0]["summary"])

    async def test_failed_isolation_never_falls_back_to_an_unrestricted_target(self):
        from unittest.mock import AsyncMock
        from dianstar.platform.command_isolation import IsolationError
        with patch("dianstar.platform.command_isolation.write_scope", side_effect=IsolationError()), \
                patch("dianstar.tools.local.process_runner._run_process", new_callable=AsyncMock) as execute:
            with self.assertRaises(IsolationError):
                await run_process([sys.executable, "-c", "print('must-not-run')"], self.root, self.environment(), 10)
        execute.assert_not_awaited()

    @unittest.skipUnless(os.name == "nt", "PowerShell failure result")
    async def test_powershell_terminating_error_cannot_be_reported_as_success(self):
        result = await self.gateway.execute(ToolAction("shell", {"repository": str(self.root), "command": "throw 'fixture failure'"}))
        self.assertFalse(result.result["success"], result.result)
        self.assertNotEqual(result.result["exit_code"], 0)
        self.assertIn("fixture failure", result.result["content"])
