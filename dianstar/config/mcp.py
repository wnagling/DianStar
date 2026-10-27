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
    git_enabled: bool = False
    git_repositories: tuple[str, ...] = ()
    github_enabled: bool = False
    github_executable: str = ""
    github_repositories: tuple[str, ...] = ()
    git_write_enabled: bool = False


def normalize(data: dict) -> MCPSettings:
    if not isinstance(data, dict) or set(data) - set(MCPSettings.__dataclass_fields__):
        raise ValueError("MCP 配置字段无效。")
    for name in ("git_enabled", "github_enabled", "git_write_enabled"):
        if type(data.get(name, False)) is not bool:
            raise ValueError("MCP 启用状态无效。")
    roots, repos = data.get("git_repositories", []), data.get("github_repositories", [])
    if (not isinstance(roots, (list, tuple)) or not isinstance(repos, (list, tuple))
            or len(roots) > 10 or len(repos) > 10):
        raise ValueError("每种服务最多配置 10 个仓库。")
    paths = []
    for root in roots:
        if not isinstance(root, str) or not root.strip() or not Path(root).is_absolute():
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
    executable = data.get("github_executable", "")
    if not isinstance(executable, str) or (executable and not Path(executable).is_absolute()):
        raise ValueError("GitHub MCP 程序必须填写绝对路径。")
    if data.get("git_enabled") and not paths:
        raise ValueError("请先指定允许访问的本地仓库。")
    if data.get("github_enabled") and (not executable or not names):
        raise ValueError("请先指定 GitHub MCP 程序和允许访问的公开仓库。")
    return MCPSettings(data.get("git_enabled", False), tuple(dict.fromkeys(paths)),
                       data.get("github_enabled", False), executable, tuple(dict.fromkeys(names)),
                       data.get("git_write_enabled", False))


def load_mcp_settings(path: Path = MCP_CONFIG_FILE) -> MCPSettings:
    if not path.exists():
        return MCPSettings()
    try:
        if path.stat().st_size > 32768:
            raise ValueError
        return normalize(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError, OSError):
        raise ValueError("MCP 配置无效，请在 /config 的 MCP 页面检查并保存。") from None


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
