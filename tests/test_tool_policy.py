"""Backend availability and approval policies do not change preview or scope checks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.runtime import run_turn
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation, ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway, ToolPreparationError
from dianstar.security.tool_policy import ToolPolicy
from tests.test_mcp_startup import git
from tests.test_permissions import call, reply


def registration(name="change", capability="fixture.write"):
    schema = {"type": "function", "function": {"name": name, "parameters": {"type": "object",
        "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}}
    prepare = AsyncMock(return_value="Exact preview")
    handler = AsyncMock(side_effect=lambda action: Observation(action, {"success": True}))
    return ToolRegistration(schema, capability, handler, prepare)


class ToolPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_policy_requires_confirmation(self):
        entry = registration()
        approve = AsyncMock(return_value=True)
        gateway = ToolGateway(Authorization(), {"change": entry}, approve)
        result = await gateway.execute(ToolAction("change", {"value": "fixture"}))
        self.assertTrue(result.result["success"])
        entry.approval_description.assert_awaited_once()
        approve.assert_awaited_once()
        entry.handler.assert_awaited_once()

    async def test_disabled_rule_hides_tool_and_overrides_existing_grant(self):
        entry = registration()
        auth = Authorization()
        auth.grant("fixture.write", name="change", arguments={"value": "fixture"})
        approve = AsyncMock(return_value=True)
        gateway = ToolGateway(auth, {"change": entry}, approve, policies={"fixture.write": ToolPolicy(enabled=False)})
        self.assertEqual(gateway.available_schemas(), [])
        result = await gateway.execute(ToolAction("change", {"value": "fixture"}))
        self.assertEqual(result.result["error"], "tool_disabled")
        approve.assert_not_awaited()
        entry.approval_description.assert_not_awaited()
        entry.handler.assert_not_awaited()

    async def test_waived_prompt_still_prepares_and_uses_exact_single_operation_grant(self):
        entry = registration()
        auth = Authorization()
        approve = AsyncMock(return_value=False)
        gateway = ToolGateway(auth, {"change": entry}, approve, policies={"change": ToolPolicy(requires_approval=False)})
        for value in ("first", "second"):
            result = await gateway.execute(ToolAction("change", {"value": value}))
            self.assertTrue(result.result["success"])
            self.assertFalse(auth.has_grant("fixture.write"))
        self.assertEqual(entry.approval_description.await_count, 2)
        self.assertEqual(entry.handler.await_count, 2)
        approve.assert_not_awaited()

    async def test_no_prompt_policy_still_rejects_invalid_arguments_and_failed_preflight(self):
        entry = registration()
        gateway = ToolGateway(Authorization(), {"change": entry}, policies={"change": ToolPolicy(requires_approval=False)})
        result = await gateway.execute(ToolAction("change", {"value": 42}))
        self.assertEqual(result.result["error"], "invalid_tool_arguments")
        entry.approval_description.assert_not_awaited()
        entry.approval_description.side_effect = ToolPreparationError("scope_denied", "Outside selected project")
        result = await gateway.execute(ToolAction("change", {"value": "fixture"}))
        self.assertEqual(result.result["error"], "scope_denied")
        entry.handler.assert_not_awaited()

    async def test_no_prompt_policy_does_not_override_revocation_or_closed_request(self):
        for closed in (False, True):
            entry = registration()
            auth = Authorization()
            if closed:
                auth.close()
            else:
                auth.deny("fixture.write")
            gateway = ToolGateway(auth, {"change": entry}, policies={"change": ToolPolicy(requires_approval=False)})
            result = await gateway.execute(ToolAction("change", {"value": "fixture"}))
            self.assertIn("error", result.result)
            entry.approval_description.assert_not_awaited()
            entry.handler.assert_not_awaited()

    async def test_policy_snapshot_is_backend_owned_and_tool_rule_is_more_specific(self):
        entry = registration()
        rules = {"fixture.write": ToolPolicy(enabled=False), "change": ToolPolicy(requires_approval=False)}
        gateway = ToolGateway(Authorization(), {"change": entry}, policies=rules)
        rules["change"] = ToolPolicy(enabled=False)
        result = await gateway.execute(ToolAction("change", {"value": "fixture"}))
        self.assertTrue(result.result["success"])
        with self.assertRaises(TypeError):
            gateway.policies["change"] = ToolPolicy()
        with self.assertRaises(ValueError):
            ToolPolicy(enabled="yes")


class RuntimeToolPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_policy_survives_model_requested_reconnection(self):
        with tempfile.TemporaryDirectory() as tmp, patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            root = Path(tmp).resolve()
            git(root, "init")
            (root / "README.md").write_text("before\n")
            env = root / ".env"
            env.write_text("DEEPSEEK_API_KEY=fixture-model-key\n")
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("configure_mcp", {"changes": {"git_enabled": None, "workspace_write_enabled": True}}),
                call("workspace_read", {"repository": str(root), "path": "README.md"}), call("mcp_status", {}), reply("已读取。")])
            history = []
            approval = AsyncMock(return_value=True)
            await run_turn("连接并读取当前项目", env_file=env, project_directory=root, history=history, on_permission=approval,
                           tool_policies={"workspace.write": ToolPolicy(enabled=False)}, allow_backend_configuration=True)
            names = {s["function"]["name"] for s in create.await_args_list[1].kwargs["tools"]}
            self.assertNotIn("workspace_edit", names)
            self.assertNotIn("workspace_run", names)
            self.assertIn("workspace_read", names)
            results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
            self.assertTrue(results[0]["reconnected"])
            self.assertIn("before", results[1]["content"])
            self.assertFalse(results[2]["tool_policies"]["workspace_edit"]["enabled"])
            self.assertFalse(results[2]["workspace_writes_enabled"])
            approval.assert_awaited_once()

    async def test_readonly_backend_policy_hides_and_blocks_writes(self):
        with tempfile.TemporaryDirectory() as tmp, patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            root = Path(tmp).resolve()
            git(root, "init")
            (root / "README.md").write_text("before\n")
            env = root / ".env"
            env.write_text("DEEPSEEK_API_KEY=fixture-model-key\n")
            create = factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("workspace_edit", {"repository": str(root), "path": "README.md", "mode": "overwrite", "new_text": "after"}), reply("未执行。")])
            approval = AsyncMock(return_value=True)
            history = []
            await run_turn("读取项目", env_file=env, project_directory=root, history=history, on_permission=approval,
                           tool_policies={"workspace.write": ToolPolicy(enabled=False)})
            names = {s["function"]["name"] for s in create.await_args_list[0].kwargs["tools"]}
            self.assertIn("workspace_read", names)
            self.assertNotIn("workspace_edit", names)
            self.assertEqual((root / "README.md").read_text(), "before\n")
            approval.assert_not_awaited()

    async def test_backend_prompt_waiver_preserves_actual_workspace_preflight(self):
        with tempfile.TemporaryDirectory() as tmp, patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            root = Path(tmp).resolve()
            git(root, "init")
            (root / "README.md").write_text("before\n")
            env = root / ".env"
            env.write_text("DEEPSEEK_API_KEY=fixture-model-key\n")
            factory.return_value.chat.completions.create = AsyncMock(side_effect=[
                call("workspace_edit", {"repository": str(root), "path": "README.md", "mode": "replace", "old_text": "before", "new_text": "after"}),
                call("mcp_status", {}), reply("已修改。")])
            history = []
            await run_turn("修改项目", env_file=env, project_directory=root, history=history,
                           tool_policies={"workspace.write": ToolPolicy(requires_approval=False)})
            self.assertEqual((root / "README.md").read_text(), "after\n")
            result = next(json.loads(m["content"]) for m in history if m["role"] == "tool")
            self.assertTrue(result["success"])
            status = [json.loads(m["content"]) for m in history if m["role"] == "tool"][-1]
            self.assertFalse(status["tool_policies"]["workspace_edit"]["requires_approval"])
