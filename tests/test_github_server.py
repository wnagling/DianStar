import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dianstar.tools.mcp.github_server import MANAGED_PATH, discover_server
from dianstar.app.mcp_startup import resolve_connections
from dianstar.config.mcp import MCPSettings
from tests.test_mcp_startup import git


class GitHubServerDiscoveryTests(unittest.TestCase):
    def test_project_directory_and_relative_path_entries_are_not_executable_sources(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PATH": os.pathsep.join((".", "", str(Path.cwd())))}), patch("dianstar.tools.mcp.github_server.shutil.which") as lookup:
            self.assertEqual(discover_server("", Path(tmp)), "")
            lookup.assert_not_called()
    def test_configured_override_is_preserved(self):
        with patch("dianstar.tools.mcp.github_server.shutil.which") as lookup:
            self.assertEqual(discover_server("C:/trusted/server.exe", Path("unused")), "C:/trusted/server.exe")
            lookup.assert_not_called()

    def test_verified_managed_component_is_found_without_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            component = root / MANAGED_PATH
            component.parent.mkdir(parents=True)
            component.write_bytes(b"trusted fixture")
            with patch("dianstar.tools.mcp.github_server.WINDOWS_SHA256", hashlib.sha256(b"trusted fixture").hexdigest()), patch("dianstar.tools.mcp.github_server.shutil.which") as lookup:
                self.assertEqual(discover_server("", root), str(component))
                lookup.assert_not_called()

    def test_changed_component_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            component = root / MANAGED_PATH
            component.parent.mkdir(parents=True)
            component.write_bytes(b"changed fixture")
            with patch("dianstar.tools.mcp.github_server.shutil.which", return_value="C:/installed/github-mcp-server.exe"):
                self.assertEqual(discover_server("", root), "C:/installed/github-mcp-server.exe")

    def test_missing_component_does_not_download_or_write_configuration(self):
        with tempfile.TemporaryDirectory() as tmp, patch("dianstar.tools.mcp.github_server.shutil.which", return_value=None):
            root = Path(tmp)
            self.assertEqual(discover_server("", root), "")
            self.assertEqual(list(root.iterdir()), [])

    def test_startup_connects_with_managed_component_and_existing_token_without_mcp_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            component = root / MANAGED_PATH
            component.parent.mkdir(parents=True)
            component.write_bytes(b"trusted fixture")
            env = root / ".env"
            env.write_text("GITHUB_PERSONAL_ACCESS_TOKEN=fixture-token\n")
            git(root, "init")
            git(root, "remote", "add", "origin", "https://github.com/owner/repo.git")
            raw = MCPSettings()
            with patch("dianstar.tools.mcp.github_server.WINDOWS_SHA256", hashlib.sha256(b"trusted fixture").hexdigest()):
                effective, status = resolve_connections(raw, env, root)
            self.assertTrue(effective.github_enabled)
            self.assertEqual(effective.github_executable, str(component))
            self.assertEqual(effective.github_repositories, ("owner/repo",))
            self.assertNotIn("github", status)
            self.assertEqual(raw.github_executable, "")
            self.assertFalse((root / ".local/mcp.json").exists())
