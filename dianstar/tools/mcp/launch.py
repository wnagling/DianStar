"""Construct server launches with narrowly scoped process environments."""
import os
from pathlib import Path
import sys
from dotenv import dotenv_values
from mcp import StdioServerParameters
from dianstar.config.paths import PROJECT_ROOT
from dianstar.config.mcp import MCPSettings
from dianstar.tools.mcp.github_contract import READ_TOOLS, WRITE_TOOLS


def server_parameters(settings: MCPSettings, env_file: Path) -> dict:
    # SDK adds a small platform runtime environment, not the whole process env.
    common = {name: os.environ[name] for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE") if name in os.environ}
    parameters = {}
    if settings.git_enabled:
        args = ["-m", "dianstar.tools.mcp.servers.git.server"]
        for root in settings.git_repositories:
            args.extend(["--repo", root])
        if settings.git_write_enabled:
            args.append("--allow-writes")
        parameters["git"] = StdioServerParameters(command=sys.executable, args=args,
            cwd=str(PROJECT_ROOT), env={**common, "PYTHONIOENCODING": "utf-8",
                **({"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]} if "SSH_AUTH_SOCK" in os.environ else {})})
    if settings.github_enabled:
        executable = Path(settings.github_executable)
        if not executable.is_file() or (os.name == "nt" and executable.suffix.lower() != ".exe"):
            raise ValueError("GitHub MCP 程序不存在或不是可执行文件。")
        values = dotenv_values(env_file) if env_file.is_file() else {}
        token = values.get("GITHUB_PERSONAL_ACCESS_TOKEN") or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
        writable = bool(settings.github_write_repositories)
        selected = ",".join(sorted(READ_TOOLS | (WRITE_TOOLS if writable else set())))
        env = {**common, "GITHUB_READ_ONLY": "0" if writable else "1", "GITHUB_TOOLSETS": "",
               "GITHUB_TOOLS": selected}
        if token:
            env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
        parameters["github"] = StdioServerParameters(command=str(executable),
            args=["stdio", *([] if writable else ["--read-only"]), "--tools", selected], env=env,
            cwd=str(env_file.parent))
    return parameters
