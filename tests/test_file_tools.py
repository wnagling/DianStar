"""File tool acceptance with real temporary files, bounded search and protected writes."""
import codecs
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.hooks import HookRunner
from dianstar.hooks.registry import HookRegistration
from dianstar.security.authorization import Authorization
from dianstar.security.content_guard import Finding
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.files import migrate_file_policies
from dianstar.tools.local.workspace import WorkspaceTools


class FileToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_file_reads_can_hold_shared_locks_concurrently(self):
        import asyncio
        from dianstar.tools.local.files.read import load as original_load
        (self.root / "a.txt").write_text("a")
        (self.root / "b.txt").write_text("b")
        barrier = threading.Barrier(2)
        def simultaneous(path):
            if path.name in {"a.txt", "b.txt"}:
                barrier.wait(timeout=3)
            return original_load(path)
        with patch("dianstar.tools.local.files.read.load", side_effect=simultaneous):
            results = await asyncio.gather(self.call("read", path="a.txt"), self.call("read", path="b.txt"))
        self.assertTrue(all(result.get("type") == "file" for result in results), results)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.env = self.base / ".env"
        self.env.write_text("API_KEY=fixture-application-key\n", encoding="utf-8")
        self.auth = Authorization()
        self.mcp = SimpleNamespace(settings=MCPSettings(git_enabled=True, workspace_write_enabled=True),
                                   authorization=self.auth, git=SimpleNamespace(repositories={str(self.root)}))
        self.tools = WorkspaceTools(self.mcp, self.env)
        self.approve = AsyncMock(return_value=True)
        self.registry = self.tools.registrations()
        self.gateway = ToolGateway(self.auth, self.registry, self.approve)

    async def call(self, name, **args):
        return (await self.gateway.execute(ToolAction(name, {"repository": str(self.root), **args}))).result

    async def test_first_batch_names_and_create_read_edit_write(self):
        self.assertTrue({"read", "glob", "grep", "edit", "write"} <= self.registry.keys())
        self.assertFalse({"workspace_read", "workspace_edit", "workspace_search", "workspace_list"} & self.registry.keys())
        self.assertTrue((await self.call("write", path="src/a.py", content="value = 1\n"))["created"])
        self.assertEqual(self.approve.await_count, 1)
        self.assertEqual((await self.call("write", path="src/a.py", content="value = 2\n"))["error"], "file_read_required")
        read = await self.call("read", path="src/a.py")
        self.assertIn("1: value = 1", read["content"])
        self.assertTrue((await self.call("edit", path="src/a.py", old_string="value = 1", new_string="value = 2"))["success"])
        await self.call("read", path="src/a.py")
        self.assertTrue((await self.call("write", path="src/a.py", content="replacement\n"))["success"])
        self.assertEqual((self.root / "src/a.py").read_text(), "replacement\n")

    async def test_read_has_line_paging_and_directory_listing(self):
        (self.root / "empty").mkdir()
        (self.root / "a.txt").write_text("first\nsecond\nthird\n")
        result = await self.call("read", path="a.txt", offset=2, limit=1)
        self.assertEqual(result["content"], "2: second")
        self.assertEqual(result["next_offset"], 3)
        self.assertIn("a.txt", (await self.call("read", path="."))["content"])
        self.assertIn("empty/", (await self.call("read", path="."))["content"])
        self.approve.assert_not_awaited()

    async def test_glob_and_grep_filter_paths_and_return_line_numbers(self):
        (self.root / "src").mkdir()
        (self.root / "src/a.py").write_text("one\nHello World\n", encoding="utf-8")
        (self.root / "root.py").write_text("Hello root\n")
        (self.root / "note.txt").write_text("Hello note\n")
        result = await self.call("glob", pattern="**/*.py", limit=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_offset"], 1)
        self.assertIn("src/a.py", (await self.call("glob", path="src", pattern="*.py"))["content"])
        found = await self.call("grep", pattern="hello (World|root)", case_sensitive=False, include="*.py")
        self.assertIn("src/a.py:2: Hello World", found["content"])
        self.assertNotIn("note.txt", found["content"])
        self.assertTrue(found["complete"])
        self.approve.assert_not_awaited()

    async def test_regex_errors_and_timeout_are_not_empty_successes(self):
        (self.root / "a.txt").write_text("a" * 8000 + "!\n")
        self.assertEqual((await self.call("grep", pattern="["))["error"], "file_invalid_regex")
        self.assertEqual((await self.call("grep", pattern="(a+)+$"))["error"], "file_search_timeout")
        self.assertIn("a.txt:1:", (await self.call("grep", pattern="!", literal=True))["content"])

    async def test_sensitive_paths_and_known_credentials_never_reach_model(self):
        (self.root / ".env").write_text("private data")
        (self.root / "ordinary.txt").write_text("fixture-application-key")
        for path in (".env", "../outside.txt", "C:/outside.txt", ".git/config"):
            self.assertIn("error", await self.call("read", path=path))
        result = await self.call("read", path="ordinary.txt")
        self.assertNotIn("fixture-application-key", result["content"])
        self.assertTrue(result["redacted"])
        found = await self.call("grep", pattern="fixture", literal=True)
        self.assertNotIn("fixture-application-key", json.dumps(found))
        self.assertGreater(found["redacted_files"], 0)
        result = await self.call("write", path="new.txt", content="fixture-application-key")
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertFalse((self.root / "new.txt").exists())
        self.approve.assert_not_awaited()

    async def test_bom_and_crlf_survive_edit_and_whole_file_write(self):
        path = self.root / "a.py"
        path.write_bytes(codecs.BOM_UTF8 + b"one\r\ntwo\r\n")
        await self.call("read", path="a.py")
        self.assertTrue((await self.call("edit", path="a.py", old_string="one\ntwo", new_string="first\nsecond"))["success"])
        self.assertEqual(path.read_bytes(), codecs.BOM_UTF8 + b"first\r\nsecond\r\n")
        await self.call("read", path="a.py")
        self.assertTrue((await self.call("write", path="a.py", content="next\nline\n"))["success"])
        self.assertEqual(path.read_bytes(), codecs.BOM_UTF8 + b"next\r\nline\r\n")

    async def test_redaction_preserves_line_numbers_and_yaml_values(self):
        secret = "ghp_" + "B2c3D4e5" * 5
        (self.root / "settings.yml").write_text(f"api_key: {secret}\nport: 8000\n")
        result = await self.call("read", path="settings.yml")
        self.assertNotIn(secret, result["content"])
        self.assertIn("2: port: 8000", result["content"])
        # Generate an invalid private-key fixture; no real key material is stored.
        fixture_key = ["-----BEGIN " + "PRIVATE KEY-----", "hidden-body", "-----END " + "PRIVATE KEY-----", "after"]
        (self.root / "ordinary.txt").write_text("\n".join(fixture_key) + "\n")
        result = await self.call("read", path="ordinary.txt", offset=2)
        self.assertNotIn("hidden-body", result["content"])
        self.assertIn("4: after", result["content"])
        self.assertEqual((await self.call("write", path="ordinary.txt", content="[已遮挡私钥]"))["error"], "file_redacted_content")
        (self.root / "legacy.txt").write_bytes(("\r".join(fixture_key) + "\r").encode())
        result = await self.call("read", path="legacy.txt", offset=2)
        self.assertNotIn("hidden-body", result["content"])
        self.assertIn("4: after", result["content"])

    async def test_failed_reads_do_not_authorize_overwrite_and_atomic_failure_keeps_original(self):
        target = self.root / "a.txt"
        target.write_text("before")
        self.assertIn("error", await self.call("read", path="a.txt", offset=100))
        self.assertEqual((await self.call("write", path="a.txt", content="after"))["error"], "file_read_required")
        await self.call("read", path="a.txt")
        with patch("dianstar.tools.local.files.write.os.replace", side_effect=OSError("fixture replace failure")):
            self.assertIn("error", await self.call("write", path="a.txt", content="after"))
        self.assertEqual(target.read_text(), "before")
        self.assertEqual([p.name for p in self.root.iterdir()], ["a.txt"])

    async def test_read_hook_and_concurrent_creator_block_mutations(self):
        (self.root / "a.txt").write_text("before")
        self.tools.security = HookRunner(additional_hooks=(HookRegistration("fixture.read", ("before_read",),
            lambda _, path, data: [Finding(path, 1, "读取拒绝")]),))
        self.assertEqual((await self.call("read", path="a.txt"))["error"], "sensitive_content_denied")
        self.assertEqual((await self.call("write", path="a.txt", content="after"))["error"], "file_read_required")
        self.tools.security = HookRunner()
        async def create(_):
            (self.root / "new.txt").write_text("user created")
            return True
        self.gateway.request_approval = create
        self.assertEqual((await self.call("write", path="new.txt", content="agent content"))["error"], "file_changed")
        self.assertEqual((self.root / "new.txt").read_text(), "user created")

    def test_worker_resource_and_packaged_entry_protocol(self):
        from dianstar.tools.local.files.read import WORKER_SOURCE
        payload = json.dumps({"files": [{"path": "a.txt", "text": "hello"}], "pattern": "hello", "literal": True,
                              "case_sensitive": True, "offset": 0, "limit": 1})
        command = [sys.executable, "scripts/dianstar_entry.py", "--file-search-worker", hashlib.sha256(WORKER_SOURCE.encode()).hexdigest()]
        result = subprocess.run(command, input=payload, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["matches"][0]["line"], 1)
        result = subprocess.run([*command[:-1], "0" * 64], input=payload, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)

    async def test_stale_reads_and_changes_during_approval_are_preserved(self):
        path = self.root / "a.txt"
        path.write_text("before")
        await self.call("read", path="a.txt")
        path.write_text("user before preview")
        self.assertEqual((await self.call("edit", path="a.txt", old_string="before", new_string="after"))["error"], "file_read_required")
        await self.call("read", path="a.txt")
        async def change(_):
            path.write_text("user during approval")
            return True
        self.gateway.request_approval = change
        self.assertEqual((await self.call("write", path="a.txt", content="replacement"))["error"], "file_changed")
        self.assertEqual(path.read_text(), "user during approval")

    async def test_unique_match_and_explicit_replace_all(self):
        (self.root / "a.txt").write_text("x x")
        await self.call("read", path="a.txt")
        self.assertEqual((await self.call("edit", path="a.txt", old_string="x", new_string="y"))["error"], "file_match_failed")
        self.assertTrue((await self.call("edit", path="a.txt", old_string="x", new_string="y", replace_all=True))["success"])
        self.assertEqual((self.root / "a.txt").read_text(), "y y")

    async def test_denial_and_hook_exception_have_no_file_side_effects(self):
        self.gateway.request_approval = AsyncMock(return_value=False)
        self.assertIn("error", await self.call("write", path="dir/a.txt", content="data"))
        self.assertFalse((self.root / "dir").exists())
        def broken(*_):
            raise RuntimeError("fixture callback failure")
        self.tools.security = HookRunner(additional_hooks=(HookRegistration("fixture.failure", ("before_write",), broken),))
        self.gateway = ToolGateway(self.auth, self.registry, self.approve, policies={"write": ToolPolicy(requires_approval=False)})
        self.assertEqual((await self.call("write", path="dir/a.txt", content="other"))["error"], "security_scan_incomplete")
        self.assertFalse((self.root / "dir").exists())

    async def test_links_do_not_escape_project(self):
        outside = self.base / "outside.txt"
        outside.write_text("outside")
        linked = self.root / "linked.txt"
        try:
            os.link(outside, linked)
        except OSError:
            self.skipTest("Hard links unavailable")
        self.assertIn("error", await self.call("read", path="linked.txt"))
        self.assertIn("error", await self.call("write", path="linked.txt", content="change"))
        self.assertEqual(outside.read_text(), "outside")

    async def test_parent_link_swapped_during_approval_does_not_write_outside(self):
        outside = self.base / "outside"
        outside.mkdir()
        parent = self.root / "destination"
        parent.mkdir()
        probe = self.root / "probe"
        try:
            probe.symlink_to(outside, target_is_directory=True)
            probe.unlink()
        except OSError:
            self.skipTest("Directory symlinks unavailable")
        async def change(_):
            parent.rmdir()
            parent.symlink_to(outside, target_is_directory=True)
            return True
        self.gateway.request_approval = change
        self.assertIn("error", await self.call("write", path="destination/new.txt", content="must stay inside"))
        self.assertFalse((outside / "new.txt").exists())

    async def test_empty_new_file_and_literal_redaction_documentation_are_allowed(self):
        self.assertTrue((await self.call("write", path="empty.txt", content=""))["created"])
        self.assertEqual((self.root / "empty.txt").read_bytes(), b"")
        text = "显示 [已遮挡密钥] 代表读取时进行了脱敏。"
        self.assertTrue((await self.call("write", path="guide.txt", content=text))["created"])
        self.assertEqual((self.root / "guide.txt").read_text(encoding="utf-8"), text)

    def test_old_backend_denials_cannot_be_bypassed_by_new_names(self):
        policies = migrate_file_policies({"workspace_edit": ToolPolicy(enabled=False), "write": ToolPolicy(enabled=True)})
        self.assertFalse(policies["edit"].enabled)
        self.assertFalse(policies["write"].enabled)


class GitIgnoreFileTests(unittest.IsolatedAsyncioTestCase):
    setUp = FileToolTests.setUp
    call = FileToolTests.call

    async def test_nested_ignore_negation_hidden_and_protected_files(self):
        subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True, capture_output=True)
        (self.root / ".gitignore").write_text("ignored/\n*.tmp\n!keep.tmp\n")
        (self.root / "ignored").mkdir()
        (self.root / "ignored/no.py").write_text("needle")
        (self.root / "skip.tmp").write_text("needle")
        (self.root / "keep.tmp").write_text("needle")
        (self.root / ".hidden.py").write_text("needle")
        (self.root / ".env").write_text("needle")
        result = await self.call("glob", pattern="**/*", hidden=True)
        self.assertIn("keep.tmp", result["content"])
        self.assertIn(".hidden.py", result["content"])
        self.assertNotIn("skip.tmp", result["content"])
        self.assertNotIn("no.py", result["content"])
        self.assertNotIn(".env", result["content"])
