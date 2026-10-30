"""Tools are internal capabilities, without an MCP settings menu or screen."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from textual.widgets import Input, ListView
from dianstar.config.mcp import MCPSettings, save_mcp_settings
from dianstar.sessions.conversations import Conversation
from dianstar.ui.tui.app import DianStarApp


class MCPUITests(unittest.IsolatedAsyncioTestCase):
    async def test_config_has_only_user_settings_and_no_mcp_entry_or_screen(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
            async with app.run_test(size=(80, 24)) as pilot:
                await app._command("/config")
                await pilot.pause()
                menu = app.screen.query_one("#config-menu", ListView)
                self.assertEqual([child.id for child in menu.children], ["menu-key", "menu-models", "menu-default", "menu-shortcuts", "menu-github"])
                self.assertFalse(app.screen.query("#menu-mcp"))
                self.assertLessEqual(menu.region.right, 80)
                self.assertLessEqual(menu.region.bottom, 24)
                await pilot.press("escape")
            self.assertIsNone(importlib.util.find_spec("dianstar.ui.tui.screens.mcp"))

    async def test_user_settings_save_does_not_rewrite_backend_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".local/mcp.json"
            save_mcp_settings(MCPSettings(git_write_enabled=False, workspace_write_enabled=False), path)
            before = path.read_bytes()
            app = DianStarApp(Conversation.create(root / "conversations"), config_file=root / ".local/config.json", env_file=root / ".env")
            async with app.run_test(size=(80, 24)) as pilot:
                await app._command("/config")
                await pilot.pause()
                await pilot.press("down", "down", "down", "enter")
                await pilot.pause()
                app.screen.query_one("#shortcut-model-input", Input).value = "ctrl+b"
                await app.screen._save_shortcut()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(app.screen.query("#menu-mcp"))
