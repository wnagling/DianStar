"""Real file transactions for patches and moves, including failure and undo."""
import asyncio
import codecs
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.tool_resources import ToolResources
from dianstar.core.resources import conflicts
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.files.inspection import inspect_changes
from dianstar.tools.local.workspace import WorkspaceTools
from tests import test_file_tools as fixtures


def document(body):
    return "*** Begin Patch\n" + body + "\n*** End Patch\n"


class FileTransformTests(unittest.IsolatedAsyncioTestCase):
    call = fixtures.FileToolTests.call

    def setUp(self):
        fixtures.FileToolTests.setUp(self)
        environment = patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.base / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.tools = WorkspaceTools(self.mcp, self.env)
        self.registry = self.tools.registrations()
        self.gateway = ToolGateway(self.auth, self.registry, self.approve)

    async def source(self, name="a.txt", data=b"one\ntwo\nthree\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        result = await self.call("read", path=name)
        self.assertEqual(result.get("type"), "file", result)
        return path

    async def test_multi_file_patch_move_and_whole_transaction_undo_redo(self):
        original = codecs.BOM_UTF8 + b"one\r\ntwo\r\nthree\r\n"
        await self.source(data=original)
        await self.source("obsolete.txt", b"obsolete\n")
        await self.source("helper.py", b"value = 1\n")
        result = await self.call("patch", patch=document(
            "*** Update File: a.txt\n@@\n one\n-two\n+changed\n three\n"
            "*** Add File: docs/new.txt\n+new\n"
            "*** Delete File: obsolete.txt\n"
            "*** Update File: helper.py\n*** Move to: src/helper.py\n@@\n-value = 1\n+value = 2"))
        self.assertTrue(result.get("success"), result)
        self.approve.assert_awaited_once()
        self.assertEqual((self.root / "a.txt").read_bytes(), codecs.BOM_UTF8 + b"one\r\nchanged\r\nthree\r\n")
        self.assertEqual((self.root / "src/helper.py").read_bytes(), b"value = 2\n")
        self.assertFalse((self.root / "helper.py").exists())
        self.assertFalse((self.root / "obsolete.txt").exists())
        view = inspect_changes(self.tools, [{"repository": str(self.root), "transaction_id": result["transaction_id"]}])
        self.assertFalse(view["issues"], view)
        self.assertTrue(any(item["moved_from"] == "helper.py" for item in view["files"]))
        undo = await self.call("file_restore", transaction_id=result["transaction_id"])
        self.assertTrue(undo.get("success"), undo)
        self.assertEqual((self.root / "a.txt").read_bytes(), original)
        self.assertEqual((self.root / "helper.py").read_bytes(), b"value = 1\n")
        self.assertFalse((self.root / "src").exists())
        self.assertFalse((self.root / "docs").exists())
        redo = await self.call("file_restore", transaction_id=undo["transaction_id"])
        self.assertTrue(redo.get("success"), redo)
        self.assertEqual((self.root / "src/helper.py").read_bytes(), b"value = 2\n")

    async def test_multiple_hunks_use_original_context_and_preserve_no_final_newline(self):
        await self.source(data=b"one\nx\nthree\nfour\nlast")
        result = await self.call("patch", patch=document(
            "*** Update File: a.txt\n@@\n one\n-x\n+first\n three\n@@\n four\n-last\n+final\n*** End of File"))
        self.assertTrue(result.get("success"), result)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"one\nfirst\nthree\nfour\nfinal")

    async def test_patch_preserves_untouched_mixed_endings_and_unicode_line_characters(self):
        original = "one\r\ntwo\nthree\rfour\u2028text\nlast".encode()
        await self.source(data=original)
        result = await self.call("patch", patch=document("*** Update File: a.txt\n@@\n-two\n+changed"))
        self.assertTrue(result.get("success"), result)
        self.assertEqual((self.root / "a.txt").read_bytes(), "one\r\nchanged\r\nthree\rfour\u2028text\nlast".encode())

    async def test_empty_file_creation_and_insertion_are_supported(self):
        result = await self.call("patch", patch=document("*** Add File: empty.txt"))
        self.assertTrue(result.get("success"), result)
        self.assertEqual((self.root / "empty.txt").read_bytes(), b"")
        await self.call("read", path="empty.txt")
        result = await self.call("patch", patch=document("*** Update File: empty.txt\n@@\n+new"))
        self.assertTrue(result.get("success"), result)
        self.assertEqual((self.root / "empty.txt").read_bytes(), b"new\n")

    async def test_ambiguous_context_rejected_but_explicit_eof_can_select_last(self):
        await self.source(data=b"x\nx\n")
        body = "*** Update File: a.txt\n@@\n-x\n+y"
        self.assertEqual((await self.call("patch", patch=document(body)))["error"], "file_patch_ambiguous")
        self.approve.assert_not_awaited()
        self.assertTrue((await self.call("patch", patch=document(body + "\n*** End of File"))).get("success"))
        self.assertEqual((self.root / "a.txt").read_bytes(), b"x\ny\n")

    async def test_later_invalid_context_has_no_earlier_changes_or_directories(self):
        await self.source()
        result = await self.call("patch", patch=document(
            "*** Add File: new/first.txt\n+new\n*** Update File: a.txt\n@@\n-missing\n+changed"))
        self.assertEqual(result["error"], "file_patch_mismatch")
        self.assertFalse((self.root / "new").exists())
        self.assertEqual((self.root / "a.txt").read_bytes(), b"one\ntwo\nthree\n")
        self.approve.assert_not_awaited()

    async def test_patch_rejects_malformed_duplicate_overlapping_and_escaping_paths(self):
        bad = ["--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y",
               document("*** Update File: a.txt\n@@ -1 +1 @@\n-x\n+y"),
               document("*** Add File: x.txt\n+x\n*** Add File: x.txt\n+y"),
               document("*** Add File: parent\n+x\n*** Add File: parent/child\n+y"),
               document("*** Add File: ../outside.txt\n+x"),
               document("*** Add File: .env\n+x"),
               document("*** Add File: x.txt\nmissing-prefix"),
               document("\n".join(f"*** Add File: {i}.txt\n+x" for i in range(21)))]
        for value in bad:
            with self.subTest(value=value[:70]):
                self.assertIn("error", await self.call("patch", patch=value))
        self.assertEqual(list(self.root.iterdir()), [])
        self.approve.assert_not_awaited()

    async def test_patch_add_never_overwrites_and_existing_sources_require_read(self):
        target = self.root / "a.txt"
        target.write_bytes(b"before\n")
        for body in ("*** Update File: a.txt\n@@\n-before\n+after", "*** Delete File: a.txt"):
            self.assertEqual((await self.call("patch", patch=document(body)))["error"], "file_read_required")
        await self.call("read", path="a.txt")
        self.assertEqual((await self.call("patch", patch=document("*** Add File: a.txt\n+replacement")))["error"], "file_destination_exists")
        self.assertEqual(target.read_bytes(), b"before\n")

    async def test_move_preserves_bytes_mode_and_supports_rename_then_undo(self):
        data = codecs.BOM_UTF8 + "你好\r\nlast".encode()
        source = await self.source(data=data)
        mode = source.stat().st_mode
        result = await self.call("move", source="a.txt", destination="src/renamed.txt")
        self.assertTrue(result.get("moved"), result)
        target = self.root / "src/renamed.txt"
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(target.stat().st_mode, mode)
        self.assertFalse(source.exists())
        summary = self.approve.call_args.args[0]["summary"]
        self.assertIn("从：a.txt", summary)
        self.assertIn("到：src/renamed.txt", summary)
        undo = await self.call("file_restore", transaction_id=result["transaction_id"])
        self.assertTrue(undo.get("success"), undo)
        self.assertEqual(source.read_bytes(), data)
        self.assertFalse(target.parent.exists())

    async def test_large_unchanged_move_has_compact_preview(self):
        data = b"ordinary content\n" * 4000
        await self.source(data=data)
        result = await self.call("move", source="a.txt", destination="renamed.txt")
        self.assertTrue(result.get("success"), result)
        self.assertLess(len(self.approve.call_args.args[0]["description"]), 1000)
        self.assertEqual((self.root / "renamed.txt").read_bytes(), data)
        undo = await self.call("file_restore", transaction_id=result["transaction_id"])
        self.assertTrue(undo.get("success"), undo)
        self.assertLess(len(self.approve.call_args.args[0]["description"]), 1000)
        self.assertEqual((self.root / "a.txt").read_bytes(), data)

    async def test_move_conflicts_and_unsupported_paths_never_overwrite(self):
        await self.source()
        await self.source("existing.txt", b"user\n")
        for target in ("existing.txt", "a.txt", "../outside.txt", ".env"):
            with self.subTest(target=target):
                self.assertIn("error", await self.call("move", source="a.txt", destination=target))
        directory = self.root / "directory"
        directory.mkdir()
        self.assertIn("error", await self.call("move", source="directory", destination="other"))
        self.assertEqual((self.root / "existing.txt").read_bytes(), b"user\n")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"one\ntwo\nthree\n")
        self.approve.assert_not_awaited()

    async def test_changes_to_source_or_target_during_confirmation_are_preserved(self):
        source = await self.source()
        async def change(_):
            source.write_text("user changed")
            return True
        self.gateway.request_approval = change
        self.assertEqual((await self.call("move", source="a.txt", destination="new.txt"))["error"], "file_changed")
        self.assertFalse((self.root / "new.txt").exists())
        await self.call("read", path="a.txt")
        async def create(_):
            (self.root / "new.txt").write_text("user target")
            return True
        self.gateway.request_approval = create
        self.assertEqual((await self.call("move", source="a.txt", destination="new.txt"))["error"], "file_changed")
        self.assertEqual(source.read_text(), "user changed")
        self.assertEqual((self.root / "new.txt").read_text(), "user target")

    async def test_patch_write_failure_compensates_created_and_deleted_files(self):
        await self.source(data=b"before\n")
        await self.source("old.txt", b"old\n")
        original = os.replace
        def fail(source, destination):
            if Path(destination).name == "a.txt":
                raise OSError("fixture disk failure")
            return original(source, destination)
        with patch("dianstar.tools.local.files.recovery.os.replace", side_effect=fail):
            result = await self.call("patch", patch=document(
                "*** Add File: new/created.txt\n+new\n*** Delete File: old.txt\n*** Update File: a.txt\n@@\n-before\n+after"))
        self.assertEqual(result["recovery"]["status"], "rolled_back", result)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"before\n")
        self.assertEqual((self.root / "old.txt").read_bytes(), b"old\n")
        self.assertFalse((self.root / "new").exists())

    async def test_move_source_removal_failure_rolls_back_destination(self):
        source = await self.source()
        original = Path.unlink
        def fail(path, *args, **kwargs):
            if path == source:
                raise OSError("fixture source busy")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail):
            result = await self.call("move", source="a.txt", destination="new/target.txt")
        self.assertEqual(result["recovery"]["status"], "rolled_back", result)
        self.assertEqual(source.read_bytes(), b"one\ntwo\nthree\n")
        self.assertFalse((self.root / "new").exists())

    async def test_sensitive_content_cannot_be_laundered_by_move_or_patch(self):
        await self.source(data=b"fixture-application-key\n")
        result = await self.call("move", source="a.txt", destination="public.txt")
        self.assertEqual(result["error"], "sensitive_content_denied")
        result = await self.call("patch", patch=document("*** Add File: public.txt\n+fixture-application-key"))
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertNotIn("fixture-application-key", json.dumps(result))
        self.assertFalse((self.root / "public.txt").exists())
        self.approve.assert_not_awaited()

    async def test_base_tool_denials_and_readonly_policy_apply_to_composites(self):
        for disabled, names in (("write", ("patch", "move")), ("workspace_delete", ("patch", "move")), ("edit", ("patch",))):
            gateway = ToolGateway(self.auth, self.registry, self.approve, policies={disabled: ToolPolicy(enabled=False)})
            for name in names:
                args = {"patch": document("*** Add File: new.txt\n+x")} if name == "patch" else {"source": "a.txt", "destination": "new.txt"}
                result = await gateway.execute(ToolAction(name, {"repository": str(self.root), **args}))
                self.assertEqual(result.result["error"], "tool_disabled")

    def test_resource_claims_cover_patch_sources_targets_and_move(self):
        planner = ToolResources(self.registry, None, None)
        move = planner(ToolAction("move", {"repository": str(self.root), "source": "a.txt", "destination": "b.txt"}))
        for path in ("a.txt", "b.txt"):
            read = planner(ToolAction("read", {"repository": str(self.root), "path": path}))
            self.assertTrue(conflicts(move, read))
        other = planner(ToolAction("write", {"repository": str(self.root), "path": "other.txt"}))
        self.assertFalse(conflicts(move, other))
        change = planner(ToolAction("patch", {"repository": str(self.root), "patch": document("*** Update File: a.txt\n*** Move to: b.txt")}))
        self.assertTrue(conflicts(change, move))

    async def test_concurrent_moves_of_same_source_have_exactly_one_winner(self):
        await self.source()
        auth = Authorization()
        tools = WorkspaceTools(SimpleNamespace(settings=self.mcp.settings, authorization=auth, git=self.mcp.git), self.env)
        entered, both = 0, asyncio.Event()
        async def approve(_):
            nonlocal entered
            entered += 1
            if entered == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 5)
            return True
        second = ToolGateway(auth, tools.registrations(), approve)
        await second.execute(ToolAction("read", {"repository": str(self.root), "path": "a.txt"}))
        self.gateway.request_approval = approve
        results = await asyncio.gather(self.call("move", source="a.txt", destination="one.txt"),
            second.execute(ToolAction("move", {"repository": str(self.root), "source": "a.txt", "destination": "two.txt"})))
        results[1] = results[1].result
        self.assertEqual(sum(bool(item.get("success")) for item in results), 1, results)
        self.assertEqual(sum(item.get("error") == "file_changed" for item in results), 1, results)
        self.assertEqual(sum((self.root / name).exists() for name in ("one.txt", "two.txt")), 1)

    async def test_cancel_after_destination_publication_waits_for_compensation(self):
        await self.source()
        entered, release = threading.Event(), threading.Event()
        original = os.rename
        def pause(source, destination):
            result = original(source, destination)
            if Path(destination).name == "target.txt":
                entered.set()
                release.wait(5)
            return result
        if os.name != "nt":
            self.skipTest("Windows publication path; other platforms use link")
        with patch("dianstar.tools.local.files.recovery.os.rename", side_effect=pause):
            task = asyncio.create_task(self.call("move", source="a.txt", destination="new/target.txt"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue((self.root / "a.txt").exists())
        self.assertFalse((self.root / "new").exists())

    async def test_process_crash_after_source_deletion_can_restore_move(self):
        await self.source(data=b"original\n")
        script = r'''
import asyncio,os,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.workspace import WorkspaceTools
root,env=Path(sys.argv[1]),Path(sys.argv[2])
auth=Authorization()
tools=WorkspaceTools(SimpleNamespace(settings=MCPSettings(git_enabled=True,workspace_write_enabled=True),
    authorization=auth,git=SimpleNamespace(repositories={str(root)})),env)
gateway=ToolGateway(auth,tools.registrations(),AsyncMock(return_value=True))
original=Path.unlink
def crash(path,*args,**kwargs):
    result=original(path,*args,**kwargs)
    if path==root/'a.txt': os._exit(23)
    return result
async def main():
    await gateway.execute(ToolAction('read',{'repository':str(root),'path':'a.txt'}))
    Path.unlink=crash
    await gateway.execute(ToolAction('move',{'repository':str(root),'source':'a.txt','destination':'new/b.txt'}))
asyncio.run(main())
'''
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(self.root), str(self.env)],
            capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual((self.root / "new/b.txt").read_bytes(), b"original\n")
        history = await self.call("file_history")
        restored = await self.call("file_restore", transaction_id=history["transactions"][0]["transaction_id"])
        self.assertTrue(restored.get("success"), restored)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"original\n")
        self.assertFalse((self.root / "new").exists())
