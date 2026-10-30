"""Construct server launches with narrowly scoped process environments."""
import os
from pathlib import Path
import sys
from urllib.request import getproxies
from dotenv import dotenv_values
from mcp import StdioServerParameters
from dianstar.config.paths import PROJECT_ROOT
from dianstar.config.mcp import MCPSettings
from dianstar.tools.mcp.github_contract import READ_TOOLS, WRITE_TOOLS
from dianstar.tools.mcp.credentials import github_token
from dianstar.security.content_guard import FINGERPRINT_ENV, fingerprint_payload, known_credentials
from dianstar.config.github_account import load_account


def server_parameters(settings: MCPSettings, env_file: Path, *, allow_clone=False, confirm_destinations=False) -> dict:
    # SDK adds a small platform runtime environment, not the whole process env.
    common = {name: os.environ[name] for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE") if name in os.environ}
    # The official Go server reads proxy environment variables, while Python
    # also discovers Windows system proxy settings. Pass those settings on.
    proxies = getproxies()
    for scheme, name in (("http", "HTTP_PROXY"), ("https", "HTTPS_PROXY"), ("all", "ALL_PROXY"), ("no", "NO_PROXY")):
        value = proxies.get(scheme)
        if value and name not in common and name.lower() not in common:
            common[name] = value
    parameters = {}
    try:
        account = load_account(env_file.parent)
    except (ValueError, OSError):
        account = {"mode": "signed_out"}
    account_env = ({"DIANSTAR_GITHUB_NAMESPACE": account["namespace"], "DIANSTAR_GITHUB_LOGIN": account["login"]}
                   if account["mode"] == "managed" else
                   {"DIANSTAR_GITHUB_SIGNED_OUT": "1"} if account["mode"] == "signed_out" else {})
    if settings.git_enabled:
        args = ["-m", "dianstar.tools.mcp.servers.git.server"]
        for root in settings.git_repositories:
            args.extend(["--repo", root])
        for root in settings.git_workspace_roots:
            args.extend(["--workspace-root", root])
        if settings.git_write_enabled:
            args.append("--allow-writes")
        if allow_clone:
            args.append("--allow-clone")
        if confirm_destinations and settings.git_write_enabled:
            args.append("--confirm-destinations")
        parameters["git"] = StdioServerParameters(command=sys.executable, args=args,
            cwd=str(PROJECT_ROOT), env={**common, **account_env, "PYTHONIOENCODING": "utf-8",
                FINGERPRINT_ENV: fingerprint_payload(known_credentials(env_file,
                    extra=(github_token(settings, env_file),))),
                **({"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]} if "SSH_AUTH_SOCK" in os.environ else {})})
    if settings.github_enabled:
        executable = Path(settings.github_executable)
        if not executable.is_file() or (os.name == "nt" and executable.suffix.lower() != ".exe"):
            raise ValueError("GitHub MCP 程序不存在或不是可执行文件。")
        token = github_token(settings, env_file)
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
