import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
from textual.widgets import Checkbox, Input, Static, TextArea

from dianstar.sessions.conversations import Conversation
from dianstar.config.mcp import load_mcp_settings
from dianstar.ui.tui.screens.mcp import MCPConfigScreen
from dianstar.ui.tui.app import DianStarApp


class MCPUITests(unittest.IsolatedAsyncioTestCase):
    async def test_config_entry_save_masking_layout_and_no_implicit_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            app = DianStarApp(Conversation.create(root / "conversations"),
                config_file=root / ".local/config.json", env_file=root / ".env")
            async with app.run_test(size=(80, 24)) as pilot:
                await app._command("/config")
                await pilot.pause()
                config = app.screen
                await pilot.press("down", "down", "down", "down", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, MCPConfigScreen)
                screen = app.screen
                panel = screen.query_one("#mcp-panel")
                self.assertLessEqual(panel.region.right, 80)
                self.assertLessEqual(panel.region.bottom, 24)
                self.assertTrue(screen.query_one("#mcp-github-token", Input).password)
                screen.query_one("#mcp-git-enabled", Checkbox).value = True
                screen.query_one("#mcp-git-write-enabled", Checkbox).value = True
                screen.query_one("#mcp-git-repos", TextArea).text = str(root)
                screen.query_one("#mcp-github-repos", TextArea).text = "Owner/Repo\nOwner/ReadOnly"
                screen.query_one("#mcp-github-private-repos", TextArea).text = "Owner/Repo"
                screen.query_one("#mcp-github-write-repos", TextArea).text = "Owner/Repo"
                screen.query_one("#mcp-github-token", Input).value = "fixture-github-token"
                with patch("dianstar.ui.tui.screens.mcp.MCPTools") as tools:
                    button = screen.query_one("#mcp-save")
                    button.scroll_visible(animate=False)
                    await pilot.pause()
                    await pilot.click(button)
                    await pilot.pause()
                    tools.assert_not_called()
                settings = load_mcp_settings(root / ".local/mcp.json")
                self.assertTrue(settings.git_enabled)
                self.assertTrue(settings.git_write_enabled)
                self.assertEqual(settings.git_repositories, (str(root),))
                self.assertEqual(settings.github_repositories, ("owner/repo", "owner/readonly"))
                self.assertEqual(settings.github_private_repositories, ("owner/repo",))
                self.assertEqual(settings.github_write_repositories, ("owner/repo",))
                for identifier in ("mcp-save", "mcp-test", "mcp-back"):
                    region = screen.query_one("#" + identifier).region
                    self.assertGreater(region.width, 0)
                    self.assertLessEqual(region.right, panel.region.right)
                    self.assertLessEqual(region.bottom, panel.region.bottom)
                self.assertEqual(dotenv_values(root / ".env")["GITHUB_PERSONAL_ACCESS_TOKEN"], "fixture-github-token")
                self.assertEqual(screen.query_one("#mcp-github-token", Input).value, "")
                self.assertNotIn("fixture-github-token", (root / ".local/mcp.json").read_text(encoding="utf-8"))
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, config)
                self.assertTrue(config.changed)

    async def test_invalid_scope_does_not_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = DianStarApp(Conversation.create(root / "conversations"),
                config_file=root / ".local/config.json", env_file=root / ".env")
            async with app.run_test(size=(100, 40)) as pilot:
                app.push_screen(MCPConfigScreen(root / ".local/mcp.json", root / ".env"))
                await pilot.pause()
                screen = app.screen
                screen.query_one("#mcp-git-enabled", Checkbox).value = True
                screen.query_one("#mcp-git-repos", TextArea).text = "../outside"
                await pilot.click("#mcp-save")
                await pilot.pause()
                self.assertFalse((root / ".local/mcp.json").exists())
                self.assertIn("保存失败", str(screen.query_one("#mcp-state", Static).render()))
