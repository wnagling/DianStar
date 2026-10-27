"""Exercise import boundaries in isolated interpreters."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class ArchitectureTests(unittest.TestCase):
    def run_isolated(self, source):
        result = subprocess.run([sys.executable, "-c", source], capture_output=True,
                                text=True, encoding="utf-8", timeout=30,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_owned_git_server_business_logic_does_not_need_mcp_sdk_or_ui(self):
        self.run_isolated('''
import importlib.abc, sys
class BlockTransport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"mcp", "textual"} or fullname in {
            "dianstar.tools.mcp.client", "dianstar.tools.mcp.connection",
            "dianstar.tools.mcp.servers.git.server",
        }:
            raise ImportError("Git business logic must not depend on transport or UI")
sys.meta_path.insert(0, BlockTransport())
from dianstar.tools.mcp.servers.git.operations import GitWorkspace
from dianstar.tools.mcp.servers.git.reader import GitReader
assert issubclass(GitWorkspace, GitReader)
''')

    def test_generic_mcp_connection_does_not_import_product_tools_or_app(self):
        self.run_isolated('''
import importlib.abc, sys
class BlockProduct(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "textual" or fullname.startswith(("textual.", "dianstar.tools.local", "dianstar.tools.mcp.servers", "dianstar.app", "dianstar.ui")):
            raise ImportError("MCP transport must not import product tools or UI")
sys.meta_path.insert(0, BlockProduct())
from dianstar.tools.mcp.connection import StdioConnection
assert StdioConnection(None).tools == []
''')

    def test_mcp_client_does_not_import_owned_server_implementations(self):
        self.run_isolated('''
import importlib.abc, sys
class NoServerImplementation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("dianstar.tools.mcp.servers", "dianstar.tools.local")):
            raise ImportError("MCP client must depend on contracts, not server or built-in implementations")
sys.meta_path.insert(0, NoServerImplementation())
from dianstar.tools.mcp.client import MCPTools
from dianstar.tools.mcp.schemas import GIT_TOOLS
assert "git_status" in GIT_TOOLS
''')

    def test_local_registry_does_not_need_mcp(self):
        self.run_isolated('''
import importlib.abc, sys
class NoMCP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "mcp" or fullname.startswith(("mcp.", "dianstar.tools.mcp")):
            raise ImportError("Built-in tools must not depend on MCP")
sys.meta_path.insert(0, NoMCP())
from pathlib import Path
from types import SimpleNamespace
from dianstar.tools.local.registry import build_local_registry
registry, _ = build_local_registry(None, None, Path("unused.env"), SimpleNamespace(provider="disabled"))
assert {"get_weather", "calculate_date", "calculate_number"} <= registry.keys()
assert not any(name.startswith("mcp_") for name in registry)
''')
