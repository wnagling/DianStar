"""Application-owned Git plans. Preview tokens are one-use, state-bound, short-lived."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import getproxies

from dianstar.tools.mcp.servers.git.reader import GitReader

from dianstar.tools.mcp.git_contract import READ_TOOLS, WRITE_TOOLS
from dianstar.security.processes import terminate_tree
from dianstar.security.content_guard import protected_path, SecurityViolation
from dianstar.hooks.git import GitSecurityHooks

LABELS = {"git_stage": "暂存指定文件", "git_unstage": "取消指定文件的暂存",
          "git_create_branch": "创建分支", "git_switch_branch": "切换分支",
          "git_commit": "创建提交", "git_fetch": "获取远程分支", "git_push": "推送提交"}


class GitError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sensitive(path):
    return protected_path(path)


def branch_name(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,150}", value)
            or value == "HEAD" or ".." in value or "//" in value or value.endswith(("/", ".", ".lock"))
            or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise GitError("git_invalid_branch", "分支名称无效；不接受选项、修订表达式或特殊引用。")
    return value


class GitWorkspace(GitReader):
    def __init__(self, repositories, allow_writes=False):
        super().__init__(repositories)
        self.allow_writes = allow_writes
        self.plans = {}
        self.locks = {str(root): threading.RLock() for root in self.repositories}
        self.security = GitSecurityHooks(self)

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

    def _head(self, context):
        code, value = self._invoke(context, ["rev-parse", "--verify", "HEAD"], optional=True)
        return value.decode().strip() if code == 0 else None

    def _branch(self, context):
        code, value = self._invoke(context, ["symbolic-ref", "--quiet", "--short", "HEAD"], optional=True)
        if code:
            raise GitError("git_detached_head", "当前为 detached HEAD，第一批写操作要求具名分支。")
        return branch_name(value.decode().strip())

    def _files(self, root, files, *, allow_sensitive=False):
        if not isinstance(files, list) or not 1 <= len(files) <= 100 or len(set(files)) != len(files):
            raise GitError("git_invalid_files", "请明确选择 1–100 个不同文件，不接受目录或全量暂存。")
        for name in files:
            if (not isinstance(name, str) or not name or "\\" in name or any(ord(char) < 32 for char in name)
                    or ":" in name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))
                    or (sensitive(name) and not allow_sensitive)):
                raise GitError("git_file_denied", "文件路径越界、格式无效或属于受限敏感路径。")
            path = root / name
            if path.resolve() != path.absolute() or path.is_symlink() or (path.exists() and not path.is_file()):
                raise GitError("git_file_denied", "不允许目录、符号链接、junction 或仓库外文件。")
        return [":(literal)" + name for name in files]

    def _state(self, context):
        root = context[0]
        status = self._query(context, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"])
        index = self._git_dir(context) / "index"
        if index.exists() and (index.is_symlink() or index.resolve() != index.absolute() or index.stat().st_size > 2 * 1024 * 1024):
            raise GitError("git_index_denied", "索引路径或大小超出当前支持范围。")
        index_bytes = index.read_bytes() if index.exists() else b""
        config = (self._git_dir(context) / "config").read_bytes()
        refs = self._query(context, ["for-each-ref", "--format=%(refname) %(objectname)"])
        markers = [name for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer", "index.lock", "HEAD.lock") if (self._git_dir(context) / name).exists()]
        if markers or self._query(context, ["ls-files", "--unmerged"]):
            raise GitError("git_integration_in_progress", "存在冲突、进行中的整合或 Git 锁，请先处理；未执行。")
        # Hash changed tracked files too: approval must not silently survive edits.
        changed = self._query(context, ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z"]).split(b"\0")
        content = []
        total = 0
        for raw in changed:
            if not raw:
                continue
            name = raw.decode("utf-8", errors="strict")
            path = root / name
            if path.is_file() and not path.is_symlink() and path.resolve() == path.absolute():
                size = path.stat().st_size
                total += size
                if total > 16 * 1024 * 1024:
                    raise GitError("git_change_too_large", "修改内容过大，请缩小本次操作范围。")
                content.append([name, digest(path.read_bytes())])
            else:
                content.append([name, "missing-or-link"])
        state = {"head": self._head(context), "branch": self._branch(context), "status": digest(status),
                 "index": digest(index_bytes), "config": digest(config), "refs": digest(refs), "files": content,
                 "settings": context[2]}
        return digest(json.dumps(state, sort_keys=True).encode()), status, index_bytes

    def _identity(self, context):
        values = []
        for key in ("user.name", "user.email"):
            code, value = self._invoke(context, ["config", "--local", "--get", key], optional=True)
            if code:
                env = dict(context[1])
                env.pop("GIT_CONFIG_GLOBAL", None)
                code, value = self._invoke((context[0], env, [self.git]),
                    ["config", "--global", "--no-includes", "--get", key], optional=True)
            text = value.decode().strip() if code == 0 else ""
            if not text or len(text) > 200 or any(ord(char) < 32 for char in text):
                raise GitError("git_identity_missing", "请先配置本地或全局 Git 的 user.name 和 user.email。")
            values.append(text)
        return values

    def _remote(self, context, name, push=False):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,63}", name):
            raise GitError("git_remote_denied", "远程名称无效。")
        key = f"remote.{name}.pushurl" if push else f"remote.{name}.url"
        code, raw = self._invoke(context, ["config", "--local", "--get-all", key], optional=True)
        if code and push:
            return self._remote(context, name, False)
        urls = raw.decode().splitlines() if code == 0 else []
        if len(urls) != 1:
            raise GitError("git_remote_denied", "远程未配置或存在多个目标地址，请先明确为单一目标。")
        url = urls[0]
        if any(ord(char) < 32 for char in url) or url.startswith("-"):
            raise GitError("git_remote_denied", "远程地址无效。")
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
            return url
        if re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", url):
            return url
        if (parsed.scheme == "ssh" and parsed.hostname and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", parsed.hostname)
                and (parsed.username is None or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", parsed.username))
                and re.fullmatch(r"/[A-Za-z0-9_./-]+", parsed.path)
                and not parsed.password and not parsed.query and not parsed.fragment):
            return url
        path = Path(url)
        if path.is_absolute() and not url.startswith(("\\\\", "//")) and path.exists() and path.resolve() == path.absolute():
            return str(path)
        raise GitError("git_remote_denied", "仅支持无内嵌密码的 HTTPS、SSH 或明确的本地路径；不支持自定义传输。")

    def _network_context(self, context, url):
        root, env, prefix = context
        extra = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
                 "-c", "protocol.file.allow=always", "-c", "http.followRedirects=false", "-c", "http.sslVerify=true",
                 "-c", "http.extraHeader=", "-c", "core.askPass=", "-c", "credential.helper=", "-c", "credential.interactive=false"]
        if os.name == "nt":
            extra += ["-c", "credential.helper=manager"]
        if urlsplit(url).scheme == "https" and urlsplit(url).hostname == "github.com":
            namespace, login = os.environ.get("DIANSTAR_GITHUB_NAMESPACE", ""), os.environ.get("DIANSTAR_GITHUB_LOGIN", "")
            if os.environ.get("DIANSTAR_GITHUB_SIGNED_OUT") == "1":
                extra += ["-c", "credential.helper="]
            elif re.fullmatch(r"dianstar-[a-f0-9]{32}", namespace) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login):
                env = {**env, "GCM_NAMESPACE": namespace, "GCM_INTERACTIVE": "0",
                       "GCM_CREDENTIAL_STORE": "wincredman" if os.name == "nt" else "keychain" if sys.platform == "darwin" else "secretservice"}
                if os.name != "nt":
                    extra += ["-c", "credential.helper=manager"]
                extra += ["-c", "credential.username=" + login]
        if url.startswith("ssh://") or re.match(r"[^/]+@[^:]+:", url):
            ssh = shutil.which("ssh")
            if ssh is None:
                raise GitError("git_ssh_unavailable", "未找到 SSH 程序。")
            env = {**env, "GIT_SSH_COMMAND": shlex.quote(ssh.replace("\\", "/")) +
                   " -F " + shlex.quote(os.devnull) + " -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10"}
        return root, env, prefix + extra

    def read(self, name, repository, *, staged=False, limit=10):
        try:
            context = self._context(repository)
            if name == "git_status":
                content = self._query(context, ["status", "--porcelain=v1", "--branch", "--untracked-files=all", "--ignore-submodules=all"]).decode()
            elif name == "git_diff":
                if type(staged) is not bool:
                    raise GitError("git_invalid_arguments", "staged 必须为布尔值。")
                content = self._query(context, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=3",
                    *(["--cached"] if staged else []), "--", ".",
                    ":(exclude,icase)**/.env*", ":(exclude,icase).env*", ":(exclude,icase)**/*.pem", ":(exclude,icase)*.pem",
                    ":(exclude,icase)**/*.key", ":(exclude,icase)*.key", ":(exclude,icase)**/credentials*", ":(exclude,icase)credentials*",
                    ":(exclude,icase)**/secrets*", ":(exclude,icase)secrets*"]).decode(errors="replace")
            elif name == "git_log":
                if type(limit) is not int or not 1 <= limit <= 30:
                    raise GitError("git_invalid_arguments", "limit 必须为 1–30 的整数。")
                content = self._query(context, ["log", f"-{limit}", "--format=%h %cs %s", "--no-decorate"]).decode(errors="replace")
            elif name == "git_branches":
                content = self._query(context, ["for-each-ref", "--format=%(HEAD) %(refname:short) %(objectname:short) %(upstream:short) %(upstream:track)", "refs/heads/"]).decode()
            elif name == "git_remotes":
                names = self._query(context, ["remote"]).decode().splitlines()
                items = []
                for remote in names[:30]:
                    try:
                        items.append({"name": remote, "fetch": self._remote(context, remote), "push": self._remote(context, remote, True)})
                    except GitError:
                        items.append({"name": remote, "error": "远程地址未通过检查，未展示原始地址。"})
                content = json.dumps(items, ensure_ascii=False)
            else:
                raise GitError("git_tool_denied", "未开放此查询。")
            return {"repository": repository, "content": content[:24000], "truncated": len(content) > 24000, "untrusted": True}
        except (GitError, ValueError, OSError) as error:
            return self._error(error)

    @staticmethod
    def _error(error):
        if isinstance(error, SecurityViolation):
            return error.result()
        return {"error": getattr(error, "code", "git_operation_failed"),
                "message": getattr(error, "message", "Git 范围或环境检查失败，未继续执行。")}

    def prepare(self, name, repository, **arguments):
        try:
            if not self.allow_writes or name not in WRITE_TOOLS:
                raise GitError("git_write_disabled", "尚未启用 Git 写操作申请。")
            context = self._context(repository)
            with self.locks[str(context[0])]:
                return self._prepare(context, name, arguments)
        except (GitError, ValueError, OSError, UnicodeError) as error:
            return self._error(error)

    def _prepare(self, context, name, arguments):
        root = context[0]
        snapshot, status, index_bytes = self._state(context)
        head, current = self._head(context), self._branch(context)
        lines = [f"操作：{LABELS[name]}", f"仓库：{root}", f"当前分支：{current}"]
        details = {}
        if name in {"git_stage", "git_unstage"}:
            paths = self._files(root, arguments.get("files"), allow_sensitive=name == "git_unstage")
            if name == "git_stage":
                self.security.hooks.run("before_stage", ((file, (root / file).read_bytes())
                    for file in arguments["files"] if (root / file).is_file()))
            details["paths"] = paths
            lines += ["文件：" + ", ".join(arguments["files"])]
            patch = self._query(context, ["diff", "--no-ext-diff", "--no-textconv", "--no-color",
                *(["--cached"] if name == "git_unstage" else []), "--", *paths]).decode(errors="replace")
            selected = []
            total = 0
            for file in arguments["files"]:
                path = root / file
                if path.is_file() and path.stat().st_size > 16 * 1024 * 1024:
                    raise GitError("git_change_too_large", "选中文件过大，请缩小操作范围。")
                data = path.read_bytes() if path.is_file() else b""
                total += len(data)
                if total > 16 * 1024 * 1024:
                    raise GitError("git_change_too_large", "选中文件过大，请缩小操作范围。")
                selected.append([file, digest(data), path.exists()])
                if name == "git_stage":
                    tracked = self._invoke(context, ["ls-files", "--error-unmatch", "--", ":(literal)" + file], optional=True)[0] == 0
                    if not tracked:
                        if not path.is_file():
                            raise GitError("git_file_missing", "选择的文件不存在且未被跟踪。")
                        patch += f"\n新增文件 {file}（{len(data)} 字节）\n" + (data.decode("utf-8", errors="replace") if b"\0" not in data else f"二进制文件 SHA256：{digest(data)}")
            details["selected"] = selected
            lines += [patch or "无文本差异。", "仅修改暂存区，不改写工作区文件。"]
        elif name in {"git_create_branch", "git_switch_branch"}:
            branch = branch_name(arguments.get("branch"))
            exists = self._invoke(context, ["show-ref", "--verify", "--quiet", "refs/heads/" + branch], optional=True)[0] == 0
            if name == "git_create_branch":
                if exists or head is None:
                    raise GitError("git_branch_unavailable", "分支已存在，或仓库尚无初始提交。")
                base = arguments.get("start_point", "HEAD")
                if base != "HEAD":
                    base = "refs/heads/" + branch_name(base)
                code, raw = self._invoke(context, ["rev-parse", "--verify", base], optional=True)
                if code:
                    raise GitError("git_branch_unavailable", "基准分支不存在。")
                details["base"] = raw.decode().strip()
                checkout = arguments.get("checkout", False)
                if type(checkout) is not bool or (checkout and status and details["base"] != head):
                    raise GitError("git_dirty_worktree", "有未提交修改时，只允许从当前 HEAD 创建并切换新分支。")
                lines += [f"新分支：{branch}", f"基准提交：{details['base']}", f"创建后切换：{checkout}"]
            else:
                if not exists or status:
                    raise GitError("git_dirty_worktree", "切换现有分支要求工作区及暂存区干净，且目标分支存在。")
                lines += [f"目标分支：{branch}"]
        elif name == "git_commit":
            message = arguments.get("message")
            if (not isinstance(message, str) or not message.strip() or len(message) > 4000
                    or any(ord(char) < 32 and char not in "\n\t" for char in message)):
                raise GitError("git_invalid_message", "提交说明不能为空且最多 4000 字符。")
            staged = self._query(context, ["diff", "--cached", "--name-only", "-z"]).split(b"\0")
            files = [file.decode() for file in staged if file]
            present = self._query(context, ["diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z"]).split(b"\0")
            if not files or any(sensitive(file.decode()) for file in present if file):
                raise GitError("git_commit_denied", "没有暂存修改，或暂存区包含受限敏感路径。")
            self.security.index(context)
            self.security.hooks.run("before_commit", [("[提交说明]", message.encode())])
            identity = self._identity(context)
            details.update(identity=identity, index=index_bytes, head=head, branch=current)
            patch = self._query(context, ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color"]).decode(errors="replace")
            lines += ["作者：" + " <".join(identity) + ">", "提交说明：" + message, "暂存文件：" + ", ".join(files), patch,
                      "提交已批准的暂存快照；未暂存修改保持不变。Git hooks 和签名不运行，请单独完成项目检查。"]
        else:
            remote = arguments.get("remote", "origin")
            url = self._remote(context, remote, name == "git_push")
            if name == "git_push" and arguments.get("set_upstream", False) and self._remote(context, remote) != url:
                raise GitError("git_remote_denied", "获取与推送地址不同，不能自动设置上游，请先明确远程配置。")
            self._network_context(context, url)
            details.update(url=url, remote=remote)
            lines += [f"远程：{remote}", f"地址：{url}"]
            if name == "git_push":
                if head is None:
                    raise GitError("git_no_commit", "没有可推送的提交。")
                target = branch_name(arguments.get("branch", current))
                details["security_remote_head"] = self.security.remote_head(context, url, target)
                self.security.push(context, head, details["security_remote_head"])
                details.update(head=head, branch=current, target=target)
                force = arguments.get("force_with_lease", False)
                if type(force) is not bool:
                    raise GitError("git_invalid_arguments", "force_with_lease 必须为布尔值。")
                if force:
                    advertised = self._query(self._network_context(context, url), ["ls-remote", "--heads", "--", url, "refs/heads/" + target], network=True).decode().split()
                    details["lease"] = advertised[0] if advertised else ""
                tracking = "refs/remotes/" + remote + "/" + target
                known = self._invoke(context, ["rev-parse", "--verify", tracking], optional=True)[0] == 0
                commits = self._query(context, ["log", "--format=%h %s", "--max-count=31", (tracking + ".." + head) if known else head]).decode()
                lines += [f"推送：{current} → refs/heads/{target}", f"本地提交：{head}",
                          "待推送提交（按本地远程跟踪记录估算，最多展示 31 条）：\n" + commits,
                          f"设置上游分支：{arguments.get('set_upstream', False)}", "普通推送，不强制覆盖，不推送其他分支或标签。"]
                if force:
                    lines[-1] = f"明确申请 force-with-lease：仅当远程仍为 {details['lease'] or '分支不存在'} 时允许替换历史，不使用无条件强推。"
            else:
                lines += ["仅获取远程分支到对应跟踪引用，不切换分支、不合并、不删除旧引用。"]
        description = self.security.hooks.scanner.redact("\n".join(lines))
        if len(description) > 24000:
            raise GitError("git_preview_too_large", "待确认内容过长，请缩小暂存或提交范围，不能省略差异后直接批准。")
        self.plans = {token: plan for token, plan in self.plans.items() if plan["expires"] > time.monotonic()}
        if len(self.plans) >= 30:
            raise GitError("git_plan_limit", "本轮 Git 预览数量已达上限。")
        token = secrets.token_urlsafe(32)
        self.plans[token] = dict(repository=str(root), name=name, arguments=arguments,
                                 snapshot=snapshot, details=details, expires=time.monotonic() + 600)
        preview = {"prepared": True, "token": token, "description": description}
        if name == "git_push":
            preview["remote_target"] = {"url": self.security.hooks.scanner.redact(details["url"]),
                                        "remote": details["remote"], "branch": details["target"]}
        return preview

    def apply(self, token):
        plan = self.plans.pop(token, None)
        if plan is None or plan["expires"] <= time.monotonic():
            return {"error": "git_plan_expired", "message": "操作预览已过期或已使用，未执行。"}
        try:
            context = self._context(plan["repository"])
            with self.locks[plan["repository"]]:
                if self._state(context)[0] != plan["snapshot"]:
                    raise GitError("git_state_changed", "批准后文件、暂存区、分支、引用或远程配置已改变，未执行；请重新检查。")
                return self._apply(context, plan)
        except (GitError, ValueError, OSError, UnicodeError) as error:
            return self._error(error)

    def _apply(self, context, plan):
        name, args, details = plan["name"], plan["arguments"], plan["details"]
        root = context[0]
        result = {"operation": name, "repository": str(root)}
        if name in {"git_stage", "git_unstage"}:
            self._files(root, args["files"], allow_sensitive=name == "git_unstage")
            for file, expected, existed in details["selected"]:
                path = root / file
                if path.exists() != existed or digest(path.read_bytes() if path.is_file() else b"") != expected:
                    raise GitError("git_state_changed", "选中文件已改变，未执行。")
            if name == "git_stage":
                self.security.hooks.run("before_stage", ((file, (root / file).read_bytes())
                    for file in args["files"] if (root / file).is_file()))
            command = (["add", "--", *details["paths"]] if name == "git_stage" else
                       ["restore", "--staged", "--source=HEAD", "--", *details["paths"]] if self._head(context) else
                       ["update-index", "--force-remove", "--", *args["files"]])
            self._query(context, command)
        elif name == "git_create_branch":
            command = (["switch", "-c", args["branch"], details["base"]] if args.get("checkout", False) else
                       ["branch", "--", args["branch"], details["base"]])
            self._query(context, command)
        elif name == "git_switch_branch":
            self._query(context, ["switch", "--no-guess", "--", args["branch"]])
        elif name == "git_commit":
            if self._identity(context) != details["identity"]:
                raise GitError("git_state_changed", "提交身份已改变，未执行。")
            # A private index freezes exactly what the user approved. update-ref
            # compares the old HEAD, so an external commit is never overwritten.
            with tempfile.TemporaryDirectory(prefix="dianstar-index-", dir=self._git_dir(context)) as temporary:
                index = Path(temporary) / "index"
                index.write_bytes(details["index"])
                tree = self._query(context, ["write-tree"], env_extra={"GIT_INDEX_FILE": str(index)}).decode().strip()
                self.security.tree(context, tree)
                self.security.hooks.run("before_commit", [("[提交说明]", args["message"].encode())])
                prefix = context[2] + ["-c", "user.name=" + details["identity"][0], "-c", "user.email=" + details["identity"][1]]
                sha = self._query((root, context[1], prefix), ["commit-tree", tree,
                    *(["-p", details["head"]] if details["head"] else []), "-m", args["message"]]).decode().strip()
                self._query(context, ["update-ref", "-m", "DianStar commit", "refs/heads/" + details["branch"], sha,
                                      details["head"] or "0" * len(sha)])
                if self._head(context) != sha:
                    raise GitError("git_outcome_unknown", "提交后 HEAD 已改变，请查询状态核对。")
                result["commit"] = sha
        elif name in {"git_fetch", "git_push"}:
            network = self._network_context(context, details["url"])
            if name == "git_fetch":
                self._query(network, ["fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--no-auto-maintenance",
                    "--", details["url"], "refs/heads/*:refs/remotes/" + details["remote"] + "/*"], network=True)
                result["tracking_refs"] = self._query(context, ["for-each-ref", "--format=%(refname:short) %(objectname)",
                    "refs/remotes/" + details["remote"] + "/"]).decode()
            else:
                if self.security.remote_head(context, details["url"], details["target"]) != details["security_remote_head"]:
                    raise GitError("git_state_changed", "远程分支在确认期间已变化，请重新检查待推送历史。")
                self.security.push(context, details["head"], details["security_remote_head"])
                spec = details["head"] + ":refs/heads/" + details["target"]
                self._query(network, ["push", "--porcelain", "--no-follow-tags", "--no-verify",
                    *(["--force-with-lease=refs/heads/" + details["target"] + ":" + details["lease"]] if "lease" in details else []),
                    "--", details["url"], spec], network=True)
                remote = self._query(network, ["ls-remote", "--heads", "--", details["url"], "refs/heads/" + details["target"]], network=True).decode().split()
                if not remote or remote[0] != details["head"]:
                    raise GitError("git_remote_verification_failed", "推送命令已结束，但远程提交未核验一致；不要自动重推。")
                if args.get("set_upstream", False):
                    self._query(context, ["update-ref", "refs/remotes/" + details["remote"] + "/" + details["target"], details["head"]])
                    self._query(context, ["config", "--local", "branch." + details["branch"] + ".remote", details["remote"]])
                    self._query(context, ["config", "--local", "branch." + details["branch"] + ".merge", "refs/heads/" + details["target"]])
                result.update(commit=details["head"], remote=details["remote"], branch=details["target"], remote_verified=True,
                              remote_url=self.security.hooks.scanner.redact(details["url"]))
        result.update(success=True, content=self._query(context, ["status", "--porcelain=v1", "--branch", "--untracked-files=all", "--ignore-submodules=all"]).decode(), untrusted=True)
        return result
