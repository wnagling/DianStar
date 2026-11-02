"""Real process locks, transaction failures, restart recovery and safe undo."""
import asyncio
import codecs
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.platform.file_state import protect
from dianstar.security.authorization import Authorization
from dianstar.security.file_locks import FileLocks, FileLockError, canonical
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.workspace import WorkspaceTools


class FileRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.env = self.base / "application.env"
        self.env.write_text("API_KEY=fixture-application-key\n")
        self.state = self.base / "private-state"
        environment = patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.state)})
        environment.start()
        self.addCleanup(environment.stop)
        self.tools, self.gateway = self.instance()

    def instance(self):
        authorization = Authorization()
        mcp = SimpleNamespace(settings=MCPSettings(git_enabled=True, workspace_write_enabled=True), authorization=authorization,
                              git=SimpleNamespace(repositories={str(self.root)}))
        tools = WorkspaceTools(mcp, self.env)
        return tools, ToolGateway(authorization, tools.registrations(), AsyncMock(return_value=True))

    async def call(self, name, gateway=None, **args):
        return (await (gateway or self.gateway).execute(ToolAction(name, {"repository": str(self.root), **args}))).result

    async def test_write_delete_restore_and_redo_preserve_bytes(self):
        target = self.root / "a.txt"
        original = codecs.BOM_UTF8 + b"before\r\n"
        target.write_bytes(original)
        await self.call("read", path="a.txt")
        change = await self.call("write", path="a.txt", content="after\n")
        self.assertTrue(change["success"], change)
        tools2, gateway2 = self.instance()
        undo = await self.call("file_restore", gateway=gateway2, transaction_id=change["transaction_id"])
        self.assertTrue(undo["success"], undo)
        self.assertEqual(target.read_bytes(), original)
        redo = await self.call("file_restore", gateway=gateway2, transaction_id=undo["transaction_id"])
        self.assertTrue(redo["success"], redo)
        self.assertEqual(target.read_bytes(), codecs.BOM_UTF8 + b"after\r\n")
        deleted = await self.call("workspace_delete", gateway=gateway2, path="a.txt")
        self.assertFalse(target.exists())
        self.assertTrue((await self.call("file_restore", gateway=gateway2, transaction_id=deleted["transaction_id"]))["success"])
        self.assertEqual(target.read_bytes(), codecs.BOM_UTF8 + b"after\r\n")
        history = await self.call("file_history", gateway=gateway2)
        self.assertEqual(len(history["transactions"]), 5)
        self.assertNotIn("before", json.dumps(history))

    async def test_successful_batch_has_one_approval_and_whole_batch_undo(self):
        (self.root / "a.txt").write_text("before")
        await self.call("read", path="a.txt")
        self.gateway.request_approval.reset_mock()
        result = await self.call("file_batch", operations=[
            {"operation": "edit", "path": "a.txt", "old_string": "before", "new_string": "after"},
            {"operation": "write", "path": "docs/new.txt", "content": "new"}])
        self.assertTrue(result["success"], result)
        self.gateway.request_approval.assert_awaited_once()
        undo = await self.call("file_restore", transaction_id=result["transaction_id"])
        self.assertTrue(undo["success"], undo)
        self.assertEqual((self.root / "a.txt").read_text(), "before")
        self.assertFalse((self.root / "docs/new.txt").exists())
        self.assertFalse((self.root / "docs").exists())

    async def test_successive_undos_follow_recorded_lineage_but_equal_user_rewrite_is_rejected(self):
        target = self.root / "a.txt"
        target.write_text("A")
        await self.call("read", path="a.txt")
        first = await self.call("write", path="a.txt", content="B")
        await self.call("read", path="a.txt")
        second = await self.call("write", path="a.txt", content="C")
        self.assertTrue((await self.call("file_restore", transaction_id=second["transaction_id"]))["success"])
        result = await self.call("file_restore", transaction_id=first["transaction_id"])
        self.assertTrue(result["success"], result)
        self.assertEqual(target.read_text(), "A")
        created = await self.call("write", path="new.txt", content="same")
        (self.root / "new.txt").unlink()
        (self.root / "new.txt").write_text("same")
        self.assertEqual((await self.call("file_restore", transaction_id=created["transaction_id"]))["error"], "file_restore_conflict")

    async def test_failed_batch_rolls_back_successful_files_and_empty_directories(self):
        (self.root / "a.txt").write_text("before")
        await self.call("read", path="a.txt")
        original = os.rename
        def fail_new(source, destination):
            if Path(destination).name == "b.txt":
                raise OSError("fixture disk full")
            return original(source, destination)
        with patch("dianstar.tools.local.files.recovery.os.rename", side_effect=fail_new):
            result = await self.call("file_batch", operations=[
                {"operation": "write", "path": "a.txt", "content": "after"},
                {"operation": "write", "path": "dir/b.txt", "content": "new"}])
        self.assertEqual(result["recovery"]["status"], "rolled_back", result)
        self.assertEqual((self.root / "a.txt").read_text(), "before")
        self.assertFalse((self.root / "dir").exists())
        self.assertFalse(list(self.root.rglob(".dianstar-tmp-*")))

    async def test_conflicting_user_change_is_preserved_and_requires_resolution(self):
        target = self.root / "a.txt"
        target.write_text("before")
        await self.call("read", path="a.txt")
        original = os.rename
        def change_then_fail(source, destination):
            if Path(destination).name == "b.txt":
                target.write_text("user update")
                raise OSError("fixture failure")
            return original(source, destination)
        with patch("dianstar.tools.local.files.recovery.os.rename", side_effect=change_then_fail):
            result = await self.call("file_batch", operations=[
                {"operation": "write", "path": "a.txt", "content": "after"},
                {"operation": "write", "path": "b.txt", "content": "new"}])
        self.assertEqual(result["recovery"]["status"], "conflict", result)
        self.assertEqual(target.read_text(), "user update")
        self.assertEqual((await self.call("read", path="a.txt"))["type"], "file")
        denied = await self.call("write", path="a.txt", content="would overwrite")
        self.assertIn("error", denied)
        kept = await self.call("file_restore", transaction_id=result["recovery"]["transaction_id"], keep_current=True)
        self.assertTrue(kept["success"], kept)
        self.assertEqual(target.read_text(), "user update")
        self.assertTrue((await self.call("write", path="a.txt", content="explicit new edit"))["success"])

    async def test_restore_rejects_new_user_version_and_changes_during_approval(self):
        created = await self.call("write", path="a.txt", content="agent")
        target = self.root / "a.txt"
        target.write_text("user")
        result = await self.call("file_restore", transaction_id=created["transaction_id"])
        self.assertEqual(result["error"], "file_restore_conflict")
        self.assertEqual(target.read_text(), "user")
        created = await self.call("write", path="b.txt", content="agent")
        async def change(_):
            (self.root / "b.txt").write_text("user during approval")
            return True
        self.gateway.request_approval = change
        result = await self.call("file_restore", transaction_id=created["transaction_id"])
        self.assertEqual(result["error"], "file_changed")
        self.assertEqual((self.root / "b.txt").read_text(), "user during approval")

    async def test_journal_failure_prevents_target_changes(self):
        target = self.root / "a.txt"
        target.write_text("before")
        await self.call("read", path="a.txt")
        with patch.object(self.tools.files.journal, "save", side_effect=OSError("fixture full disk")):
            result = await self.call("write", path="a.txt", content="after")
        self.assertIn("error", result)
        self.assertEqual(target.read_text(), "before")

    async def test_short_temporary_write_never_replaces_original(self):
        target = self.root / "a.txt"
        target.write_text("before")
        await self.call("read", path="a.txt")
        original = tempfile.NamedTemporaryFile
        def short(**kwargs):
            stream = original(**kwargs)
            write = stream.write
            stream.write = lambda data: write(data[:-1])
            return stream
        with patch("dianstar.tools.local.files.recovery.tempfile.NamedTemporaryFile", side_effect=short):
            result = await self.call("write", path="a.txt", content="after")
        self.assertEqual(result["error"], "file_temporary_incomplete", result)
        self.assertEqual(result["recovery"]["status"], "rolled_back")
        self.assertEqual(target.read_text(), "before")
        self.assertFalse(list(self.root.rglob(".dianstar-tmp-*")))

    async def test_batch_local_configuration_approval_masks_unquoted_credentials(self):
        # Synthetic fixture: exercise redaction without publishing a credential literal.
        secret = "-".join(("local", "only", "synthetic", "password"))
        requests = []
        async def approve(request):
            requests.append(request)
            return True
        self.gateway.request_approval = approve
        result = await self.call("file_batch", operations=[{"operation": "write", "path": ".env",
            "content": "".join(f"{name}={value}\n" for name, value in {"DEPLOY_PASSWORD": secret}.items()), "local_only": True}])
        self.assertTrue(result["success"], result)
        self.assertEqual(len(requests), 1)
        self.assertNotIn(secret, json.dumps(requests, ensure_ascii=False))
        self.assertIn("仅在本机保存", requests[0]["summary"])
        self.assertIn("已遮挡", requests[0]["description"])
        self.assertNotIn(secret, json.dumps(await self.call("file_history")))

    async def test_failed_commit_marker_rolls_back_but_cleanup_failure_after_commit_does_not(self):
        target = self.root / "a.txt"
        target.write_text("before")
        await self.call("read", path="a.txt")
        original = self.tools.files.journal.save
        def fail_commit(record):
            if record["status"] == "committed":
                raise OSError("fixture commit marker failed")
            return original(record)
        with patch.object(self.tools.files.journal, "save", side_effect=fail_commit):
            result = await self.call("write", path="a.txt", content="after")
        self.assertEqual(result["recovery"]["status"], "rolled_back", result)
        self.assertEqual(target.read_text(), "before")
        await self.call("read", path="a.txt")
        with patch.object(self.tools.files, "cleanup", side_effect=OSError("fixture cleanup failed")):
            result = await self.call("write", path="a.txt", content="after")
        self.assertTrue(result["success"], result)
        self.assertEqual(target.read_text(), "after")
        self.assertEqual((await self.call("file_history"))["transactions"][0]["status"], "committed")

    async def test_rollback_failure_keeps_durable_record_and_can_resume(self):
        target = self.root / "a.txt"
        target.write_text("before")
        await self.call("read", path="a.txt")
        original_replace = os.replace
        def fail_undo(source, destination):
            if Path(destination).name == "a.txt" and target.read_text() == "after":
                raise OSError("fixture rollback failure")
            return original_replace(source, destination)
        with patch("dianstar.tools.local.files.recovery.os.rename", side_effect=OSError("fixture forward failure")), \
                patch("dianstar.tools.local.files.recovery.os.replace", side_effect=fail_undo):
            result = await self.call("file_batch", operations=[
                {"operation": "write", "path": "a.txt", "content": "after"},
                {"operation": "write", "path": "new/b.txt", "content": "new"}])
        self.assertEqual(result["recovery"]["status"], "recovery_required", result)
        restored = await self.call("file_restore", transaction_id=result["recovery"]["transaction_id"])
        self.assertTrue(restored["success"], restored)
        self.assertEqual(target.read_text(), "before")
        self.assertFalse(list(self.root.rglob(".dianstar-tmp-*")))
        self.assertFalse((self.root / "new").exists())

    async def test_readonly_does_not_restore_interrupted_changes_or_bypass_component_policy(self):
        (self.root / "a.txt").write_text("before")
        result = await asyncio.to_thread(self.child, "write")
        self.assertEqual(result.returncode, 23, result.stderr)
        tools, _ = self.instance()
        registry = tools.registrations()
        gateway = ToolGateway(tools.mcp.authorization, registry, AsyncMock(return_value=True),
                              policies={"workspace.write": ToolPolicy(enabled=False)})
        read = await self.call("read", gateway=gateway, path="a.txt")
        self.assertIn("after", read["content"])
        self.assertTrue(read["recovery_pending"])
        self.assertEqual((self.root / "a.txt").read_text(), "after")
        self.assertEqual((await self.call("file_restore", gateway=gateway,
            transaction_id=read["recovery_pending"][0]["transaction_id"]))["error"], "tool_disabled")
        gateway = ToolGateway(tools.mcp.authorization, registry, AsyncMock(return_value=True),
                              policies={"edit": ToolPolicy(enabled=False)})
        result = await self.call("file_batch", gateway=gateway, operations=[{"operation": "write", "path": "b.txt", "content": "new"}])
        self.assertEqual(result["error"], "tool_disabled")
        self.assertFalse((self.root / "b.txt").exists())

    async def test_corrupted_backup_fails_closed_and_history_never_decrypts_file_payloads(self):
        created = await self.call("write", path="a.txt", content="unchanged")
        with closing(sqlite3.connect(self.state / "history.sqlite3")) as connection, connection:
            connection.execute("UPDATE transactions SET payload=? WHERE id=?", (b"fixture-corrupt-backup", created["transaction_id"]))
        history = await self.call("file_history")
        self.assertEqual(history["transactions"][0]["transaction_id"], created["transaction_id"])
        result = await self.call("file_restore", transaction_id=created["transaction_id"])
        self.assertEqual(result["error"], "file_journal_invalid", result)
        self.assertEqual((self.root / "a.txt").read_text(), "unchanged")

    async def test_two_real_agent_processes_cannot_commit_the_same_baseline(self):
        (self.root / "a.txt").write_text("before")
        script = r'''
import asyncio,json,sys
from pathlib import Path
from types import SimpleNamespace
from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.workspace import WorkspaceTools
root,env,label=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
auth=Authorization()
tools=WorkspaceTools(SimpleNamespace(settings=MCPSettings(git_enabled=True,workspace_write_enabled=True),
    authorization=auth,git=SimpleNamespace(repositories={str(root)})),env)
async def confirm(request):
    (root.parent / ('ready-'+label)).write_text('ready')
    for _ in range(300):
        if (root.parent / 'go').exists(): return True
        await asyncio.sleep(0.02)
    return False
async def main():
    gateway=ToolGateway(auth,tools.registrations(),confirm)
    await gateway.execute(ToolAction('read',{'repository':str(root),'path':'a.txt'}))
    result=await gateway.execute(ToolAction('write',{'repository':str(root),'path':'a.txt','content':label}))
    print(json.dumps(result.result))
asyncio.run(main())
'''
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.root), str(self.env), label],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE) for label in ("one", "two")]
        try:
            for _ in range(200):
                if all((self.base / ("ready-" + label)).exists() for label in ("one", "two")):
                    break
                await asyncio.sleep(0.03)
            else:
                self.fail("Both processes must reach approval before the race")
            (self.base / "go").write_text("go")
            outputs = await asyncio.gather(*(asyncio.to_thread(process.communicate, timeout=15) for process in processes))
            self.assertTrue(all(process.returncode == 0 for process in processes), outputs)
            results = [json.loads(output[0]) for output in outputs]
            self.assertEqual(sum(bool(result.get("success")) for result in results), 1, results)
            self.assertEqual(sum(result.get("error") == "file_changed" for result in results), 1, results)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    async def test_parallel_agents_with_same_baseline_cannot_both_overwrite(self):
        target = self.root / "a.txt"
        target.write_text("before")
        _, gateway2 = self.instance()
        await asyncio.gather(self.call("read", path="a.txt"), self.call("read", gateway=gateway2, path="a.txt"))
        ready = 0
        both = asyncio.Event()
        async def approve(_):
            nonlocal ready
            ready += 1
            if ready == 2:
                both.set()
            await both.wait()
            return True
        self.gateway.request_approval = gateway2.request_approval = approve
        results = await asyncio.gather(self.call("write", path="a.txt", content="agent one"),
            self.call("write", gateway=gateway2, path="a.txt", content="agent two"))
        self.assertEqual(sum(bool(item.get("success")) for item in results), 1, results)
        self.assertEqual(sum(item.get("error") == "file_changed" for item in results), 1, results)

    async def test_live_transactions_in_separate_processes_are_not_crash_recovered(self):
        script = r'''
import asyncio,json,sys,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.workspace import WorkspaceTools
from dianstar.tools.local.files.journal import FileJournal
root,env,label=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
auth=Authorization()
tools=WorkspaceTools(SimpleNamespace(settings=MCPSettings(git_enabled=True,workspace_write_enabled=True),
    authorization=auth,git=SimpleNamespace(repositories={str(root)})),env)
gateway=ToolGateway(auth,tools.registrations(),AsyncMock(return_value=True))
original=FileJournal.save
seen=set()
def save(journal,record):
    result=original(journal,record)
    if record['status']=='prepared' and record['id'] not in seen:
        seen.add(record['id'])
        (root.parent/('live-'+label)).touch()
        deadline=time.monotonic()+8
        while not all((root.parent/('live-'+item)).exists() for item in ('one','two')):
            if time.monotonic()>deadline: raise RuntimeError('Independent processes did not overlap')
            time.sleep(.01)
    return result
FileJournal.save=save
async def main():
    result=await gateway.execute(ToolAction('write',{'repository':str(root),'path':label+'.txt','content':label}))
    print(json.dumps(result.result))
asyncio.run(main())
'''
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.root), str(self.env), label],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for label in ("one", "two")]
        try:
            outputs = await asyncio.gather(*(asyncio.to_thread(process.communicate, timeout=18) for process in processes))
            self.assertTrue(all(process.returncode == 0 for process in processes), outputs)
            results = [json.loads(output[0]) for output in outputs]
            self.assertTrue(all(result.get("success") for result in results), results)
            self.assertEqual((self.root / "one.txt").read_text(), "one")
            self.assertEqual((self.root / "two.txt").read_text(), "two")
            transactions = (await self.call("file_history"))["transactions"]
            self.assertEqual([record["status"] for record in transactions], ["committed", "committed"])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    async def test_cancel_after_first_publication_waits_for_rollback(self):
        (self.root / "a.txt").write_text("before")
        await self.call("read", path="a.txt")
        first, release = threading.Event(), threading.Event()
        original = os.replace
        def pause(source, destination):
            result = original(source, destination)
            if Path(destination).name == "a.txt" and not first.is_set():
                first.set()
                release.wait(5)
            return result
        with patch("dianstar.tools.local.files.recovery.os.replace", side_effect=pause):
            task = asyncio.create_task(self.call("file_batch", operations=[
                {"operation": "write", "path": "a.txt", "content": "after"},
                {"operation": "write", "path": "b.txt", "content": "new"}]))
            self.assertTrue(await asyncio.to_thread(first.wait, 5))
            task.cancel()
            await asyncio.sleep(0.05)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual((self.root / "a.txt").read_text(), "before")
        self.assertFalse((self.root / "b.txt").exists())
        self.assertEqual((await self.call("file_history"))["transactions"][0]["status"], "rolled_back")

    def child(self, mode):
        program = r'''
import asyncio, os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.workspace import WorkspaceTools
root, env, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
auth = Authorization()
tools = WorkspaceTools(SimpleNamespace(settings=MCPSettings(git_enabled=True, workspace_write_enabled=True),
    authorization=auth, git=SimpleNamespace(repositories={str(root)})), env)
gateway = ToolGateway(auth, tools.registrations(), AsyncMock(return_value=True))
original = os.replace
def crash(source, destination):
    original(source, destination)
    if Path(destination).name == 'a.txt': os._exit(23)
async def main():
    if mode == 'write':
        await gateway.execute(ToolAction('read', {'repository':str(root),'path':'a.txt'}))
        os.replace = crash
        await gateway.execute(ToolAction('file_batch', {'repository':str(root),'operations':[
            {'operation':'write','path':'a.txt','content':'after'},
            {'operation':'write','path':'new/b.txt','content':'new'}]}))
    else:
        os.replace = crash
        with tools.files.locks.repository(root): tools.files.recover_pending(root)
asyncio.run(main())
'''
        return subprocess.run([sys.executable, "-c", program, str(self.root), str(self.env), mode], capture_output=True, timeout=20)

    async def test_process_crash_and_crash_during_rollback_are_restartable(self):
        (self.root / "a.txt").write_text("before")
        result = await asyncio.to_thread(self.child, "write")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual((self.root / "a.txt").read_text(), "after")
        result = await asyncio.to_thread(self.child, "recover")
        self.assertEqual(result.returncode, 23, result.stderr)
        _, gateway2 = self.instance()
        pending = (await self.call("file_history", gateway=gateway2))["transactions"][0]
        recovered = await self.call("file_restore", gateway=gateway2, transaction_id=pending["transaction_id"])
        self.assertTrue(recovered["success"], recovered)
        read = await self.call("read", gateway=gateway2, path="a.txt")
        self.assertIn("before", read["content"], read)
        self.assertFalse((self.root / "new/b.txt").exists())
        self.assertEqual((await self.call("file_history", gateway=gateway2))["transactions"][0]["status"], "rolled_back")

    def test_os_user_backup_protection_roundtrip(self):
        raw = b"fixture-private-backup-content"
        protected = protect(raw)
        self.assertEqual(protect(protected, decrypt=True), raw)
        if os.name == "nt":
            self.assertNotIn(raw, protected)


