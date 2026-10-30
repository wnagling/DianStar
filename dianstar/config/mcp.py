"""User-owned MCP configuration; no commands or grants from model messages."""

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from dianstar.config.paths import CONFIG_FILE

MCP_CONFIG_FILE = CONFIG_FILE.with_name("mcp.json")


@dataclass(frozen=True)
class MCPSettings:
    git_enabled: bool | None = None
    git_repositories: tuple[str, ...] = ()
    github_enabled: bool | None = None
    github_executable: str = ""
    github_repositories: tuple[str, ...] = ()
    git_write_enabled: bool | None = None
    github_private_repositories: tuple[str, ...] = ()
    github_write_repositories: tuple[str, ...] | None = None
    git_workspace_roots: tuple[str, ...] = ()
    github_admin_repositories: tuple[str, ...] | None = None
    github_use_git_credentials: bool = False
    workspace_write_enabled: bool | None = None


def normalize(data: dict) -> MCPSettings:
    if not isinstance(data, dict) or set(data) - set(MCPSettings.__dataclass_fields__):
        raise ValueError("MCP 配置字段无效。")
    for name in ("git_enabled", "github_enabled", "git_write_enabled", "github_use_git_credentials", "workspace_write_enabled"):
        value = data.get(name, False if name == "github_use_git_credentials" else None)
        if type(value) is not bool and not (value is None and name != "github_use_git_credentials"):
            raise ValueError("MCP 启用状态无效。")
    roots, repos = data.get("git_repositories", []), data.get("github_repositories", [])
    if (not isinstance(roots, (list, tuple)) or not isinstance(repos, (list, tuple))
            or len(roots) > 10 or len(repos) > 10):
        raise ValueError("每种服务最多配置 10 个仓库。")
    paths = []
    for root in roots:
        if not isinstance(root, str) or not root.strip() or not Path(root).is_absolute() or root.startswith(("\\\\", "//")):
            raise ValueError("本地仓库必须填写绝对路径。")
        # Resolve again when executing: a junction may change after configuration.
        paths.append(str(Path(root).resolve()))
    names = []
    for repo in repos:
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("GitHub 仓库格式应为 owner/repo。")
        if any(part in {".", ".."} for part in repo.split("/")):
            raise ValueError("GitHub 仓库名称无效。")
        names.append(repo.lower())
    scopes = {}
    for field in ("github_private_repositories", "github_write_repositories", "github_admin_repositories"):
        values = data.get(field, [] if field == "github_private_repositories" else None)
        if values is None and field != "github_private_repositories":
            scopes[field] = None
            continue
        if (not isinstance(values, (list, tuple)) or len(values) > 10
                or any(not isinstance(value, str) or value.lower() not in names for value in values)):
            raise ValueError("私有访问和写操作仓库必须属于允许访问的 GitHub 仓库。")
        scopes[field] = tuple(dict.fromkeys(value.lower() for value in values))
    executable = data.get("github_executable", "")
    if not isinstance(executable, str) or (executable and not Path(executable).is_absolute()):
        raise ValueError("GitHub MCP 程序必须填写绝对路径。")
    workspace_roots = data.get("git_workspace_roots", [])
    if (not isinstance(workspace_roots, (list, tuple)) or len(workspace_roots) > 10
            or any(not isinstance(path, str) or not Path(path).is_absolute() or path.startswith(("\\\\", "//")) for path in workspace_roots)):
        raise ValueError("创建仓库的目录必须为绝对路径，最多 10 项。")
    workspace_roots = tuple(dict.fromkeys(str(Path(path).resolve()) for path in workspace_roots))
    return MCPSettings(data.get("git_enabled"), tuple(dict.fromkeys(paths)),
                       data.get("github_enabled"), executable, tuple(dict.fromkeys(names)),
                       data.get("git_write_enabled"),
                       scopes["github_private_repositories"], scopes["github_write_repositories"], workspace_roots,
                       scopes["github_admin_repositories"], data.get("github_use_git_credentials", False), data.get("workspace_write_enabled"))


def load_mcp_settings(path: Path = MCP_CONFIG_FILE) -> MCPSettings:
    if not path.exists():
        return MCPSettings()
    try:
        if path.stat().st_size > 32768:
            raise ValueError
        return normalize(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError, OSError):
        raise ValueError("后台工具配置损坏，请恢复最近的配置备份后重试。") from None


def save_mcp_settings(settings: MCPSettings, path: Path = MCP_CONFIG_FILE) -> MCPSettings:
    normalized = normalize(asdict(settings))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".mcp-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump(asdict(normalized), file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return normalized
