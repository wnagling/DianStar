"""Account UI acceptance: masking, cancellation, layout and no model/history use."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, Input, Static, ListView

from dianstar.app.github_account import AccountStatus
from dianstar.sessions.conversations import Conversation
from dianstar.ui.tui.app import DianStarApp
from dianstar.ui.tui.screens.github_account import GitHubAccountScreen
from dianstar.ui import cli


VALUE = "fixture-github-ui-secret"


class GitHubAccountUITests(unittest.IsolatedAsyncioTestCase):
    async def test_masked_token_entry_saves_without_chat_history_or_model_requests(self):
        for size in ((80, 24), (100, 32)):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
                service = AsyncMock()
                service.check.return_value = AccountStatus("signed_out", message="尚未登录 GitHub。")
                service.login.return_value = AccountStatus("connected", "fixture-user", "登录成功。")
                async with app.run_test(size=size) as pilot:
                    screen = GitHubAccountScreen(service=service)
                    app.push_screen(screen)
                    await pilot.pause()
                    await pilot.click("#github-account-token-mode")
                    await pilot.pause()
                    field = screen.query_one("#github-account-token", Input)
                    field.value = VALUE
                    self.assertTrue(field.password)
                    self.assertNotIn(VALUE, str(field.render()))
                    for button in screen.query(Button):
                        if button.display:
                            self.assertGreaterEqual(button.region.y, 0)
                            self.assertLessEqual(button.region.bottom, size[1])
                            self.assertLessEqual(button.region.right, size[0])
                    await pilot.click("#github-account-save")
                    await pilot.pause()
                    service.login.assert_awaited_once_with(VALUE)
                    self.assertEqual(field.value, "")
                    self.assertIn("fixture-user", str(screen.query_one("#github-account-status", Static).render()))
                    self.assertEqual(app.conversation.messages, [])
                    await pilot.press("escape")
                    self.assertEqual(field.value, "")

    async def test_browser_wait_is_cancelled_when_account_screen_closes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
            started, cancelled = asyncio.Event(), asyncio.Event()
            service = AsyncMock()
            service.check.return_value = AccountStatus("signed_out", message="尚未登录。")
            async def waiting():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            service.login.side_effect = waiting
            async with app.run_test(size=(80, 24)) as pilot:
                app.push_screen(GitHubAccountScreen(service=service))
                await pilot.pause()
                await pilot.click("#github-account-browser")
                await asyncio.wait_for(started.wait(), 2)
                await pilot.press("escape")
                await asyncio.wait_for(cancelled.wait(), 2)
                self.assertEqual(app.conversation.messages, [])

    async def test_config_account_entry_and_slash_command_do_not_expose_mcp_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
            with patch("dianstar.ui.tui.screens.github_account.GitHubAccount") as factory:
                factory.return_value.check = AsyncMock(return_value=AccountStatus("signed_out", message="尚未登录。"))
                async with app.run_test() as pilot:
                    await app._command("/config")
                    await pilot.pause()
                    menu = app.screen.query_one("#config-menu", ListView)
                    menu.index = 4
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, GitHubAccountScreen)
                    self.assertFalse(app.screen.query("#menu-mcp"))
                    await pilot.press("escape", "escape")
                    await app._command("/github")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, GitHubAccountScreen)
                    self.assertEqual(app.conversation.messages, [])

    async def test_token_command_arguments_are_rejected_without_sending_to_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
            async with app.run_test() as pilot:
                await app._command("/github token " + VALUE)
                self.assertEqual(app.conversation.messages, [])
                self.assertNotIn(VALUE, str(app.query_one("#status", Static).render()))


class GitHubAccountCLITests(unittest.TestCase):
    def test_token_input_never_falls_back_to_echoing_the_credential(self):
        from getpass import GetPassWarning
        from dianstar.ui.github_account_cli import run_account_command
        with patch("dianstar.ui.github_account_cli.getpass", side_effect=GetPassWarning()), \
             patch("dianstar.ui.github_account_cli.GitHubAccount") as service, patch("builtins.print"):
            self.assertEqual(run_account_command("token"), 1)
            service.return_value.login.assert_not_called()

    def test_account_flag_runs_without_creating_conversation_or_model(self):
        with patch("sys.argv", ["dianstar", "--github", "status"]), \
             patch("dianstar.ui.github_account_cli.run_account_command", return_value=0) as operation, \
             patch("dianstar.ui.cli.Conversation.create") as create:
            self.assertEqual(cli.main(), 0)
        operation.assert_called_once_with("status")
        create.assert_not_called()
