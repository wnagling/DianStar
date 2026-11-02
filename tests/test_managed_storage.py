"""Real filesystem acceptance for trash, scratch scopes, recovery and expiry."""
import asyncio
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.workspace import WorkspaceTools
from dianstar.tools.local.storage.tree import snapshot, same, StorageError
from tests import test_file_tools as fixtures


class ManagedStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.FileToolTests.setUp(self)
        environment = patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.base / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.tools = WorkspaceTools(self.mcp, self.env, conversation_id="fixture-session")
        self.addCleanup(lambda: self.tools.storage.manager.close())
        self.registry = self.tools.registrations()
        self.gateway = ToolGateway(self.auth, self.registry, self.approve)
        self.manager = self.tools.storage.manager

    async def call(self, name, **args):
        return (await self.gateway.execute(ToolAction("workspace_" + name, args))).result

    def folder(self, name="example"):
        path = self.root / name
        path.mkdir()
        (path / "main.py").write_text("print('fixture hello')\n", encoding="utf-8")
        (path / "binary.bin").write_bytes(bytes(range(256)) * 200)
        (path / "empty").mkdir()
        return path

    async def put(self, *paths):
        result = await self.call("trash_put", paths=[str(path) for path in paths])
        self.assertTrue(result.get("success"), result)
        return result["entries"][0]["id"]

    async def test_directory_binary_empty_subdir_and_git_metadata_round_trip(self):
        folder = self.folder()
        (folder / ".git").mkdir()
        (folder / ".git/config").write_text("fixture Git metadata")
        before = snapshot(folder)
        identifier = await self.put(folder)
        self.assertFalse(folder.exists())
        listed = await self.call("trash_list")
        self.assertEqual(listed["entries"][0]["id"], identifier)
        result = await self.call("trash_restore", id=identifier)
        self.assertTrue(result.get("success"), result)
        self.assertTrue(same(snapshot(folder), before))
        self.assertEqual(self.approve.await_count, 2)

    async def test_batch_single_confirmation_and_no_overwrite_restore(self):
        first, second = self.folder("one"), self.folder("two")
        result = await self.call("trash_put", paths=[str(first), str(second)])
        self.assertTrue(result.get("success"), result)
        self.approve.assert_awaited_once()
        first.mkdir()
        (first / "new.txt").write_text("fixture-new-work")
        identifier = result["entries"][0]["id"]
        refused = await self.call("trash_restore", id=identifier)
        self.assertEqual(refused["error"], "storage_destination_exists")
        alternative = self.root / "restored-copy"
        restored = await self.call("trash_restore", id=identifier, destination=str(alternative))
        self.assertTrue(restored.get("success"), restored)
        self.assertEqual((first / "new.txt").read_text(), "fixture-new-work")
        self.assertTrue((alternative / "binary.bin").exists())

    async def test_whole_repository_can_be_recycled_and_restored(self):
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        (self.root / "untracked.txt").write_text("fixture original user work")
        identifier = await self.put(self.root)
        self.assertFalse(self.root.exists())
        result = await self.call("trash_restore", id=identifier)
        self.assertTrue(result.get("success"), result)
        self.assertTrue((self.root / ".git/config").is_file())
        self.assertEqual((self.root / "untracked.txt").read_text(), "fixture original user work")

    async def test_batch_move_failure_rolls_back_earlier_targets(self):
        first, second = self.folder("one"), self.folder("two")
        original = os.rename
        def fail(source, destination):
            if Path(source) == second:
                raise PermissionError("fixture move failure")
            return original(source, destination)
        with patch("dianstar.tools.local.storage.tree.os.rename", side_effect=fail):
            result = await self.call("trash_put", paths=[str(first), str(second)])
        self.assertIn("error", result)
        self.assertTrue((first / "main.py").exists())
        self.assertTrue((second / "main.py").exists())

    async def test_cross_device_branch_copies_real_bytes_and_preserves_metadata(self):
        folder = self.folder()
        before = snapshot(folder)
        original = os.rename
        def cross(source, destination):
            if Path(source) == folder and Path(destination).name == "payload":
                raise OSError(errno.EXDEV, "fixture cross-device")
            return original(source, destination)
        with patch("dianstar.tools.local.storage.tree.os.rename", side_effect=cross):
            identifier = await self.put(folder)
        self.assertEqual(self.manager.record(identifier)["method"], "copy")
        self.assertFalse(folder.exists())
        self.assertFalse(self.manager.source_stage(self.manager.record(identifier)).exists())
        result = await self.call("trash_restore", id=identifier)
        self.assertTrue(result.get("success"), result)
        self.assertTrue(same(snapshot(folder), before, identities=False))

    async def test_copy_failure_keeps_original_and_returns_recovery_id(self):
        folder = self.folder()
        before = snapshot(folder)
        with patch("dianstar.tools.local.storage.tree.os.rename", side_effect=OSError(errno.EXDEV, "fixture cross-device")), \
                patch("dianstar.tools.local.storage.trash.copy_tree", side_effect=OSError("fixture copy failed")):
            result = await self.call("trash_put", paths=[str(folder)])
        self.assertIn("error", result)
        self.assertTrue(result["entries"][0]["id"])
        self.assertTrue(same(snapshot(folder), before))

    async def test_scope_protected_paths_and_overlapping_targets_are_rejected(self):
        folder = self.folder()
        for paths in ([str(self.base)], [str(self.env)], [str(self.manager.directory)], [str(folder), str(folder / "main.py")]):
            self.assertIn("error", await self.call("trash_put", paths=paths))
        self.assertTrue(folder.exists())
        self.approve.assert_not_awaited()

    async def test_preview_changes_and_backend_denial_do_not_delete(self):
        folder = self.folder()
        async def change(request):
            (folder / "main.py").write_text("fixture user changed during approval")
            return True
        self.gateway = ToolGateway(self.auth, self.registry, change)
        result = await self.call("trash_put", paths=[str(folder)])
        self.assertEqual(result["error"], "storage_changed")
        self.assertTrue(folder.exists())
        for policies in ({"workspace.write": ToolPolicy(enabled=False)}, {"workspace_delete": ToolPolicy(enabled=False)}):
            self.gateway = ToolGateway(self.auth, self.registry, self.approve, policies=policies)
            self.assertEqual((await self.call("trash_put", paths=[str(folder)]))["error"], "tool_disabled")

    async def test_lease_protected_scratch_supports_file_and_command_tools_then_reopen(self):
        created = await self.call("scratch_create", repository=str(self.root))
        self.assertTrue(created.get("success"), created)
        scratch = created["repository"]
        self.approve.assert_not_awaited()
        for name, args in (("write", {"path": "check.py", "content": "print('scratch-ok')\n"}),
                           ("read", {"path": "check.py"}),
                           ("workspace_run", {"argv": [sys.executable, "check.py"]})):
            result = await self.gateway.execute(ToolAction(name, {"repository": scratch, **args}))
            self.assertNotIn("error", result.result, result.result)
            if name == "workspace_run":
                self.assertTrue(result.result["success"], result.result)
                self.assertIn("scratch-ok", result.result["content"])
        self.assertTrue(self.manager.live(created["id"]))
        result = await self.call("trash_purge", ids=[created["id"]])
        self.assertEqual(result["error"], "storage_retained")
        self.manager.close()
        self.assertFalse(self.manager.live(created["id"]))
        reopened = await self.call("scratch_open", id=created["id"])
        self.assertTrue(reopened.get("success"), reopened)
        other = WorkspaceTools(self.mcp, self.env, conversation_id="fixture-other-session")
        gateway = ToolGateway(self.auth, other.registrations(), self.approve)
        result = await gateway.execute(ToolAction("workspace_scratch_open", {"id": created["id"]}))
        self.assertEqual(result.result["error"], "scratch_scope_denied")

    async def test_retention_based_on_removal_not_old_file_time_and_pin(self):
        folder = self.folder()
        old = 1_000_000
        os.utime(folder / "main.py", (old, old))
        identifier = await self.put(folder)
        self.assertEqual((await self.call("storage_cleanup"))["entries"], [])
        now = self.manager.journal.clock()
        self.manager.journal.clock = lambda: now + 31 * 86400
        pinned = await self.call("storage_pin", id=identifier, pinned=True)
        self.assertTrue(pinned.get("success"), pinned)
        self.assertEqual((await self.call("storage_cleanup"))["entries"], [])
        await self.call("storage_pin", id=identifier, pinned=False)
        cleaned = await self.call("storage_cleanup")
        self.assertTrue(cleaned.get("success"), cleaned)
        self.assertEqual(cleaned["entries"][0]["status"], "purged")
        self.assertFalse(self.manager.payload(self.manager.record(identifier)).exists())

    async def test_permanent_purge_asks_and_does_not_touch_recreated_original(self):
        folder = self.folder()
        identifier = await self.put(folder)
        folder.mkdir()
        (folder / "latest.txt").write_text("fixture latest")
        self.approve.reset_mock()
        purged = await self.call("trash_purge", ids=[identifier])
        self.assertTrue(purged.get("success"), purged)
        self.approve.assert_awaited_once()
        self.assertIn("永久", self.approve.call_args.args[0]["summary"])
        self.assertEqual((folder / "latest.txt").read_text(), "fixture latest")

    async def test_two_sessions_competing_for_one_path_have_one_complete_winner(self):
        folder = self.folder()
        other_auth = Authorization()
        from copy import copy
        mcp = copy(self.mcp)
        mcp.authorization = other_auth
        other = WorkspaceTools(mcp, self.env, conversation_id="fixture-other")
        gateway = ToolGateway(other_auth, other.registrations(), AsyncMock(return_value=True))
        action = ToolAction("workspace_trash_put", {"paths": [str(folder)]})
        results = await asyncio.gather(self.gateway.execute(action), gateway.execute(action))
        winners = [result for result in results if result.result.get("success")]
        self.assertEqual(len(winners), 1, results)
        self.assertFalse(folder.exists())
        identifier = winners[0].result["entries"][0]["id"]
        self.assertTrue((self.manager.payload(self.manager.record(identifier)) / "main.py").exists())

    async def test_cross_volume_restore_copies_then_publishes_without_losing_bytes(self):
        folder = self.folder()
        before = snapshot(folder)
        identifier = await self.put(folder)
        payload = self.manager.payload(self.manager.record(identifier))
        original = os.rename
        def cross(source, destination):
            if Path(source) == payload:
                raise OSError(errno.EXDEV, "fixture cross-volume restore")
            return original(source, destination)
        with patch("dianstar.tools.local.storage.tree.os.rename", side_effect=cross):
            restored = await self.call("trash_restore", id=identifier)
        self.assertTrue(restored.get("success"), restored)
        self.assertTrue(same(snapshot(folder), before, identities=False))
        self.assertFalse(payload.exists())
        self.assertFalse(restored["cleanup_pending"])

    async def test_ready_cross_volume_cleanup_failure_can_be_resumed(self):
        folder = self.folder()
        original = os.rename
        def cross(source, destination):
            if Path(source) == folder and "trash" in Path(destination).parts:
                raise OSError(errno.EXDEV, "fixture cross-volume")
            return original(source, destination)
        with patch("dianstar.tools.local.storage.tree.os.rename", side_effect=cross), \
                patch("dianstar.tools.local.storage.trash.remove_verified", side_effect=PermissionError("fixture busy")):
            identifier = await self.put(folder)
        record = self.manager.record(identifier)
        self.assertTrue(record["pinned"])
        self.assertTrue(self.manager.source_stage(record).exists())
        recovered = await self.call("storage_recover", id=identifier)
        self.assertTrue(recovered.get("success"), recovered)
        self.assertFalse(self.manager.source_stage(record).exists())
        self.assertTrue(self.manager.payload(record).exists())
        self.assertFalse(recovered["cleanup_pending"])

    async def test_process_crash_after_move_recovery_restores_batch(self):
        first, second = self.folder("one"), self.folder("two")
        script = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from dianstar.config.mcp import MCPSettings
