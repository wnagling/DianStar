"""Repository lifecycle and engineering workflows over state-bound Git plans."""
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from .operations import GitWorkspace as BasicWorkspace, GitError, branch_name, digest, sensitive
from dianstar.tools.mcp.git_contract import BOOTSTRAP_TOOLS
from .workflows import WorkflowMixin
from .publish import PublishMixin


CONFIG_KEYS = {"user.name", "user.email", "core.autocrlf", "core.eol", "core.safecrlf", "pull.ff", "pull.rebase"}
EXTRA_READS = {"git_show", "git_diff_refs", "git_config_read", "git_worktree_list", "git_stash_list", "git_conflicts"}
EXTRA_WRITES = {"git_remote_set", "git_remote_remove", "git_set_upstream", "git_config_set", "git_checkout_remote",
    "git_pull", "git_restore", "git_revert", "git_merge", "git_rebase", "git_cherry_pick", "git_conflict_continue",
    "git_conflict_abort", "git_stash_save", "git_stash_apply", "git_stash_drop", "git_checkpoint", "git_worktree_add",
    "git_worktree_remove", "git_branch_rename", "git_branch_delete", "git_remote_branch_delete", "git_publish"}
PROGRESS = {"git_stage", "git_unstage", "git_conflict_continue", "git_conflict_abort"}


