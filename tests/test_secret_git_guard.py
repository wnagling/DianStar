"""Security acceptance against real temporary repositories and local bare remotes."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from dianstar.security.content_guard import ContentScanner, SecurityViolation, FINGERPRINT_ENV, fingerprint_payload, MAX_FILE
from dianstar.tools.mcp.servers.git.workspace import GitWorkspace
from tests.test_git_operations import GitFixture


TOKEN = "ghp_" + "B2c3D4e5" * 5


class SecretGitGuardTests(GitFixture, unittest.TestCase):
    def setUp(self):
        self.setup()

    def tearDown(self):
        self.temporary.cleanup()

    def raw_commit(self, message="external fixture"):
        return self.git("-c", "core.hooksPath=" + os.devnull, "commit", "-m", message)

    def test_placeholder_template_is_committable_but_real_env_is_not(self):
        (self.repo / ".env.example").write_text("API_KEY=your-key-here\n")
        self.apply("git_stage", files=[".env.example"])
        self.apply("git_commit", message="add configuration template")
        (self.repo / ".env").write_text("API_KEY=" + TOKEN)
        self.assertIn("error", self.workspace.prepare("git_stage", str(self.repo), files=[".env"]))
        self.apply("git_push", branch="main")
        self.assertEqual(self.git("ls-tree", "--name-only", "refs/heads/main", cwd=self.remote), ".env.example\none.txt\ntwo.txt")

    def test_renamed_secret_is_denied_before_staging_without_exposing_it(self):
        (self.repo / "normal.json").write_text(json.dumps({"access_token": TOKEN}))
        result = self.workspace.prepare("git_stage", str(self.repo), files=["normal.json"])
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_staged_blob_is_checked_instead_of_clean_working_copy(self):
        (self.repo / "normal.txt").write_text(TOKEN)
        self.git("add", "normal.txt")
        (self.repo / "normal.txt").write_text("clean working copy")
        head = self.git("rev-parse", "HEAD")
        result = self.workspace.prepare("git_commit", str(self.repo), message="attempt")
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_credential_module_can_stage_commit_and_push_as_unchanged_source(self):
        target = self.repo / "dianstar/config/credentials.py"
        target.parent.mkdir(parents=True)
        target.write_text('def read_api_key():\n    return "${SERVICE_TOKEN}"\n')
        self.apply("git_stage", files=["dianstar/config/credentials.py"])
        self.apply("git_commit", message="add credential management module")
        (self.repo / "one.txt").write_text("unrelated safe change")
        self.apply("git_stage", files=["one.txt"])
        self.apply("git_commit", message="update ordinary file")
        self.apply("git_push", branch="main")
        self.assertEqual(self.git("show", "refs/heads/main:dianstar/config/credentials.py", cwd=self.remote),
                         target.read_text().strip())

    def test_old_index_fixture_is_reported_and_explicit_restage_fixes_it(self):
        target = self.repo / "tests/test_mcp.py"
        target.parent.mkdir()
        old_value = "must-not-inherit"
        target.write_text('patch.dict(os.environ, ' + json.dumps({"UNRELATED_SECRET": old_value}) + ')\n')
        self.git("add", "tests/test_mcp.py")
        target.write_text('patch.dict(os.environ, {"UNRELATED_SECRET": "fixture-must-not-inherit"})\n')
        index_before = self.git("ls-files", "--stage")
        head_before = self.git("rev-parse", "HEAD")
        result = self.workspace.prepare("git_commit", str(self.repo), message="attempt")
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertEqual(result["source"], "git_index")
        self.assertIn("完整暂存快照", result["message"])
        self.assertIn("重新暂存", result["message"])
        self.assertEqual(result["findings"][0]["path"], "tests/test_mcp.py")
        self.assertEqual(self.git("ls-files", "--stage"), index_before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        self.apply("git_stage", files=["tests/test_mcp.py"])
        self.apply("git_commit", message="use explicit fixture value")
        self.apply("git_push", branch="main")
        self.assertEqual(self.git("show", "refs/heads/main:tests/test_mcp.py", cwd=self.remote),
                         target.read_text().strip())

    def test_clean_index_can_commit_while_unrelated_local_secret_remains_unstaged(self):
        (self.repo / "normal.txt").write_text("clean staged copy")
        self.git("add", "normal.txt")
        (self.repo / "normal.txt").write_text(TOKEN)
        self.apply("git_commit", message="clean index only")
        self.apply("git_push", branch="main")
        self.assertEqual(self.git("show", "refs/heads/main:normal.txt", cwd=self.remote), "clean staged copy")
        self.assertEqual((self.repo / "normal.txt").read_text(), TOKEN)

    def test_existing_tracked_env_can_be_removed_from_index_without_deleting_local_file(self):
        target = self.repo / ".env"
        target.write_text("local-only-setting")
        self.git("add", "-f", ".env")
        self.raw_commit()
        self.git("rm", "--cached", ".env")
        self.apply("git_commit", message="stop tracking local environment")
        self.assertTrue(target.exists())
        self.assertNotIn(".env", self.git("ls-tree", "--name-only", "HEAD").splitlines())

    def test_deleted_secret_in_intermediate_history_blocks_push_and_leaves_remote_unchanged(self):
        self.apply("git_push", branch="main")
        remote_before = self.git("rev-parse", "refs/heads/main", cwd=self.remote)
        path = self.repo / "normal.txt"
        path.write_text(TOKEN)
        self.git("add", "normal.txt")
        self.raw_commit("external secret fixture")
        self.git("rm", "normal.txt")
        self.raw_commit("delete external fixture")
        result = self.workspace.prepare("git_push", str(self.repo), branch="main")
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.remote), remote_before)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_initial_push_scans_entire_history_even_with_clean_head(self):
        path = self.repo / ".env"
        path.write_text("local-only-setting")
        self.git("add", "-f", ".env")
        self.raw_commit()
        self.git("rm", ".env")
        self.raw_commit()
        result = self.workspace.prepare("git_push", str(self.repo), branch="main")
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertEqual(self.git("for-each-ref", cwd=self.remote), "")

    def test_known_key_fingerprint_catches_nonstandard_key_in_plain_file(self):
        value = "unusual-synthetic-application-key"
        with patch.dict(os.environ, {FINGERPRINT_ENV: fingerprint_payload([value])}):
            workspace = GitWorkspace([self.repo], allow_writes=True)
        (self.repo / "ordinary.txt").write_text(value)
        result = workspace.prepare("git_stage", str(self.repo), files=["ordinary.txt"])
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertNotIn(value, json.dumps(result))

    def test_scan_failure_before_apply_does_not_create_commit(self):
        (self.repo / "one.txt").write_text("safe change")
        self.apply("git_stage", files=["one.txt"])
        plan = self.prepare("git_commit", message="safe fixture")
        head = self.git("rev-parse", "HEAD")
        with patch.object(self.workspace.security, "tree", side_effect=SecurityViolation(incomplete=True)):
            result = self.workspace.apply(plan["token"])
        self.assertEqual(result["error"], "security_scan_incomplete")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_accidentally_staged_env_can_be_unstaged_without_exposing_credentials(self):
        path = self.repo / ".env"
        path.write_text("API_KEY=" + TOKEN)
        self.git("add", "-f", ".env")
        plan = self.prepare("git_unstage", files=[".env"])
        self.assertNotIn(TOKEN, plan["description"])
        self.assertTrue(self.workspace.apply(plan["token"])["success"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertTrue(path.exists())

    def test_shallow_history_is_not_treated_as_complete(self):
        self.apply("git_push", branch="main")
        shallow = self.root / "shallow"
        self.git("clone", "--depth=1", "--branch=main", self.remote.as_uri(), str(shallow))
        self.git("config", "remote.origin.url", str(self.remote), cwd=shallow)
        workspace = GitWorkspace([shallow], allow_writes=True)
        result = workspace.prepare("git_push", str(shallow), branch="main")
        self.assertEqual(result["error"], "security_scan_incomplete")

    def test_large_object_fails_closed_before_stage(self):
        (self.repo / "large.bin").write_bytes(b"a" * (MAX_FILE + 1))
        result = self.workspace.prepare("git_stage", str(self.repo), files=["large.bin"])
        self.assertEqual(result["error"], "security_scan_incomplete")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_remote_change_during_approval_invalidates_push(self):
        self.apply("git_push", branch="main")
        (self.repo / "one.txt").write_text("safe change")
        self.apply("git_stage", files=["one.txt"])
        self.apply("git_commit", message="safe change")
        plan = self.prepare("git_push", branch="main")
        self.git("update-ref", "-d", "refs/heads/main", cwd=self.remote)
        remote = self.git("for-each-ref", cwd=self.remote)
        result = self.workspace.apply(plan["token"])
        self.assertEqual(result["error"], "git_state_changed")
        self.assertEqual(self.git("for-each-ref", cwd=self.remote), remote)

    def test_clone_reports_tracked_env_without_deleting_or_exposing_it(self):
        (self.repo / ".env").write_text("API_KEY=" + TOKEN)
        self.git("add", "-f", ".env")
        self.raw_commit()
        workspace = GitWorkspace([], allow_writes=True, workspace_roots=[str(self.root)])
        destination = self.root / "clone"
        plan = workspace.prepare("git_clone", str(destination), url=str(self.repo))
        self.assertNotIn("error", plan)
        result = workspace.apply(plan["token"])
        self.assertTrue(result["success"])
        self.assertTrue(result["security_review"]["findings"])
        self.assertEqual((destination / ".env").read_text(), "API_KEY=" + TOKEN)
        self.assertNotIn(TOKEN, json.dumps(result))
