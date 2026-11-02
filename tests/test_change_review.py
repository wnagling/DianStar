import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.change_review import ChangeReview
from dianstar.app.turn_log import TurnLog
from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction, Observation
from dianstar.security.authorization import Authorization
from dianstar.security.content_guard import ContentScanner
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.workspace import WorkspaceTools


class ChangeReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.env = self.base / "application.env"
        self.env.write_text("API_KEY=fixture-model-key\n")
        override = patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.base / "state")})
        override.start()
        self.addCleanup(override.stop)
        self.auth = Authorization()
        self.context = WorkspaceTools(SimpleNamespace(settings=MCPSettings(git_enabled=True, workspace_write_enabled=True),
            authorization=self.auth, git=SimpleNamespace(repositories={str(self.root)})), self.env)
        self.log = TurnLog(self.auth.request_id, ContentScanner(known=("fixture-private-secret",)), conversation_id="conversation-one")
        self.review = ChangeReview(lambda: self.context, self.log)
        self.registry = {**self.context.registrations(), **self.review.registrations()}
        self.gateway = ToolGateway(self.auth, self.registry, AsyncMock(return_value=True))
        self.counter = 0

    async def call(self, name, **args):
        self.counter += 1
        if name not in {"review_changes", "record_review", "task_status"}:
            args = {"repository": str(self.root), **args}
        action = ToolAction(name, args, f"call-{self.counter}", json.dumps(args))
        result = await self.gateway.execute(action)
        self.review.observe(result, self.registry[name])
        return result.result

    async def test_diff_uses_user_working_copy_and_assessment_needs_real_evidence(self):
        (self.root / "a.txt").write_text("user existing value 2\n")
        await self.call("read", path="a.txt")
        written = await self.call("edit", path="a.txt", old_string="value 2", new_string="value 3")
        self.assertFalse(await self.review.ready())
        snapshot = await self.call("review_changes")
        self.assertIn("-user existing value 2", snapshot["files"][0]["diff"])
        self.assertIn("+user existing value 3", snapshot["files"][0]["diff"])
        self.assertIn(written["transaction_id"], snapshot["files"][0]["transaction_ids"])
        self.assertTrue(snapshot["untrusted"])
        rejected = await self.call("record_review", decision="keep", reason="looks good", evidence_call_ids=["invented-test"])
        self.assertEqual(rejected["error"], "invalid_review_evidence")
        result = await self.call("record_review", decision="keep", reason="Only text change reviewed, no tests run",
                                 evidence_call_ids=[snapshot["evidence_call_id"]])
        self.assertTrue(result["recorded"])
        self.assertTrue(await self.review.ready())
        self.assertIn("未取得当前修改后的测试通过记录", self.review.summary())

    async def test_new_edits_and_external_changes_invalidate_review(self):
        await self.call("write", path="a.txt", content="first")
        snapshot = await self.call("review_changes")
        await self.call("write", path="b.txt", content="second")
        result = await self.call("record_review", decision="keep", reason="stale", evidence_call_ids=[snapshot["evidence_call_id"]])
        self.assertEqual(result["error"], "review_required")
        snapshot = await self.call("review_changes")
        (self.root / "a.txt").write_text("user update")
        result = await self.call("record_review", decision="keep", reason="stale", evidence_call_ids=[snapshot["evidence_call_id"]])
        self.assertEqual(result["error"], "review_stale")

    async def test_paginated_diff_must_be_read_before_keep(self):
        await self.call("write", path="a.txt", content="a" * 1500)
        await self.call("write", path="b.txt", content="b" * 1500)
        snapshot = await self.call("review_changes", limit=500)
        result = await self.call("record_review", decision="keep", reason="only first page", evidence_call_ids=[snapshot["evidence_call_id"]])
        self.assertEqual(result["error"], "review_incomplete")
        while snapshot["next_offset"] is not None:
            snapshot = await self.call("review_changes", offset=snapshot["next_offset"], limit=500)
        result = await self.call("record_review", decision="keep", reason="all diff pages inspected",
                                 evidence_call_ids=[snapshot["evidence_call_id"]])
        self.assertTrue(result["recorded"], result)

    async def test_restored_files_are_not_reported_as_outstanding_changes(self):
        (self.root / "a.txt").write_text("before")
        deleted = await self.call("workspace_delete", path="a.txt")
        self.assertEqual((await self.call("review_changes"))["files"][0]["operation"], "delete")
        await self.call("file_restore", transaction_id=deleted["transaction_id"])
        self.assertTrue(await self.review.ready())
        self.assertIn("已恢复至原状态", self.review.summary())

    async def test_failed_checks_cannot_be_used_as_passed_evidence(self):
        await self.call("write", path="a.txt", content="first")
        action = ToolAction("workspace_run", {"repository": str(self.root)}, "failed-check")
        self.review.observe(Observation(action, {"validation_kind": "unittest", "exit_code": 1, "success": False}), self.registry["workspace_run"])
        await self.call("review_changes")
        result = await self.call("record_review", decision="keep", reason="not valid", evidence_call_ids=["failed-check"])
        self.assertEqual(result["error"], "invalid_review_evidence")
        result = await self.call("record_review", decision="keep", reason="ignore failure",
                                 evidence_call_ids=[self.review.inspection["evidence_call_id"]])
        self.assertEqual(result["error"], "verification_failed")
        self.assertIn("测试存在失败", self.review.summary())

    async def test_task_receipts_are_redacted_durable_and_scoped_to_conversation(self):
        self.log.append({"event": "completed", "result": {"text": "fixture-private-secret"}})
        self.log.finish("cancelled")
        next_log = TurnLog("b" * 32, ContentScanner(), conversation_id="conversation-one")
        self.assertEqual(next_log.previous()[0]["task_id"], self.log.id)
        status = next_log.status(self.log.id)
        self.assertNotIn("fixture-private-secret", json.dumps(status))
        self.assertTrue(status["historical"])
        other = TurnLog("c" * 32, ContentScanner(), conversation_id="conversation-two")
        self.assertEqual(other.status(self.log.id)["error"], "task_not_found")