class GitWorkspace(PublishMixin, WorkflowMixin, BasicWorkspace):
    def __init__(self, repositories, allow_writes=False, workspace_roots=(), *, allow_clone=False, confirm_destinations=False):
        super().__init__(repositories, allow_writes)
        self.allow_clone = allow_clone
        self.confirm_destinations = confirm_destinations and allow_writes
        self.workspace_roots = tuple(Path(p).resolve(strict=True) for p in workspace_roots)
        self.managed_metadata = {}
        self.flags = threading.local()
        self.lifecycle_lock = threading.RLock()
        # Recover explicitly configured linked worktrees after application restart.
        for root in self.repositories:
            if (root / ".git").is_file():
                self._adopt_worktree(root)

    def _adopt_worktree(self, root):
        marker = root / ".git"
        if marker.is_symlink() or marker.resolve() != marker.absolute() or marker.stat().st_size > 4096:
            raise ValueError("Invalid worktree metadata")
        text = marker.read_text().strip()
        if not text.startswith("gitdir: "):
            raise ValueError("Invalid worktree metadata")
        metadata = Path(text[8:])
        metadata = (root / metadata).absolute() if not metadata.is_absolute() else metadata.absolute()
        if metadata.resolve() != metadata or not metadata.is_dir():
            raise ValueError("Invalid worktree metadata")
        for name in ("commondir", "gitdir"):
            item = metadata / name
            if item.resolve() != item.absolute() or item.stat().st_size > 4096:
                raise ValueError("Invalid worktree metadata link")
        common = (metadata / (metadata / "commondir").read_text().strip()).resolve(strict=True)
        parents = [p for p in self.repositories if (p / ".git").is_dir() and common == p / ".git"]
        if not parents or (metadata / "gitdir").read_text().strip().replace("\\", "/") != str(marker).replace("\\", "/"):
            raise ValueError("Worktree must belong to an explicitly allowed repository")
        super().check_repository(str(parents[0]))
        if metadata.parent != common / "worktrees":
            raise ValueError("Worktree metadata escaped parent repository")
        self.managed_metadata[str(root)] = metadata
        self.locks[str(root)] = self.locks[str(parents[0])]

    def check_repository(self, repository):
        root = Path(repository)
        if not root.is_absolute() or root not in self.repositories or root.resolve(strict=True) != root.absolute():
            raise ValueError("Repository is outside the allowed scope")
        if (root / ".git").is_file():
            self._adopt_worktree(root)
            metadata = self.managed_metadata[str(root)]
            for path in metadata.rglob("*"):
                if path.is_symlink() or path.resolve() != path.absolute():
                    raise ValueError("Linked metadata is not allowed")
            return root
        return super().check_repository(repository)

    def _git_dir(self, context):
        return self.managed_metadata.get(str(context[0]), context[0] / ".git")

    def _branch(self, context):
        try:
            return super()._branch(context)
        except GitError:
            if getattr(self.flags, "progress", False) and self._progress(context) == "rebase":
                for directory in ("rebase-merge", "rebase-apply"):
                    path = self._git_dir(context) / directory / "head-name"
                    if path.is_file():
                        return branch_name(path.read_text().strip().removeprefix("refs/heads/"))
            raise

    def _allow_destination(self, value, *, clone=False):
        path = Path(value)
        scoped = any(path != root and path.is_relative_to(root) for root in self.workspace_roots)
        if not self.workspace_roots and (self.confirm_destinations or (clone and self.allow_clone)):
            scoped = path != path.parent
        if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or not scoped
                or path.resolve() != path.absolute()
                or not path.parent.is_dir()):
            raise GitError("git_destination_denied", "目标必须位于已允许的创建目录内，父目录已存在且路径不经过链接。")
        return path

    def _url(self, value):
        if not isinstance(value, str) or not value or value.startswith("-") or any(ord(c) < 32 for c in value):
            raise GitError("git_remote_denied", "远程地址无效。")
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
            return value
        if re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", value):
            return value
        if parsed.scheme == "ssh" and parsed.hostname and not parsed.password and not parsed.query and not parsed.fragment:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", parsed.hostname) and re.fullmatch(r"/[A-Za-z0-9_./-]+", parsed.path):
                return value
        path = Path(value)
        if path.is_absolute() and (path in self.repositories or any(path.is_relative_to(root) for root in self.workspace_roots)):
            if path.exists() and path.resolve() == path.absolute():
                return str(path)
        raise GitError("git_remote_denied", "仅支持明确的 HTTPS/SSH 地址或允许范围内的本地仓库，不接受内嵌凭据。")

    def _ref(self, context, value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/~-]{0,180}", value) or ".." in value:
            raise GitError("git_invalid_ref", "请提供明确的分支、提交或 HEAD~n，不接受选项或范围表达式。")
        return self._query(context, ["rev-parse", "--verify", "--end-of-options", value + "^{commit}"]).decode().strip()

    def _remote_name(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,63}", value):
            raise GitError("git_remote_denied", "远程名称无效。")
        return value

    def _common(self, context):
        return Path(self._query(context, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).decode().strip())

    def _progress(self, context):
        metadata = self._git_dir(context)
        if any((metadata / name).exists() for name in ("rebase-merge", "rebase-apply")):
            return "rebase"
        for name, operation in (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert")):
            if (metadata / name).exists():
                return operation
        if (metadata / "sequencer").exists():
            text = (metadata / "sequencer/todo").read_text() if (metadata / "sequencer/todo").exists() else ""
            return "revert" if text.startswith("revert ") else "cherry-pick"
        return None

    def _state(self, context):
        metadata, common = self._git_dir(context), self._common(context)
        status = self._query(context, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"])
        progress = self._progress(context)
        unmerged = self._query(context, ["ls-files", "--unmerged"])
        if any((p / name).exists() for p in {metadata, common} for name in ("index.lock", "HEAD.lock", "config.lock", "packed-refs.lock")):
            raise GitError("git_locked", "仓库有进行中的 Git 写操作，未执行。")
        if (progress or unmerged) and not getattr(self.flags, "progress", False):
            raise GitError("git_integration_in_progress", "存在冲突或进行中的整合，请使用冲突查询、暂存、继续或中止工具。")
        files = set(self._query(context, ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z"]).split(b"\0"))
        files.update(self._query(context, ["ls-files", "--others", "--exclude-standard", "-z"]).split(b"\0"))
        contents, total = [], 0
        for raw in sorted(files):
            if not raw:
                continue
            path = context[0] / raw.decode("utf-8")
            if path.is_file() and path.resolve() == path.absolute():
                total += path.stat().st_size
                if total > 16 * 1024 * 1024:
                    raise GitError("git_change_too_large", "未提交修改超过快照容量，请缩小任务。")
                contents.append((raw.decode(), digest(path.read_bytes())))
            else:
                contents.append((raw.decode(), "missing-or-link"))
        index = metadata / "index"
        if index.exists() and (index.resolve() != index.absolute() or index.stat().st_size > 2 * 1024 * 1024):
            raise GitError("git_index_denied", "索引路径或大小无效。")
        index_bytes = index.read_bytes() if index.exists() else b""
        progress_files = []
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
            path = metadata / name
            paths = path.rglob("*") if path.is_dir() else [path]
            for item in paths:
                if item.exists() and item.is_file():
                    if item.resolve() != item.absolute() or item.stat().st_size > 2 * 1024 * 1024:
                        raise GitError("git_metadata_denied", "整合状态路径或大小无效。")
                    progress_files.append((str(item), digest(item.read_bytes())))
        head = self._head(context)
        branch = self._query(context, ["symbolic-ref", "--quiet", "--short", "HEAD"], optional=True).decode().strip()
        values = {"head": head, "branch": branch, "status": digest(status), "index": digest(index_bytes),
                  "config": digest((common / "config").read_bytes()), "files": contents, "progress": progress_files,
                  "refs": self._query(context, ["for-each-ref", "--format=%(refname) %(objectname)"]).decode(),
                  "worktrees": self._query(context, ["worktree", "list", "--porcelain"]).decode(), "settings": context[2]}
        return digest(json.dumps(values, sort_keys=True).encode()), status, index_bytes

    def _destination_state(self, target):
        if not target.exists():
            return "missing"
        if not target.is_dir() or (target / ".git").exists():
            raise GitError("git_destination_exists", "目标不是可初始化目录，或已经是仓库。")
        files, total = [], 0
        for path in target.rglob("*"):
            if path.resolve() != path.absolute():
                raise GitError("git_destination_denied", "目标内存在链接。")
            if path.is_file():
                total += path.stat().st_size
                if total > 16 * 1024 * 1024 or len(files) > 10000:
                    raise GitError("git_preview_too_large", "目标目录过大。")
                files.append((str(path.relative_to(target)), digest(path.read_bytes())))
        return digest(json.dumps(sorted(files)).encode())

    def _plan(self, name, repository, arguments, snapshot, details):
        description = (details["description"] if name == "git_publish" else
                       json.dumps({"操作": name, "仓库": repository, "完整参数": arguments, "当前状态及影响": details}, ensure_ascii=False, indent=2))
        if len(description) > 24000:
            raise GitError("git_preview_too_large", "完整预览超出限制，请缩小操作。")
        self.plans = {key: value for key, value in self.plans.items() if value["expires"] > time.monotonic()}
        if len(self.plans) >= 30:
            raise GitError("git_plan_limit", "本轮预览数量已达上限。")
        token = secrets.token_urlsafe(32)
        self.plans[token] = dict(name=name, repository=repository, arguments=arguments, snapshot=snapshot,
                                 details=details, expires=time.monotonic() + 600, extended=True)
        return {"prepared": True, "token": token, "description": description}

    def prepare(self, name, repository, **arguments):
        self.flags.progress = name in PROGRESS
        if name not in EXTRA_WRITES | BOOTSTRAP_TOOLS:
            try:
                return super().prepare(name, repository, **arguments)
            finally:
                self.flags.progress = False
        try:
            if not self.allow_writes and not (name == "git_clone" and self.allow_clone):
                raise GitError("git_write_disabled", "尚未启用 Git 写操作申请。")
            if name in BOOTSTRAP_TOOLS:
                target = self._allow_destination(repository, clone=name == "git_clone")
                if name == "git_clone" and target.exists():
                    raise GitError("git_destination_exists", "克隆目标必须不存在，不覆盖已有目录。")
                snapshot = self._destination_state(target)
                details = {"destination": str(target), "existing": snapshot != "missing"}
                if name == "git_clone":
                    details["url"] = self._url(arguments["url"])
                    if arguments.get("branch"):
                        branch_name(arguments["branch"])
                    depth = arguments.get("depth", 0)
                    if type(depth) is not int or not 0 <= depth <= 100000:
                        raise GitError("git_invalid_arguments", "depth 必须为 0–100000，0 表示完整克隆。")
                else:
                    branch_name(arguments.get("branch", "main"))
                return self._plan(name, str(target), arguments, snapshot, details)
            context = self._context(repository)
            with self.locks[str(context[0])]:
                snapshot, status, _ = self._state(context)
                details = self._prepare_extra(context, name, arguments, status)
                return self._plan(name, repository, arguments, snapshot, details)
        except (GitError, ValueError, OSError, KeyError, TypeError, UnicodeError) as error:
            return self._error(error)
        finally:
            self.flags.progress = False

    def _prepare_extra(self, context, name, args, status):
        if name == "git_publish":
            return self.prepare_publish(context, args)
        root = context[0]
        details = {"head": self._head(context), "status": status.decode(errors="replace").replace("\0", "\n")}
        if name in {"git_remote_set", "git_remote_remove"}:
            remote = self._remote_name(args.get("remote", "origin"))
            existing = self._query(context, ["remote"]).decode().splitlines()
            details.update(remote=remote, existing=remote in existing)
            if name == "git_remote_set":
                details["url"] = self._url(args["url"])
                if args.get("push", False) and remote not in existing:
                    raise GitError("git_remote_denied", "设置推送地址前必须先创建该远程。")
            elif remote not in existing:
                raise GitError("git_remote_denied", "远程不存在。")
            details["before"] = self.read("git_remotes", str(root)).get("content")
        elif name == "git_config_set":
            key, value = args["key"], args.get("value")
            if key not in CONFIG_KEYS:
                raise GitError("git_config_denied", "此工具只修改仓库级身份、换行及 pull 策略，不修改执行命令或凭据配置。")
            enums = {"core.autocrlf": {"true", "false", "input"}, "core.eol": {"lf", "crlf", "native"},
                     "core.safecrlf": {"true", "false", "warn"}, "pull.ff": {"true", "false", "only"},
                     "pull.rebase": {"true", "false", "merges"}}
            if not args.get("remove", False) and (not isinstance(value, str) or not value.strip() or len(value) > 200
                    or any(ord(c) < 32 for c in value) or (key in enums and value not in enums[key])):
                raise GitError("git_invalid_arguments", "配置值无效。")
            details["before"] = self._query(context, ["config", "--local", "--get-all", key], optional=True).decode()
        elif name in {"git_set_upstream", "git_checkout_remote"}:
            remote = self._remote_name(args.get("remote", "origin"))
            remote_branch = branch_name(args["remote_branch"])
            local = branch_name(args.get("branch", self._branch(context)))
            details.update(remote=remote, remote_branch=remote_branch, branch=local,
                           target=self._ref(context, "refs/remotes/" + remote + "/" + remote_branch))
            self._ref(context, "refs/heads/" + local) if name == "git_set_upstream" else self._clean(context, status)
        else:
            return self.prepare_workflow(context, name, args, status)
        return details

    def _clean(self, context, status=None):
        if status is None:
            status = self._query(context, ["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=all"])
        if status:
            raise GitError("git_dirty_worktree", "此操作要求干净工作区；请先保存或暂存当前修改，不会自动丢弃。")

    def apply(self, token):
        plan = self.plans.get(token)
        if not plan or not plan.get("extended"):
            self.flags.progress = bool(plan and plan["name"] in PROGRESS)
            try:
                return super().apply(token)
            finally:
                self.flags.progress = False
        self.plans.pop(token)
        self.flags.progress = plan["name"] in PROGRESS
        try:
            if plan["expires"] <= time.monotonic():
                raise GitError("git_plan_expired", "预览已过期。")
            if plan["name"] in BOOTSTRAP_TOOLS:
                with self.lifecycle_lock:
                    target = self._allow_destination(plan["repository"], clone=plan["name"] == "git_clone")
                    if self._destination_state(target) != plan["snapshot"]:
                        raise GitError("git_state_changed", "目标目录在确认期间已变化。")
                    return self._apply_bootstrap(plan, target)
            context = self._context(plan["repository"])
            with self.locks[str(context[0])]:
                if self._state(context)[0] != plan["snapshot"]:
                    raise GitError("git_state_changed", "仓库在确认期间已变化，请重新核对。")
                return self._apply_extra(context, plan)
        except (GitError, OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
            return self._error(error)
        finally:
            self.flags.progress = False

    def _apply_bootstrap(self, plan, target):
        env = {key: value for key, value in os.environ.items() if key.upper() in
               {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"}}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="0")
        context = (target.parent, env, [self.git, "-c", "core.hooksPath=" + os.devnull, "-c", "maintenance.auto=false"])
        args = plan["arguments"]
        if plan["name"] == "git_clone":
            self._url(plan["details"]["url"])
            network = self._network_context(context, plan["details"]["url"])
            self._query(network, ["clone", "--no-hardlinks", "--no-recurse-submodules",
                *(["--depth", str(args["depth"])] if args.get("depth") else []),
                *(["--branch", args["branch"]] if args.get("branch") else []), "--", plan["details"]["url"], str(target)], network=True, timeout=180)
        else:
            self._query(context, ["init", "--initial-branch=" + args.get("branch", "main"), "--", str(target)])
        self.repositories = (*self.repositories, target)
        self.locks[str(target)] = threading.RLock()
        context = self._context(str(target))
        result = {"success": True, "operation": plan["name"], "repository": str(target), "created_repository": str(target),
                "head": self._head(context), "content": self.read("git_status", str(target)).get("content", "")}
        if plan["name"] == "git_clone" and result["head"]:
            try:
                result["security_review"] = self.security.hooks.run("after_clone", self.security.tree_files(context, result["head"]))
            except ValueError:
                result["security_review"] = {"event": "after_clone", "complete": False,
                    "message": "项目已克隆，但当前版本安全检查未完整完成；未自动删除或修改文件。"}
        return result

    def _apply_extra(self, context, plan):
        if plan["name"] == "git_publish":
            return self.apply_publish(context, plan)
        name, args, details = plan["name"], plan["arguments"], plan["details"]
        if name == "git_remote_set":
            if args.get("push", False):
                command = ["remote", "set-url", "--push", details["remote"], details["url"]]
            else:
                command = (["remote", "set-url", details["remote"], details["url"]] if details["existing"] else
                           ["remote", "add", details["remote"], details["url"]])
            self._query(context, command)
            assert self._remote(context, details["remote"], args.get("push", False)) == details["url"]
        elif name == "git_remote_remove":
            self._query(context, ["remote", "remove", details["remote"]])
        elif name == "git_config_set":
            if args.get("remove", False):
                self._query(context, ["config", "--local", "--unset-all", args["key"]], optional=True)
            else:
                self._query(context, ["config", "--local", "--replace-all", args["key"], args["value"]])
                assert self._query(context, ["config", "--local", "--get", args["key"]]).decode().strip() == args["value"]
        elif name == "git_set_upstream":
            self._query(context, ["branch", "--set-upstream-to=" + details["remote"] + "/" + details["remote_branch"], details["branch"]])
        elif name == "git_checkout_remote":
            self._query(context, ["switch", "--track", "-c", details["branch"], details["remote"] + "/" + details["remote_branch"]])
        else:
            return self.apply_workflow(context, plan)
        return {"success": True, "operation": name, "repository": str(context[0]),
                "content": self.read("git_status", str(context[0])).get("content", "")}

    def read(self, name, repository, **args):
        if name not in EXTRA_READS:
            return super().read(name, repository, **args)
        try:
            context = self._context(repository)
            if name == "git_show":
                ref = self._ref(context, args.get("ref", "HEAD"))
                path = args.get("path")
                if path:
                    self._files(context[0], [path])
                    content = self._query(context, ["show", ref + ":" + path]).decode(errors="replace")
                else:
                    content = self._query(context, ["show", "--no-ext-diff", "--no-textconv", "--format=fuller", ref, "--", ".", *self._excludes()]).decode(errors="replace")
            elif name == "git_diff_refs":
                base, head = self._ref(context, args["base"]), self._ref(context, args.get("head", "HEAD"))
                content = self._query(context, ["diff", "--no-ext-diff", "--no-textconv", base, head, "--", ".", *self._excludes()]).decode(errors="replace")
            elif name == "git_config_read":
                items = []
                for key in sorted(CONFIG_KEYS):
                    for scope in ("local", "global"):
                        env = dict(context[1])
                        if scope == "global":
                            env.pop("GIT_CONFIG_GLOBAL", None)
                        code, value = self._invoke((context[0], env, context[2]), ["config", "--" + scope, "--no-includes", "--get", key], optional=True)
                        if code == 0:
                            items.append({"key": key, "value": value.decode().strip(), "scope": scope})
                content = json.dumps(items, ensure_ascii=False)
            elif name == "git_worktree_list":
                content = self._query(context, ["worktree", "list", "--porcelain"]).decode()
            elif name == "git_stash_list":
                content = self._query(context, ["stash", "list", "--format=%H %gd %s"]).decode()
            else:
                content = json.dumps({"operation": self._progress(context), "unmerged": self._query(context, ["ls-files", "--unmerged"]).decode(),
                    "files": self._query(context, ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "--diff-filter=U"]).decode()}, ensure_ascii=False)
            return {"repository": repository, "content": content[:24000] or "（无结果）", "truncated": len(content) > 24000, "untrusted": True}
        except (GitError, OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
            return self._error(error)

    @staticmethod
    def _excludes():
        return [":(exclude,icase)" + p for p in (".env*", "**/.env*", "*.pem", "**/*.pem", "*.key", "**/*.key",
                "credentials*", "**/credentials*", "secrets*", "**/secrets*", ".local/**", "**/.local/**",
                ".aws/**", "**/.aws/**", ".ssh/**", "**/.ssh/**")]
