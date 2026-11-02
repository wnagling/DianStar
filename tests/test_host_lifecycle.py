"""Real Windows isolation cleanup, recovery and bounded foreground execution."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch, AsyncMock

from dianstar.core.types import ToolAction
from dianstar.platform.command_isolation import WindowsWriteScope
from dianstar.platform.file_state import state_directory
from dianstar.tools.local.process_runner import run_process
from tests import test_host_safety as safety


@unittest.skipUnless(os.name == "nt", "Windows AppContainer acceptance")
class HostLifecycleTests(unittest.IsolatedAsyncioTestCase):
    setUp = safety.HostBoundaryTests.setUp
    environment = safety.HostBoundaryTests.environment

    async def execute(self, program, **options):
        return (await self.gateway.execute(ToolAction("workspace_run", {"repository": str(self.root),
            "argv": [sys.executable, "-c", program], **options}))).result

    async def test_sensitive_acl_and_root_are_restored_on_success_and_failure(self):
        from dianstar.platform.windows_acl import WindowsSecurity
        api = WindowsSecurity()
        secret = self.root / ".env"
        secret.write_text("FIXTURE=fixture-private")
        before = api.acl_snapshot(secret)
        root_before = api.acl_snapshot(self.root)
        for code in (0, 9):
            result = await self.execute(f"import sys;sys.exit({code})")
            self.assertEqual(result["exit_code"], code)
            self.assertFalse(result.get("isolation_cleanup_pending"))
            self.assertEqual(api.acl_snapshot(secret), before)
            self.assertEqual(api.acl_snapshot(self.root), root_before)
        self.assertEqual(secret.read_text(), "FIXTURE=fixture-private")

    async def test_git_metadata_readable_but_not_writable_or_deletable(self):
        metadata = self.root / ".git"
        metadata.mkdir()
        config = metadata / "config"
        config.write_text("fixture-git-metadata")
        result = await self.execute('''from pathlib import Path
p=Path('.git/config');print(p.read_text())
for action in (lambda:p.write_text('wrong'),lambda:p.unlink(),lambda:p.parent.rename('moved-git')):
    try: action()
    except PermissionError: print('denied')
    else: raise RuntimeError('metadata modification allowed')
''')
        self.assertTrue(result["success"], result)
        self.assertEqual(result["content"].count("denied"), 3)
        self.assertEqual(config.read_text(), "fixture-git-metadata")

    async def test_credential_configuration_is_excluded_but_plain_configuration_remains_usable(self):
        private = self.root / "settings.json"
        private.write_text(json.dumps({"api_key": "fixture-application-key"}))
        public = self.root / "public.json"
        public.write_text(json.dumps({"theme": "dark"}))
        # Known credentials are blocked even when a fixture prefix would
        # normally qualify as a harmless placeholder.
        from dianstar.security.content_guard import ContentScanner
        program = '''from pathlib import Path
print(Path('public.json').read_text())
try: Path('settings.json').read_text()
except PermissionError: print('configuration-denied')
else: raise RuntimeError('credential config readable')
'''
        result = await run_process([sys.executable, "-c", program], self.root, self.environment(), 10,
            scanner=ContentScanner(known=("fixture-application-key",)))
        self.assertTrue(result["success"], result)
        self.assertIn(b"configuration-denied", result["output"])
        self.assertIn(b"dark", result["output"])

    def child_program(self, wait=True):
        child = "import time,pathlib;time.sleep(1.8);pathlib.Path('escaped.txt').write_text('fixture-unexpected');time.sleep(20)"
        return ("import subprocess,sys,pathlib,time;p=subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);"
                "pathlib.Path('ready.txt').touch();print('ready',flush=True);" + ("time.sleep(30)" if wait else ""))

    async def test_timeout_kills_descendants_and_restores_acl(self):
        result = await self.execute(self.child_program(), timeout_seconds=1)
        self.assertTrue(result["timed_out"], result)
        self.assertFalse(result["success"])
        self.assertTrue((self.root / "ready.txt").exists())
        self.assertFalse(result.get("isolation_cleanup_pending"))
        await asyncio.sleep(1.9)
        self.assertFalse((self.root / "escaped.txt").exists())

    async def test_cancel_kills_descendants_and_releases_project_lock(self):
        task = asyncio.create_task(self.execute(self.child_program()))
        try:
            for _ in range(250):
                if (self.root / "ready.txt").exists():
                    break
                await asyncio.sleep(.02)
            self.assertTrue((self.root / "ready.txt").exists())
        finally:
            task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = await self.execute("print('lock released')")
        self.assertTrue(result["success"], result)
        await asyncio.sleep(1.9)
        self.assertFalse((self.root / "escaped.txt").exists())

    async def test_foreground_exit_does_not_leave_child_or_output_pipe(self):
        result = await self.execute(self.child_program(wait=False), timeout_seconds=5)
        self.assertTrue(result["success"], result)
        self.assertLess(result["command_duration_ms"], 4000)
        await asyncio.sleep(1.9)
        self.assertFalse((self.root / "escaped.txt").exists())

    async def test_shell_timeout_kills_the_whole_tree(self):
        executable = str(Path(sys.executable).resolve()).replace("'", "''")
        (self.root / "fixture_parent.py").write_text(self.child_program())
        result = await self.gateway.execute(ToolAction("shell", {"repository": str(self.root),
            "command": f"& '{executable}' fixture_parent.py", "timeout_seconds": 2}))
        self.assertTrue(result.result["timed_out"], result.result)
        await asyncio.sleep(1.9)
        self.assertFalse((self.root / "escaped.txt").exists())

    async def test_output_budget_stops_execution(self):
        result = await self.execute("print('x'*(3*1024*1024))")
        self.assertTrue(result["output_limit_exceeded"], result)
        self.assertFalse(result["success"])
        self.assertLessEqual(len(result["content"]), 24000)

    async def test_target_cannot_spoof_a_launcher_startup_error(self):
        result = await self.execute("from pathlib import Path;import sys;Path('ran.txt').touch();print('DIANSTAR_ISOLATION_ERROR:fixture-fake:before:5:1');sys.exit(125)")
        self.assertTrue((self.root / "ran.txt").exists())
        self.assertEqual(result["exit_code"], 125)
        self.assertNotIn("error", result)
        self.assertFalse(result["success"])

    async def test_authenticated_launcher_execution_failure_is_uncertain_not_unexecuted(self):
        async def incomplete(*args, **kwargs):
            config = json.loads(kwargs["launch_input"])
            return {"exit_code": 125, "output": ("DIANSTAR_ISOLATION_ERROR:" + config["nonce"] + ":execution:fixture:1").encode(),
                    "success": False, "duration_ms": 1, "timed_out": False, "output_limit_exceeded": False}
        with patch("dianstar.tools.local.process_runner._run_process", side_effect=incomplete):
            result = await run_process([sys.executable, "-c", "pass"], self.root, self.environment(), 5)
        self.assertEqual(result["error"], "command_execution_incomplete")
        self.assertIn("可能已发生修改", result["message"])
        self.assertNotIn(b"DIANSTAR_ISOLATION_ERROR", result["output"])

    async def test_process_crash_permission_journal_is_recovered_before_next_command(self):
        from dianstar.platform.windows_acl import WindowsSecurity
        api = WindowsSecurity()
        secret = self.root / ".env"
        secret.write_text("FIXTURE=fixture-private")
        before = api.acl_snapshot(secret)
        code = ("import os,sys;from dianstar.platform.command_isolation import WindowsWriteScope;"
                f"s=WindowsWriteScope({str(self.root)!r}).start(sys.executable,{{}});os._exit(0)")
        completed = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1], env=dict(os.environ), capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertNotEqual(api.acl_snapshot(secret), before)
        result = await self.execute("print('recovered')")
        self.assertTrue(result["success"], result)
        self.assertFalse(result.get("isolation_cleanup_pending"))
        self.assertEqual(api.acl_snapshot(secret), before)
        self.assertFalse(list((state_directory() / "command-security").glob("*.json")))

    async def test_application_state_cannot_be_registered_as_a_project(self):
        root = state_directory()
        root.mkdir(parents=True, exist_ok=True)
        self.mcp.git.repositories.add(str(root))
        result = await self.gateway.execute(ToolAction("write", {"repository": str(root), "path": "fixture.txt", "content": "fixture"}))
        self.assertEqual(result.result["error"], "host_path_denied")
        self.approve.assert_not_awaited()

    async def test_external_acl_change_is_preserved_and_blocks_unverified_recovery(self):
        from dianstar.platform.windows_acl import WindowsSecurity
        api = WindowsSecurity()
        secret = self.root / ".env"
        secret.write_text("FIXTURE=fixture-private")
        before = api.acl_snapshot(secret)
        original = WindowsWriteScope.launch
        changed = []
        def launch(scope, *args, **options):
            api.edit_acl(secret, "S-1-1-0", permissions=0x120089, inherit=False)
            changed.append(api.acl_snapshot(secret))
            return original(scope, *args, **options)
        try:
            with patch.object(WindowsWriteScope, "launch", launch):
                result = await self.execute("print('fixture finished')")
            self.assertTrue(result["success"], result)
            self.assertTrue(result["isolation_cleanup_pending"], result)
            self.assertEqual(api.acl_snapshot(secret), changed[0])
            retry = await self.execute("from pathlib import Path;Path('must-not-run').touch()")
            self.assertEqual(retry["error"], "command_isolation_unavailable")
            self.assertFalse((self.root / "must-not-run").exists())
        finally:
            api.set_acl(secret, [bytes.fromhex(item) for item in before["entries"]], protected=before["protected"])
        result = await self.execute("print('recovery confirmed')")
        self.assertTrue(result["success"], result)
        self.assertFalse(result.get("isolation_cleanup_pending"))

    async def test_network_permission_changes_only_the_internet_capability(self):
        source = Path(__file__).resolve().parents[1] / "dianstar/platform/windows_acl.py"
        program = source.read_text(encoding="utf-8") + '''
api=WindowsSecurity();token=W.HANDLE()
api.require(api.advapi.OpenProcessToken(api.kernel.GetCurrentProcess(),8,ctypes.byref(token)))
try:
    print('internet-capability',any(sid=='S-1-15-3-1' for sid,flags in api.token_sids(token,30)))
finally: api.kernel.CloseHandle(token)
'''
        for allowed in (False, True):
            result = await run_process([sys.executable, "-c", program], self.root, self.environment(), 10, network=allowed)
            self.assertTrue(result["success"], result)
            self.assertIn(("internet-capability " + str(allowed)).encode(), result["output"])

    def test_credentials_and_broad_sdk_locations_do_not_expand_access(self):
        from dianstar.security.system_paths import check_host_path, HostSafetyError
        from dianstar.platform.command_reads import runtime_roots
        for name in (".ssh", ".aws", ".azure", ".gnupg", ".kube", ".codex"):
            with self.subTest(name=name), self.assertRaises(HostSafetyError):
                check_host_path(Path.home() / name, project=True)
        roots = runtime_roots(sys.executable, self.root, {"JAVA_HOME": str(Path.home()), "RUSTUP_HOME": str(state_directory())})
        self.assertNotIn(Path.home(), roots)
        self.assertFalse(any(state_directory().is_relative_to(path) for path in roots))
        self.assertNotIn(self.root, runtime_roots(sys.executable, self.root, {"JAVA_HOME": str(self.root)}))

    async def test_public_application_package_acl_does_not_expand_the_write_scope(self):
        from dianstar.platform.windows_acl import WindowsSecurity
        api = WindowsSecurity()
        outside = self.base / "all-packages-fixture"
        outside.mkdir()
        before = api.capture_sid(outside, "S-1-15-2-1")
        try:
            api.edit_acl(outside, "S-1-15-2-1")
            result = await self.execute(f'''from pathlib import Path
try: Path({str(outside / 'must-not-exist')!r}).touch()
except PermissionError: print('package-write-denied')
else: raise RuntimeError('public app package grant escaped scope')
''')
            self.assertTrue(result["success"], result)
            self.assertFalse((outside / "must-not-exist").exists())
        finally:
            api.restore_sid(outside, "S-1-15-2-1", before)

    async def test_isolated_child_cannot_duplicate_the_unrestricted_host_token(self):
        program = f'''import ctypes
from ctypes import wintypes as W
k=ctypes.WinDLL('kernel32',use_last_error=True);a=ctypes.WinDLL('advapi32',use_last_error=True)
k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE
k.CloseHandle.argtypes=[W.HANDLE]
a.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,ctypes.POINTER(W.HANDLE)];a.OpenProcessToken.restype=W.BOOL
p=k.OpenProcess(0x1000,False,{os.getpid()});token=W.HANDLE()
try:
    granted=bool(p and a.OpenProcessToken(p,0xA,ctypes.byref(token)))
    if granted: raise RuntimeError('host token duplication was allowed')
    print('host-token-denied')
finally:
    if token: k.CloseHandle(token)
    if p:k.CloseHandle(p)
'''
        result = await self.execute(program)
        self.assertTrue(result["success"], result)
        self.assertIn("host-token-denied", result["content"])

    async def test_other_project_waits_for_cold_sdk_setup_instead_of_using_short_file_timeout(self):
        import threading
        import time
        from dianstar.platform.command_reads import allow_runtime
        other = self.base / "other-project"
        other.mkdir()
        entered = threading.Event()
        original = WindowsWriteScope.__init__
        def short_file_lock(scope, *args, **options):
            original(scope, *args, **options)
            scope.locks.timeout = .05
        def cold(*args, **options):
            if not entered.is_set():
                entered.set()
                time.sleep(.2)
            return allow_runtime(*args, **options)
        with patch.object(WindowsWriteScope, "__init__", short_file_lock), \
                patch("dianstar.platform.command_reads.allow_runtime", side_effect=cold):
            first = asyncio.create_task(run_process([sys.executable, "-c", "print('first project')"], self.root, self.environment(), 10))
            try:
                async with asyncio.timeout(25):
                    while not entered.is_set():
                        await asyncio.sleep(.01)
                second = asyncio.create_task(run_process([sys.executable, "-c", "print('second project')"], other, self.environment(), 10))
                results = await asyncio.gather(first, second)
                self.assertTrue(all(item["success"] and not item.get("isolation_cleanup_pending") for item in results), results)
            finally:
                if not first.done():
                    first.cancel()
                    await asyncio.gather(first, return_exceptions=True)
