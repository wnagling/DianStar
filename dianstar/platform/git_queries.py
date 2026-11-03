"""Scoped Git reads, independent of MCP and model providers."""

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.request import getproxies
from dianstar.security.processes import terminate_tree
from dianstar.security.system_paths import check_host_path


MAX_OUTPUT = 24000


class GitReader:
    def __init__(self, repositories):
        self.repositories = tuple(Path(root).resolve(strict=True) for root in repositories)
        self.git = shutil.which("git")
        if self.git is None:
            raise ValueError("未找到 Git，请先安装并加入 PATH。")

    def check_repository(self, repository: str) -> Path:
        root = Path(repository)
        check_host_path(root, project=True)
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


class GitError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)



class SafeGitQueries(GitReader):
    def _git_dir(self, context):
        return context[0] / ".git"

    def _context(self, repository):
        root = self.check_repository(repository)
        env = {key: value for key, value in os.environ.items() if key.upper() in
               {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "USERPROFILE", "HOME",
                "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH"}}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                   GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat", GCM_INTERACTIVE="0")
        prefix = [self.git, "--git-dir=" + str(self._git_dir((root,))), "--work-tree=" + str(root), "--no-pager",
                  "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", "-c", "color.ui=false",
                  "-c", "core.bare=false", "-c", "core.hooksPath=" + os.devnull,
                  "-c", "commit.gpgSign=false", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                  "-c", "submodule.recurse=false", "-c", "diff.submodule=short"]
        context = (root, env, prefix)
        keys = self._query(context, ["config", "--local", "--no-includes", "--name-only", "--list"]).decode().lower().splitlines()
        if any(key.startswith(("filter.", "include.", "includeif.", "extensions.worktreeconfig", "extensions.partialclone", "core.splitindex", "url."))
               or (key.startswith("remote.") and key.endswith((".promisor", ".partialclonefilter")))
               or (key.startswith("merge.") and key.endswith(".driver"))
               or (key.startswith("diff.") and key.endswith(".command")) for key in keys):
            raise GitError("git_configuration_denied", "仓库含过滤器、配置包含项、URL 重写或特殊索引配置，未执行。")
        # Preserve trusted user EOL settings without loading their command hooks,
        # filters, aliases or includes into repository operations.
        global_env = dict(env)
        global_env.pop("GIT_CONFIG_GLOBAL", None)
        global_env.pop("GIT_CONFIG_NOSYSTEM", None)
        code, values = self._invoke((root, global_env, [self.git]), ["config", "--no-includes", "--get-regexp",
            r"^core\.(autocrlf|eol|safecrlf)$"], optional=True)
        if code == 0:
            for line in values.decode().splitlines():
                key, _, value = line.partition(" ")
                if key.lower() not in keys and value.lower() in {"true", "false", "input", "warn", "lf", "crlf", "native"}:
                    prefix += ["-c", key + "=" + value]
        return context

    @staticmethod
    def _invoke(context, args, *, optional=False, env_extra=None, network=False, timeout=None,
                input_data=None, output_limit=2 * 1024 * 1024):
        root, base_env, prefix = context
        env = {**base_env, **(env_extra or {})}
        if network:
            for scheme, proxy in getproxies().items():
                if scheme in {"http", "https", "all", "no"}:
                    env[scheme.upper() + "_PROXY"] = proxy
            if "SSH_AUTH_SOCK" in os.environ:
                env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        with subprocess.Popen(prefix + args, cwd=root, env=env, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True})) as process:
            expired = threading.Event()
            def stop():
                expired.set()
                terminate_tree(process)
            timer = threading.Timer(timeout or (30 if network else 8), stop)
            timer.daemon = True
            timer.start()
            writer = None
            if input_data is not None:
                def send():
                    try:
                        process.stdin.write(input_data)
                    except (BrokenPipeError, OSError):
                        pass
                    finally:
                        try:
                            process.stdin.close()
                        except (BrokenPipeError, OSError):
                            pass
                writer = threading.Thread(target=send, daemon=True)
                writer.start()
            output = bytearray()
            try:
                while chunk := process.stdout.read1(8192):
                    output.extend(chunk)
                    if len(output) > output_limit:
                        stop()
                        break
                process.wait()
                if writer is not None:
                    writer.join(timeout=1)
            finally:
                timer.cancel()
                terminate_tree(process)
                process.wait()
            if expired.is_set():
                raise GitError("git_outcome_unknown", "Git 超时或输出过大；请先查询当前状态，不要自动重试写操作。")
            if process.returncode and not optional:
                text = bytes(output).lower()
                reason = ("远程拒绝非快进推送，请先获取并检查分叉；不会强制覆盖。" if b"non-fast-forward" in text or b"fetch first" in text else
                          "Git 操作未成功；请检查身份、凭据、网络或仓库状态。未自动回滚或重试。")
                raise GitError("git_command_failed", reason)
            return process.returncode, bytes(output)

    def _query(self, context, args, **kwargs):
        return self._invoke(context, args, **kwargs)[1]
