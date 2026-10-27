"""Scoped Git reads, independent of MCP and model providers."""

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import threading


MAX_OUTPUT = 24000


class GitReader:
    def __init__(self, repositories):
        self.repositories = tuple(Path(root).resolve(strict=True) for root in repositories)
        self.git = shutil.which("git")
        if self.git is None:
            raise ValueError("未找到 Git，请先安装并加入 PATH。")

    def check_repository(self, repository: str) -> Path:
        root = Path(repository)
        if not root.is_absolute():
            raise ValueError("仓库必须为已配置的绝对路径。")
        resolved = root.resolve(strict=True)
        if resolved not in self.repositories or resolved != root.absolute():
            raise ValueError("仓库不在允许范围内，或路径经过链接。")
        # First version excludes linked worktrees/submodules and external gitdirs.
        metadata = resolved / ".git"
        if not metadata.is_dir() or metadata.is_symlink() or metadata.resolve() != resolved / ".git":
            raise ValueError("仅支持 .git 位于仓库内的普通工作区。")
        config = metadata / "config"
        if config.is_symlink() or config.resolve() != metadata / "config":
            raise ValueError("不允许仓库配置经过链接。")
        if (metadata / "commondir").exists() or (metadata / "objects/info/alternates").exists():
            raise ValueError("暂不支持共享 Git 元数据或外部对象目录。")
        for name in ("HEAD", "index", "packed-refs", "shallow", "refs", "logs", "objects"):
            item = metadata / name
            if item.is_symlink() or item.resolve() != item.absolute():
                raise ValueError("Git 元数据不允许经过链接。")
        pending = [metadata / name for name in ("refs", "logs", "objects") if (metadata / name).is_dir()]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    item = Path(entry.path)
                    if entry.is_symlink() or (entry.is_dir(follow_symlinks=False) and item.resolve() != item.absolute()):
                        raise ValueError("Git 元数据不允许经过链接。")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(item)
        return resolved

    async def execute(self, operation: str, repository: str, *, staged: bool = False, limit: int = 10) -> dict:
        try:
            root = self.check_repository(repository)
            if type(staged) is not bool or type(limit) is not int or not 1 <= limit <= 30:
                raise ValueError("查询参数无效。")
            commands = {
                "status": ["status", "--porcelain=v1", "--branch", "--untracked-files=all"],
                "diff": ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=3",
                         *(["--cached"] if staged else []), "--", ".",
                         ":(exclude,icase)**/.env*", ":(exclude,icase).env*",
                         ":(exclude,icase)**/*.pem", ":(exclude,icase)*.pem",
                         ":(exclude,icase)**/*.key", ":(exclude,icase)*.key",
                         ":(exclude,icase)**/credentials*", ":(exclude,icase)credentials*",
                         ":(exclude,icase)**/secrets*", ":(exclude,icase)secrets*"],
                "log": ["log", f"-{limit}", "--format=%h %cs %s", "--no-decorate"],
            }
            if operation not in commands:
                raise ValueError("未开放此 Git 操作。")
            # No inherited GIT_*, model key, pager, SSH command or user Git config.
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}}
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat")
            prefix = [self.git, "--git-dir=" + str(root / ".git"), "--work-tree=" + str(root),
                    "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false",
                    "-c", "color.ui=false", "-c", "core.bare=false"]
            # A diff can invoke clean/process filters even with --no-textconv.
            # Inspect names only, without following config includes or returning values.
            configuration = await asyncio.to_thread(self._run, prefix + ["config", "--local", "--no-includes",
                "--name-only", "--list"], root, env, "configuration")
            if (configuration.get("error") or configuration.get("truncated") or any(
                    key.lower().startswith(("filter.", "include.", "includeif.", "extensions.worktreeconfig"))
                    for key in configuration.get("content", "").splitlines())):
                return {"error": "git_configuration_denied", "message": "仓库含过滤器、配置包含项或独立工作区配置，第一阶段未开放查询。"}
            args = prefix + commands[operation]
            return await asyncio.to_thread(self._run, args, root, env, operation)
        except (ValueError, OSError, TimeoutError):
            return {"error": "git_read_denied", "message": "仓库范围、Git 环境或查询超时检查未通过。"}

    @staticmethod
    def _run(args, root, env, operation):
        # Keep blocking pipe handling off the MCP event loop (including Windows).
        with subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})) as process:
            timed_out = threading.Event()
            def expire():
                timed_out.set()
                if process.poll() is None:
                    process.kill()
            timer = threading.Timer(10, expire)
            timer.daemon = True
            timer.start()
            output = bytearray()
            truncated = False
            try:
                while chunk := process.stdout.read1(4096):
                    remaining = MAX_OUTPUT - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                        process.kill()
                        break
                process.wait()
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                process.wait()
            if timed_out.is_set() or (process.returncode and not truncated):
                return {"error": "git_read_failed", "message": "Git 查询失败，请检查仓库状态和目录权限。"}
            text = output.decode("utf-8", errors="replace")
            text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
            return {"repository": str(root), "operation": operation, "content": text,
                    "truncated": truncated, "untrusted": True,
                    "note": "仅已跟踪内容；diff 排除常见密钥文件，仍不保证识别所有敏感内容。"}
