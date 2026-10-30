"""Real workspace writes and process audits, with no real credentials."""
import json
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.config.mcp import MCPSettings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.workspace import WorkspaceTools
from dianstar.tools.mcp.client import MCPTools


TOKEN = "ghp_" + "C3d4E5f6" * 5


class SecretWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.env = self.base / ".env"
        self.env.write_text("API_KEY=synthetic-application-secret\n")
        self.auth = Authorization()
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.root),), workspace_write_enabled=True)
        self.mcp = SimpleNamespace(settings=settings, authorization=self.auth, git=SimpleNamespace(repositories={str(self.root)}))
        self.tools = WorkspaceTools(self.mcp, self.env)
        self.approve = AsyncMock(return_value=True)
        self.gateway = ToolGateway(self.auth, self.tools.registrations(), self.approve)

    async def call(self, name="workspace_edit", **args):
        return (await self.gateway.execute(ToolAction(name, {"repository": str(self.root), **args}))).result

    async def test_importing_token_as_ordinary_file_is_blocked_before_confirmation_or_write(self):
        result = await self.call(path="src/downloaded.py", mode="create", new_text=TOKEN)
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertFalse((self.root / "src").exists())
        self.approve.assert_not_awaited()
        self.assertNotIn(TOKEN, json.dumps(result))

    async def test_explicit_local_config_can_contain_token_with_masked_preview(self):
        result = await self.call(path="config.json", mode="create", new_text=json.dumps({"token": TOKEN}), local_only=True)
        self.assertTrue(result["success"])
        self.assertEqual(json.loads((self.root / "config.json").read_text())["token"], TOKEN)
        request = self.approve.await_args.args[0]
        self.assertIn("仅在本机保存", request["summary"])
        self.assertNotIn(TOKEN, json.dumps(request))

    async def test_local_environment_write_requires_explicit_intent_and_confirmation(self):
        result = await self.call(path=".env", mode="create", new_text="API_KEY=" + TOKEN)
        self.assertIn("error", result)
        self.assertFalse((self.root / ".env").exists())
        result = await self.call(path=".env", mode="create", new_text="API_KEY=" + TOKEN, local_only=True)
        self.assertTrue(result["success"])
        self.assertNotIn(TOKEN, json.dumps(self.approve.await_args.args[0]))

    async def test_application_key_is_never_copied_even_with_local_only_or_no_prompt_policy(self):
        self.gateway = ToolGateway(self.auth, self.tools.registrations(), self.approve,
            policies={"workspace_edit": ToolPolicy(requires_approval=False)})
        result = await self.call(path="config.json", mode="create", new_text="synthetic-application-secret", local_only=True)
        self.assertEqual(result["error"], "sensitive_content_denied")
        self.assertFalse((self.root / "config.json").exists())
        self.approve.assert_not_awaited()

    async def test_application_own_environment_cannot_be_overwritten(self):
        tools = WorkspaceTools(self.mcp, self.root / ".env")
        self.gateway = ToolGateway(self.auth, tools.registrations(), self.approve)
        result = await self.call(path=".env", mode="create", new_text="API_KEY=your-key-here", local_only=True)
        self.assertIn("error", result)
        self.assertFalse((self.root / ".env").exists())

    async def test_local_only_cannot_write_internal_settings_or_credential_directories(self):
        for path in (".local/mcp.json", ".local/config.json", ".git/config.json", ".aws/config.json", "nested/.ssh/settings.json"):
            result = await self.call(path=path, mode="create", new_text="{}", local_only=True)
            self.assertIn("error", result)
            self.assertFalse((self.root / path).exists())
        self.approve.assert_not_awaited()

    async def test_placeholder_template_and_normal_config_are_allowed(self):
        for path, text in ((".env.example", "API_KEY=your-key-here\n"), ("config.json", '{"port": 8000}')):
            self.assertTrue((await self.call(path=path, mode="create", new_text=text))["success"])

    async def test_removing_existing_secret_masks_old_diff(self):
        (self.root / "config.json").write_text(json.dumps({"token": TOKEN}))
        result = await self.call(path="config.json", mode="overwrite", new_text='{"token": "${SERVICE_TOKEN}"}')
        self.assertTrue(result["success"])
        self.assertNotIn(TOKEN, json.dumps(self.approve.await_args.args[0]))

    async def test_command_generated_secret_is_reported_without_deleting_it(self):
        script = "from pathlib import Path;Path('.env').write_text('API_KEY=' + 'ghp_' + 'D4e5F6g7' * 5)"
        result = await self.call("workspace_run", argv=[sys.executable, "-c", script])
        self.assertTrue(result["success"])
        self.assertTrue(result["security_review"]["findings"])
        self.assertTrue((self.root / ".env").exists())
        self.assertNotIn("ghp_" + "D4e5F6g7" * 5, json.dumps(result))

    async def test_command_output_secrets_are_masked(self):
        result = await self.call("workspace_run", argv=[sys.executable, "-c", "print('ghp_' + 'E5f6G7h8' * 5)"])
        self.assertTrue(result["success"])
        self.assertNotIn("ghp_" + "E5f6G7h8" * 5, result["content"])

    async def test_cancelling_local_config_has_no_file_side_effect(self):
        self.approve.return_value = False
        result = await self.call(path="config.json", mode="create", new_text=json.dumps({"token": TOKEN}), local_only=True)
        self.assertEqual(result["error"], "authorization_required")
        self.assertFalse((self.root / "config.json").exists())

    async def test_real_stdio_git_rejects_known_key_using_only_fingerprints(self):
        subprocess.run(["git", "init", "--initial-branch=main"], cwd=self.root, check=True, capture_output=True)
        (self.root / "ordinary.txt").write_text("synthetic-application-secret")
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.root),), git_write_enabled=True)
        async with MCPTools(settings, self.env, self.auth) as mcp:
            gateway = ToolGateway(self.auth, mcp.registry, self.approve)
            result = await gateway.execute(ToolAction("mcp_git__git_stage", {"repository": str(self.root), "files": ["ordinary.txt"]}))
        self.assertEqual(result.result["error"], "sensitive_content_denied")
        self.assertTrue(result.result["findings"])
        self.approve.assert_not_awaited()
        self.assertNotIn("synthetic-application-secret", json.dumps(result.result))