from dianstar.security.authorization import Authorization
from dianstar.tools.local.workspace import WorkspaceTools
from dianstar.tools.local.storage import trash
root, env, first, second = map(Path, sys.argv[1:])
mcp=SimpleNamespace(settings=MCPSettings(git_enabled=True, workspace_write_enabled=True), authorization=Authorization(), git=SimpleNamespace(repositories={str(root)}))
manager=WorkspaceTools(mcp,env,conversation_id="fixture-session").storage.manager
original=trash.rename_new
def crash(source,destination):
    original(source,destination)
    os._exit(71)
trash.rename_new=crash
with manager.mutation([first,second]):
    manager.put(manager.plan_put({"paths":[str(first),str(second)]}))
'''
        child = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(self.root), str(self.env), str(first), str(second)],
            capture_output=True, timeout=20)
        self.assertEqual(child.returncode, 71, child.stderr.decode(errors="replace"))
        pending = self.manager.journal.records("trash")
        self.assertEqual(len(pending), 2)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        result = await self.call("storage_recover", id=pending[0]["id"])
        self.assertTrue(result.get("success"), result)
        self.assertTrue((first / "binary.bin").exists())
        self.assertTrue((second / "main.py").exists())
        self.assertTrue(all(record["status"] == "rolled_back" for record in self.manager.journal.records("trash")))

    async def test_restore_published_before_journal_commit_is_recovered(self):
        folder = self.folder()
        identifier = await self.put(folder)
        original = self.manager.journal.save
        def fail(record):
            if record["status"] == "restored":
                raise OSError("fixture persistence failure after published destination")
            return original(record)
        with patch.object(self.manager.journal, "save", side_effect=fail):
            incomplete = await self.call("trash_restore", id=identifier)
        self.assertIn("error", incomplete)
        self.assertTrue(folder.exists())
        result = await self.call("storage_recover", id=identifier)
        self.assertTrue(result.get("success"), result)
        self.assertEqual(result["status"], "restored")

    async def test_purge_partial_failure_resumes_and_unknown_children_are_retained(self):
        folder = self.folder()
        identifier = await self.put(folder)
        original = Path.unlink
        def fail(path, *args, **kwargs):
            if path.name == "binary.bin":
                raise PermissionError("fixture busy")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail):
            result = await self.call("trash_purge", ids=[identifier])
        self.assertFalse(result["success"])
        payload = self.manager.payload(self.manager.record(identifier))
        self.assertFalse((payload / "main.py").exists())
        (payload / "unrecorded.txt").write_text("fixture new content, must survive")
        refused = await self.call("storage_recover", id=identifier)
        self.assertEqual(refused["error"], "storage_recovery_conflict")
        self.assertTrue((payload / "unrecorded.txt").exists())
        # A second recovery remains a purge, never restores a partial directory.
        refused_again = await self.call("storage_recover", id=identifier)
        self.assertEqual(refused_again["error"], "storage_recovery_conflict")
        self.assertFalse(folder.exists())

    async def test_maintenance_expires_scratch_and_trash_but_never_opens_forced_dialog(self):
        from dianstar.app.storage_lifecycle import finish_storage
        from types import SimpleNamespace
        scratch = await self.call("scratch_create", repository=str(self.root))
        Path(scratch["repository"], "script.py").write_text("print('fixture')")
        self.manager.close()
        identifier = await self.put(self.folder())
        now = self.manager.journal.clock()
        self.manager.journal.clock = lambda: now + 31 * 86400
        self.approve.reset_mock()
        log = SimpleNamespace(append=lambda event: None)
        forced = ToolGateway(self.auth, self.registry, self.approve, policies={"workspace_storage_cleanup": ToolPolicy(requires_approval=True)})
        await finish_storage(self.tools, forced, log)
        self.assertEqual(self.manager.record(identifier)["status"], "ready")
        await finish_storage(self.tools, self.gateway, log)
        await finish_storage(self.tools, self.gateway, log)
        self.approve.assert_not_awaited()
        self.assertEqual(self.manager.record(identifier)["status"], "purged")
        self.assertFalse(Path(scratch["repository"]).exists())

    async def test_external_task_lease_prevents_cleanup_and_dead_lease_gets_grace_period(self):
        created = await self.call("scratch_create", repository=str(self.root))
        from dianstar.tools.local.storage.manager import StorageManager
        other = StorageManager(self.tools)
        self.assertTrue(other.live(created["id"]))
        self.assertEqual(other.abandoned(), [])
        # Simulate a killed process: OS lease vanishes, database remains active.
        lease = self.manager.leases.pop(created["id"])
        lease.__exit__(None, None, None)
        result = await self.call("storage_cleanup", small_only=True)
        self.assertTrue(result["success"], result)
        record = self.manager.record(created["id"])
        self.assertEqual(record["status"], "released")
        self.assertGreater(record["expires"], self.manager.journal.clock() + 29 * 86400)
        self.assertTrue(Path(created["repository"]).exists())

    async def test_database_failure_does_not_execute_file_moves(self):
        import sqlite3
        folder = self.folder()
        with patch.object(self.manager.journal, "connect", side_effect=sqlite3.OperationalError("fixture damaged database")):
            result = await self.call("trash_put", paths=[str(folder)])
        self.assertEqual(result["error"], "storage_operation_failed")
        self.assertTrue((folder / "binary.bin").exists())
        self.approve.assert_not_awaited()

    @unittest.skipUnless(os.name == "nt", "NTFS alternate stream acceptance")
    async def test_ntfs_alternate_stream_is_refused_without_data_loss(self):
        folder = self.folder()
        extra = str(folder / "main.py") + ":fixture-stream"
        Path(extra).write_text("fixture additional data")
        result = await self.call("trash_put", paths=[str(folder)])
        self.assertEqual(result["error"], "storage_stream_unsupported")
        self.assertEqual(Path(extra).read_text(), "fixture additional data")
        Path(extra).unlink()

    async def test_opposite_order_batches_complete_without_deadlock(self):
        first, second = self.folder("one"), self.folder("two")
        from copy import copy
        mcp = copy(self.mcp)
        mcp.authorization = Authorization()
        other = WorkspaceTools(mcp, self.env, conversation_id="fixture-other")
        gateway = ToolGateway(mcp.authorization, other.registrations(), AsyncMock(return_value=True))
        actions = [ToolAction("workspace_trash_put", {"paths": [str(first), str(second)]}),
                   ToolAction("workspace_trash_put", {"paths": [str(second), str(first)]})]
        results = await asyncio.wait_for(asyncio.gather(self.gateway.execute(actions[0]), gateway.execute(actions[1])), timeout=15)
        self.assertEqual(sum(result.result.get("success", False) for result in results), 1, results)

    async def test_partial_purge_can_resume_for_trash_and_scratch(self):
        folder = self.folder()
        identifier = await self.put(folder)
        created = await self.call("scratch_create", repository=str(self.root))
        Path(created["repository"], "held.txt").write_text("fixture temporary")
        self.manager.close()
        original = Path.unlink
        def fail(path, *args, **kwargs):
            if path.name in {"binary.bin", "held.txt"}:
                raise PermissionError("fixture file busy")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail):
            result = await self.call("trash_purge", ids=[identifier, created["id"]])
        self.assertFalse(result["success"])
        for value in (identifier, created["id"]):
            result = await self.call("storage_recover", id=value)
            self.assertTrue(result.get("success"), result)
            self.assertEqual(result["status"], "purged")
        self.assertFalse(folder.exists())
        self.assertFalse(Path(created["repository"]).exists())

    async def test_partial_cross_volume_copy_retains_original_and_partial_payload(self):
        folder = self.folder()
        before = snapshot(folder)
        def partial(source, destination, expected, cancel):
            destination.mkdir()
            (destination / "binary.bin").write_bytes(b"fixture partial copy")
            raise OSError("fixture copy interrupted")
        with patch("dianstar.tools.local.storage.tree.os.rename", side_effect=OSError(errno.EXDEV, "fixture cross-volume")), \
                patch("dianstar.tools.local.storage.trash.copy_tree", side_effect=partial):
            result = await self.call("trash_put", paths=[str(folder)])
        self.assertIn("error", result)
        record = self.manager.record(result["entries"][0]["id"])
        self.assertTrue(same(snapshot(folder), before))
        self.assertTrue(record["pinned"])
        self.assertEqual((self.manager.payload(record) / "binary.bin").read_bytes(), b"fixture partial copy")

    async def test_hardlinks_and_protected_standalone_files_are_refused(self):
        folder = self.folder()
        os.link(folder / "main.py", folder / "alias.py")
        result = await self.call("trash_put", paths=[str(folder)])
        self.assertEqual(result["error"], "storage_file_unsupported")
        (folder / "alias.py").unlink()
        (folder / ".env").write_text("FIXTURE_PLACEHOLDER=fixture-local-only")
        refused = await self.call("trash_put", paths=[str(folder / ".env")])
        self.assertIn("error", refused)
        # The opaque whole-directory backup still retains protected descendants.
        identifier = await self.put(folder)
        payload = self.manager.payload(self.manager.record(identifier))
        self.assertTrue((payload / ".env").exists())

    async def test_real_cross_process_scratch_lease_survives_until_process_exits(self):
        script = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from dianstar.config.mcp import MCPSettings
from dianstar.security.authorization import Authorization
from dianstar.tools.local.workspace import WorkspaceTools
root, env = map(Path,sys.argv[1:])
mcp=SimpleNamespace(settings=MCPSettings(git_enabled=True,workspace_write_enabled=True),authorization=Authorization(),git=SimpleNamespace(repositories={str(root)}))
manager=WorkspaceTools(mcp,env,conversation_id="fixture-session").storage.manager
with manager.mutation():
    record=manager.create_scratch(str(root))
print(record["id"],flush=True)
sys.stdin.readline()
os._exit(72)
'''
        with subprocess.Popen([sys.executable, "-c", script, str(self.root), str(self.env)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            try:
                identifier = (await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), timeout=15)).strip()
                self.assertRegex(identifier, r"^[a-f0-9]{32}$")
                self.assertTrue(self.manager.live(identifier))
                opened = await self.call("scratch_open", id=identifier)
                self.assertEqual(opened["error"], "scratch_in_use")
                self.assertEqual(self.manager.abandoned(), [])
            finally:
                await asyncio.to_thread(child.communicate, input="exit\n", timeout=15)
        self.assertEqual(child.returncode, 72)
        self.assertFalse(self.manager.live(identifier))
        self.assertEqual(self.manager.abandoned()[0]["id"], identifier)

    async def test_restored_duplicate_is_preserved_if_published_target_has_changed(self):
        folder = self.folder()
        identifier = await self.put(folder)
        payload = self.manager.payload(self.manager.record(identifier))
        original = os.rename
        def cross(source, destination):
            if Path(source) == payload:
                raise OSError(errno.EXDEV, "fixture cross-volume")
            return original(source, destination)
        with patch("dianstar.tools.local.storage.tree.os.rename", side_effect=cross), \
                patch("dianstar.tools.local.storage.trash.remove_verified", side_effect=PermissionError("fixture busy")):
            result = await self.call("trash_restore", id=identifier)
        self.assertTrue(result["cleanup_pending"])
        (folder / "main.py").write_text("fixture new user work")
        result = await self.call("storage_recover", id=identifier)
        self.assertEqual(result["error"], "storage_recovery_conflict")
        self.assertTrue((payload / "binary.bin").exists())
        self.assertEqual((folder / "main.py").read_text(), "fixture new user work")

    async def test_hook_failure_blocks_move_and_automatic_scratch_creation(self):
        from dianstar.hooks.registry import HookRegistration
        from dianstar.hooks import HookRunner
        def broken(context, path, data):
            raise RuntimeError("fixture hook unavailable")
        self.tools.security = HookRunner(self.tools.security.scanner, additional_hooks=[HookRegistration(
            "fixture.storage.failure", ("before_trash", "before_scratch"), broken)])
        folder = self.folder()
        refused = await self.call("trash_put", paths=[str(folder)])
        self.assertIn("error", refused)
        refused = await self.call("scratch_create", repository=str(self.root))
        self.assertIn("error", refused)
        self.assertTrue(folder.exists())
        self.assertEqual(self.manager.journal.records("scratch"), [])

    async def test_scratch_command_inherits_original_project_session_environment(self):
        settings = await self.gateway.execute(ToolAction("workspace_environment_set", {
            "repository": str(self.root), "environment": {"CFLAGS": "fixture-build-scope"},
            "path_prepend": [], "expected_revision": 0}))
        self.assertNotIn("error", settings.result, settings.result)
        created = await self.call("scratch_create", repository=str(self.root))
        result = await self.gateway.execute(ToolAction("workspace_run", {"repository": created["repository"],
            "argv": [sys.executable, "-c", "import os; print(os.environ.get('CFLAGS'))"]}))
        self.assertNotIn("error", result.result, result.result)
        self.assertIn("fixture-build-scope", result.result["content"])
        self.assertTrue(result.result["success"], result.result)

    async def test_cancellation_after_first_move_compensates_batch_before_returning(self):
        first, second = self.folder("one"), self.folder("two")
        moved, release = threading.Event(), threading.Event()
        original = os.rename
        def pause(source, destination):
            result = original(source, destination)
            if Path(source) == first:
                moved.set()
                release.wait(timeout=5)
            return result
        with patch("dianstar.tools.local.storage.tree.os.rename", side_effect=pause):
            task = asyncio.create_task(self.call("trash_put", paths=[str(first), str(second)]))
            try:
                self.assertTrue(await asyncio.to_thread(moved.wait, 5))
                task.cancel()
                await asyncio.sleep(0)  # Deliver cancellation before releasing the I/O worker.
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue((first / "main.py").exists())
        self.assertTrue((second / "binary.bin").exists())
        self.assertTrue(all(record["status"] == "rolled_back" for record in self.manager.journal.records("trash")))
