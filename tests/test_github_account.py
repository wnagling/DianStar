"""User-driven account lifecycle with synthetic credentials and mocked GitHub."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from dianstar.app.github_account import GitHubAccount
from dianstar.config.github_account import load_account, save_account, cached_token
from dianstar.config.mcp import MCPSettings, save_mcp_settings
from dianstar.platform.github_credentials import CredentialManager, GitHubLoginError, credential_environment
from dianstar.tools.mcp.credentials import github_token
from dianstar.tools.mcp.launch import server_parameters
from dianstar.tools.mcp.servers.git.operations import GitWorkspace


VALUE = "fixture-github-account-secret"
SPACE = "dianstar-" + "1" * 32


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = self.root / ".env"
        self.manager = AsyncMock(spec=CredentialManager)
        self.manager.read.return_value = ("fixture-user", VALUE)
        self.requests = []
        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json={"login": "fixture-user", "id": 123})
        self.service = GitHubAccount(self.env, manager=self.manager, transport=httpx.MockTransport(respond))
        self.addCleanup(cached_token.cache_clear)

    async def test_token_is_verified_then_saved_only_in_vault_and_reused_after_restart(self):
        policy = self.root / ".local/mcp.json"
        save_mcp_settings(MCPSettings(git_write_enabled=False), policy)
        before = policy.read_bytes()
        status = await self.service.login(VALUE)
        self.assertEqual(status.state, "connected")
        account = load_account(self.root)
        self.assertEqual(account["login"], "fixture-user")
        self.manager.store.assert_awaited_once_with(account["namespace"], "fixture-user", VALUE)
        self.assertEqual(self.requests[0].url, "https://api.github.com/user")
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer " + VALUE)
        self.assertEqual(policy.read_bytes(), before)
        self.assertFalse(self.env.exists())
        self.assertNotIn(VALUE, (self.root / ".local/github_account.json").read_text())
        with patch("dianstar.config.github_account.read_stored_token", return_value=VALUE):
            self.assertEqual(github_token(MCPSettings(), self.env), VALUE)
            restarted = GitHubAccount(self.env, manager=self.manager, transport=self.service.transport)
            self.assertEqual((await restarted.check()).login, "fixture-user")

    async def test_browser_login_uses_new_namespace_and_checks_returned_account(self):
        status = await self.service.login()
        self.assertEqual(status.state, "connected")
        namespace = load_account(self.root)["namespace"]
        self.manager.browser_login.assert_awaited_once_with(namespace)
        self.manager.store.assert_not_awaited()

    async def test_invalid_token_does_not_replace_existing_account_or_save_secret(self):
        save_account(self.root, {"mode": "managed", "login": "old-user", "namespace": SPACE})
        before = (self.root / ".local/github_account.json").read_bytes()
        self.service.transport = httpx.MockTransport(lambda _: httpx.Response(401, json={"message": "bad credential"}))
        with self.assertRaises(GitHubLoginError):
            await self.service.login(VALUE)
        self.manager.store.assert_not_awaited()
        self.assertEqual((self.root / ".local/github_account.json").read_bytes(), before)

    async def test_browser_identity_mismatch_never_activates_account(self):
        self.manager.read.return_value = ("different-user", VALUE)
        with self.assertRaises(GitHubLoginError):
            await self.service.login()
        self.assertEqual(load_account(self.root)["mode"], "legacy")

    async def test_cancelled_browser_login_does_not_activate_credentials(self):
        started = asyncio.Event()
        async def wait(_):
            started.set()
            await asyncio.Event().wait()
        self.manager.browser_login.side_effect = wait
        task = asyncio.create_task(self.service.login())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(load_account(self.root)["mode"], "legacy")

    async def test_logout_blocks_all_fallbacks_and_deletes_only_app_namespace(self):
        save_account(self.root, {"mode": "managed", "login": "fixture-user", "namespace": SPACE})
        self.env.write_text("GITHUB_PERSONAL_ACCESS_TOKEN=fixture-external-token\n")
        status = await self.service.logout()
        self.assertEqual(status.state, "signed_out")
        self.manager.erase.assert_awaited_once_with(SPACE, "fixture-user")
        with patch("dianstar.tools.mcp.credentials.git_credential", return_value="fixture-global-token") as external:
            self.assertIsNone(github_token(MCPSettings(github_use_git_credentials=True), self.env))
            external.assert_not_called()
        self.assertIn("fixture-external-token", self.env.read_text())

    async def test_failed_vault_cleanup_keeps_logout_effective_and_can_be_retried(self):
        save_account(self.root, {"mode": "managed", "login": "fixture-user", "namespace": SPACE})
        self.manager.erase.side_effect = GitHubLoginError("unavailable")
        self.assertEqual((await self.service.logout()).state, "signed_out")
        self.assertIsNone(github_token(MCPSettings(), self.env))
        self.assertEqual(load_account(self.root)["namespace"], SPACE)
        self.manager.erase.side_effect = None
        await self.service.logout()
        self.assertEqual(load_account(self.root), {"mode": "signed_out"})

    async def test_malformed_and_redirected_identity_responses_are_not_trusted(self):
        for response in (httpx.Response(302, headers={"Location": "https://example.com"}),
                         httpx.Response(200, json=[]), httpx.Response(200, json={"login": "bad\nname", "id": 1})):
            self.service.transport = httpx.MockTransport(lambda _, response=response: response)
            with self.assertRaises(GitHubLoginError):
                await self.service.login(VALUE)
        self.manager.store.assert_not_awaited()

    async def test_new_login_activates_new_namespace_before_cleaning_old_session(self):
        save_account(self.root, {"mode": "managed", "login": "old-user", "namespace": SPACE})
        await self.service.login(VALUE)
        self.assertNotEqual(load_account(self.root)["namespace"], SPACE)
        self.manager.erase.assert_awaited_once_with(SPACE, "old-user")

    async def test_failed_store_does_not_activate_account_or_change_policy(self):
        self.manager.store.side_effect = GitHubLoginError("store unavailable")
        with self.assertRaises(GitHubLoginError):
            await self.service.login(VALUE)
        self.assertEqual(load_account(self.root)["mode"], "legacy")

    async def test_managed_login_reports_unreadable_vault_without_using_external_credentials(self):
        save_account(self.root, {"mode": "managed", "login": "fixture-user", "namespace": SPACE})
        with patch("dianstar.config.github_account.read_stored_token", return_value=None):
            status = await self.service.check()
        self.assertEqual(status.state, "unavailable")
        self.assertEqual(status.login, "fixture-user")

    async def test_helper_timeout_terminates_its_owned_process(self):
        import sys
        manager = CredentialManager(executable=sys.executable)
        with self.assertRaises(GitHubLoginError):
            await manager.call(SPACE, ["-c", "import time; time.sleep(60)"], timeout=0.05)


class CredentialBoundaryTests(unittest.TestCase):
    def test_missing_credentials_point_to_the_account_entry(self):
        from dianstar.app.mcp_startup import resolve_connections
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("dianstar.app.mcp_startup.github_token", return_value=None):
                _, status = resolve_connections(MCPSettings(git_enabled=False, github_executable=str(root / "server.exe"),
                    github_repositories=("owner/project",)), root / ".env", root)
            self.assertIn("/github", status["github"]["message"])

    def test_metadata_writer_rejects_secret_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                save_account(root, {"mode": "managed", "login": "fixture-user", "namespace": SPACE, "token": VALUE})
            self.assertFalse((root / ".local/github_account.json").exists())

    def test_helper_environment_uses_secure_storage_without_inheriting_keys_or_trace(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": VALUE, "GCM_TRACE_SECRETS": "1", "GCM_CREDENTIAL_STORE": "plaintext"}):
            env = credential_environment()
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertEqual(env["GCM_TRACE_SECRETS"], "0")
        self.assertNotEqual(env["GCM_CREDENTIAL_STORE"], "plaintext")
        self.assertNotIn(VALUE, json.dumps(env))

    def test_corrupt_account_state_does_not_fall_back_to_external_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".local").mkdir()
            (root / ".local/github_account.json").write_text('{"mode":"managed","token":"fixture-value"}')
            (root / ".env").write_text("GITHUB_PERSONAL_ACCESS_TOKEN=" + VALUE)
            self.assertIsNone(github_token(MCPSettings(), root / ".env"))

    def test_git_account_metadata_contains_no_token_and_is_scoped_to_github_https(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            save_account(root, {"mode": "managed", "login": "fixture-user", "namespace": SPACE})
            with patch("dianstar.tools.mcp.launch.github_token", return_value=VALUE):
                parameters = server_parameters(MCPSettings(git_enabled=True), root / ".env")["git"]
            self.assertNotIn(VALUE, json.dumps(parameters.env))
            self.assertEqual(parameters.env["DIANSTAR_GITHUB_NAMESPACE"], SPACE)
            workspace = GitWorkspace([])
            with patch.dict(os.environ, {"DIANSTAR_GITHUB_NAMESPACE": SPACE, "DIANSTAR_GITHUB_LOGIN": "fixture-user"}):
                context = workspace._network_context((root, {}, ["git"]), "https://github.com/owner/project.git")
                other = workspace._network_context((root, {}, ["git"]), "https://example.com/project.git")
            self.assertEqual(context[1]["GCM_NAMESPACE"], SPACE)
            self.assertNotIn("GCM_NAMESPACE", other[1])
