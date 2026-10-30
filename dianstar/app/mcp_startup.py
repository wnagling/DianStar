"""Automatic connections; inferred scopes never change persisted write permissions."""
import configparser
from dataclasses import replace
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

from dianstar.tools.mcp.credentials import github_token
from dianstar.tools.mcp.github_server import discover_server


def project_repositories(directory):
    """Find the opened project and, for a verified worktree, its main repo."""
    directory = Path(directory).absolute()
    try:
        if str(directory).startswith(("\\\\", "//")) or directory.resolve(strict=True) != directory:
            return ()
        for root in (directory, *directory.parents):
            marker = root / ".git"
            if marker.resolve() != marker.absolute():
                return ()
            if not marker.exists():
                continue
            if marker.is_dir():
                return (str(root),)
            if not marker.is_file() or marker.stat().st_size > 4096:
                return ()
            text = marker.read_text().strip()
            if not text.startswith("gitdir: "):
                return ()
            metadata = Path(text[8:])
            metadata = (root / metadata).absolute() if not metadata.is_absolute() else metadata.absolute()
            if metadata.resolve(strict=True) != metadata:
                return ()
            for name in ("commondir", "gitdir"):
                item = metadata / name
                if item.resolve() != item.absolute() or item.stat().st_size > 4096:
                    return ()
            common = (metadata / (metadata / "commondir").read_text().strip()).resolve(strict=True)
            main = common.parent
            if (common.name != ".git" or metadata.parent != common / "worktrees"
                    or common.resolve() != common.absolute()
                    or (metadata / "gitdir").read_text().strip().replace("\\", "/") != str(marker).replace("\\", "/")):
                return ()
            return (str(main), str(root))
    except (OSError, ValueError, UnicodeError):
        pass
    return ()


def github_repository(repository):
    """Parse only origin; config includes and commands are never evaluated."""
    config = Path(repository) / ".git" / "config"
    try:
        if config.resolve() != config.absolute() or config.stat().st_size > 131072:
            return None
        parser = configparser.RawConfigParser(strict=False)
        parser.read_string(config.read_text(encoding="utf-8"))
        value = parser.get('remote "origin"', "url", fallback="").strip()
        match = re.fullmatch(r"git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?", value)
        if match:
            name = match[1]
        else:
            url = urlsplit(value)
            if (url.scheme not in {"https", "ssh"} or url.hostname != "github.com"
                    or url.password or url.query or url.fragment or url.port
                    or (url.scheme == "https" and url.username) or (url.scheme == "ssh" and url.username not in {None, "git"})):
                return None
            name = url.path.strip("/")
        name = name.removesuffix(".git")
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) and all(p not in {".", ".."} for p in name.split("/")):
            return name.lower()
    except (OSError, ValueError, UnicodeError, configparser.Error):
        pass
    return None


def resolve_connections(settings, env_file, directory):
    """None selects auto-connect; False preserves a user's explicit disable."""
    status = {}
    git = settings.git_enabled is not False
    if git and shutil.which("git") is None:
        git = False
        status["git"] = {"state": "not_installed", "message": "未找到 Git，请安装并加入 PATH；无需开启 MCP 开关。"}
    elif not git:
        status["git"] = {"state": "disabled", "message": "本地 Git 已在设置中明确停用。"}
    discover = (settings.git_enabled is None or settings.github_enabled is None
                or settings.github_write_repositories is None or settings.github_admin_repositories is None)
    current = project_repositories(directory) if discover else ()
    inferred_local = current if git and settings.git_enabled is None else ()
    repositories = tuple(dict.fromkeys((*settings.git_repositories, *inferred_local)))
    github_repositories = settings.github_repositories
    project_github = tuple(dict.fromkeys(filter(None, (github_repository(p) for p in current))))
    if settings.github_enabled is None:
        github_repositories = tuple(dict.fromkeys((*github_repositories, *project_github)))
    # Default mutation scope is only the current project's allowed origin.
    # Account-level token permissions never expand this application scope.
    mutation_scope = tuple(p for p in project_github if p in github_repositories)
    github = settings.github_enabled is not False
    executable = discover_server(settings.github_executable, env_file.parent) if github else settings.github_executable
    if github:
        missing = []
        if not executable:
            missing.append("GitHub 连接组件")
        if not github_repositories:
            missing.append("当前项目的 GitHub origin 或允许访问的仓库")
        if not missing and not github_token(settings, env_file):
            missing.append("GitHub 登录凭据")
        if missing:
            github = False
            instruction = ("请在 /github 或设置的 GitHub 账号中完成登录，随后会自动连接。"
                           if "GitHub 登录凭据" in missing else "请完成 GitHub 登录或检查应用安装，随后会自动连接。")
            status["github"] = {"state": "not_configured", "missing": missing,
                                "message": "GitHub 暂不可用：缺少" + "、".join(missing) + "。" + instruction}
    else:
        status["github"] = {"state": "disabled", "message": "GitHub 已在设置中明确停用。"}
    effective = replace(settings, git_enabled=git, git_repositories=repositories,
                        git_write_enabled=settings.git_write_enabled is not False,
                        workspace_write_enabled=settings.workspace_write_enabled is not False,
                        github_enabled=github, github_executable=executable, github_repositories=github_repositories,
                        github_write_repositories=mutation_scope if settings.github_write_repositories is None else settings.github_write_repositories,
                        github_admin_repositories=mutation_scope if settings.github_admin_repositories is None else settings.github_admin_repositories)
    return effective, status