class FileLockTests(unittest.TestCase):
    def test_opposite_file_orders_finish_without_circular_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            script = r'''
import sys,time
from pathlib import Path
from dianstar.security.file_locks import FileLocks
base=Path(sys.argv[1]); paths=[base/name for name in sys.argv[2].split(',')]
print('ready',flush=True)
deadline=time.monotonic()+5
while not (base/'go').exists():
    if time.monotonic()>deadline: raise RuntimeError('Fixture start timeout')
    time.sleep(.005)
with FileLocks(base/'state',timeout=2).files(paths): time.sleep(.05)
print('done',flush=True)
'''
            processes = [subprocess.Popen([sys.executable, "-c", script, str(base), order],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for order in ("a,b", "b,a")]
            try:
                self.assertTrue(all(process.stdout.readline().strip() == "ready" for process in processes))
                (base / "go").touch()
                for process in processes:
                    output, error = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, error)
                    self.assertIn("done", output)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)

    def test_cross_process_read_sharing_exclusive_write_timeout_and_crash_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve() / "state"
            locks = FileLocks(state, timeout=0.15)
            key = "fixture-path"
            script = "from dianstar.security.file_locks import FileLocks; import sys,time; " \
                "locks=FileLocks(sys.argv[1],timeout=0.2); " \
                "cm=locks.hold('fixture-path',shared=sys.argv[2]=='read'); cm.__enter__(); print('locked',flush=True); time.sleep(30)"
            with locks.hold(key, shared=True):
                process = subprocess.Popen([sys.executable, "-c", script, str(state), "read"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(process.stdout.readline().strip(), "locked")
                    with self.assertRaises(FileLockError):
                        with locks.hold(key):
                            self.fail("Writer must not enter while readers hold the lock")
                finally:
                    process.kill()
                    process.communicate(timeout=5)
            with locks.hold(key):
                result = subprocess.run([sys.executable, "-c", script, str(state), "read"], capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
            with locks.hold(key):
                pass
