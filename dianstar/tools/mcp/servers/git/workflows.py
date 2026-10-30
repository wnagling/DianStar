"""Updates, selective recovery, worktrees and interruptible integration flows."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .operations import GitError, branch_name, sensitive


class WorkflowMixin:
    def prepare_workflow(self, context, name, args, status):
        details = {"head": self._head(context), "status": status.decode(errors="replace").replace("\0", "\n")}
        if name == "git_remote_branch_delete":
            branch = branch_name(args["branch"])
            remote = self._remote_name(args.get("remote", "origin"))
            url = self._remote(context, remote, True)
            advertised = self._query(self._network_context(context, url), ["ls-remote", "--heads", "--", url, "refs/heads/" + branch], network=True).decode().split()
            if not advertised:
                raise GitError("git_branch_unavailable", "远程目标分支不存在。")
            details.update(branch=branch, remote=remote, url=url, target=advertised[0], effect="删除这一远程分支；不删除其他分支及本地工作文件。")
        elif name == "git_pull":
            self._clean(context, status)
            remote = self._remote_name(args.get("remote", "origin"))
            branch = branch_name(args["branch"])
            url = self._remote(context, remote)
            advertised = self._query(self._network_context(context, url), ["ls-remote", "--heads", "--", url, "refs/heads/" + branch], network=True).decode().split()
            if not advertised:
                raise GitError("git_branch_unavailable", "远程分支不存在。")
            details.update(remote=remote, branch=branch, url=url, target=advertised[0], policy="仅快进；分叉时停止，获取记录可能已更新")
        elif name == "git_restore":
            details["paths"] = self._files(context[0], args["files"])
            details["source"] = self._ref(context, args.get("source", "HEAD"))
            details["before"] = self._query(context, ["diff", "--no-ext-diff", "--no-textconv", "--", *details["paths"]]).decode(errors="replace")
            details["source_files"] = {path: self._query(context, ["show", details["source"] + ":" + path], optional=True).decode(errors="replace") for path in args["files"]}
            details["effect"] = "只恢复明确选择的工作区文件；其他文件及暂存区保持不变。"
        elif name in {"git_merge", "git_rebase", "git_cherry_pick", "git_revert"}:
            self._clean(context, status)
            details["target"] = self._ref(context, args["ref"])
            details["identity"] = self._identity(context)
            details["changes"] = self.read("git_show", str(context[0]), ref=details["target"]).get("content")
            if name == "git_rebase":
                details["effect"] = "重放当前分支提交，改变本地提交 ID；不会自动推送。"
        elif name in {"git_conflict_continue", "git_conflict_abort"}:
            details["integration"] = self._progress(context)
            if not details["integration"]:
                raise GitError("git_no_integration", "没有可继续或中止的整合。")
            details["conflicts"] = self.read("git_conflicts", str(context[0])).get("content")
            if name == "git_conflict_continue":
                if self._query(context, ["ls-files", "--unmerged"]):
                    raise GitError("git_unresolved_conflicts", "冲突文件尚未解决并暂存。")
                details["identity"] = self._identity(context)
                details["staged"] = self._query(context, ["diff", "--cached", "--no-ext-diff", "--no-textconv"]).decode(errors="replace")
            else:
                details["effect"] = "中止当前整合；整合过程中手动编辑的冲突文件将恢复到开始整合前状态。"
        elif name in {"git_stash_save", "git_checkpoint"}:
            message = args.get("message", "DianStar checkpoint")
            if not isinstance(message, str) or not message.strip() or len(message) > 500 or any(ord(c) < 32 for c in message):
                raise GitError("git_invalid_message", "说明必须为 1–500 字符的单行文本。")
            changed = self._query(context, ["diff", "--name-only", "-z"]).split(b"\0")
            changed += self._query(context, ["diff", "--cached", "--name-only", "-z"]).split(b"\0")
            changed += self._query(context, ["ls-files", "--others", "--exclude-standard", "-z"]).split(b"\0")
            names = sorted({x.decode() for x in changed if x})
            if name == "git_stash_save" and any(sensitive(p) for p in names):
                raise GitError("git_sensitive_changes", "待保存修改包含敏感路径，未将其写入 stash。")
            details["files"] = names
            details["changes"] = self._query(context, ["diff", "HEAD", "--no-ext-diff", "--no-textconv", "--", ".", *self._excludes()], optional=True).decode(errors="replace")
            details["untracked"] = {p: (context[0] / p).read_text(encoding="utf-8", errors="replace") for p in names
                if not sensitive(p) and (context[0] / p).is_file() and self._invoke(context, ["ls-files", "--error-unmatch", "--", ":(literal)" + p], optional=True)[0] != 0}
            if name == "git_checkpoint":
                details["identity"] = self._identity(context)
                details["effect"] = "保存受支持文件的工作区快照到 refs/dianstar/checkpoints，不移动 HEAD、不清空修改；敏感及忽略文件不纳入快照。"
        elif name in {"git_stash_apply", "git_stash_drop"}:
            entries = self._query(context, ["stash", "list", "--format=%H %gd"]).decode().splitlines()
            requested = args.get("stash", "stash@{0}")
            match = next((line.split() for line in entries if requested in line.split()), None)
            if not match:
                raise GitError("git_stash_missing", "指定 stash 不存在。")
            details.update(stash_sha=match[0], stash_ref=match[1])
            details["changes"] = self._query(context, ["stash", "show", "--include-untracked", "--patch", "--no-ext-diff", "--no-textconv", match[0]]).decode(errors="replace")
            if name == "git_stash_apply":
                self._clean(context, status)
        elif name == "git_worktree_add":
            target = self._allow_destination(args["path"])
            if target.exists():
                raise GitError("git_destination_exists", "worktree 目标必须不存在。")
            branch = branch_name(args["branch"])
            target_sha = self._ref(context, args.get("start_point", "HEAD"))
            details.update(path=str(target), branch=branch, target=target_sha, destination="missing")
            if not args.get("create_branch", True):
                details["target"] = self._ref(context, "refs/heads/" + branch)
        elif name == "git_worktree_remove":
            target = self._allow_destination(args["path"])
            if target == context[0] or target not in self.repositories or not (target / ".git").is_file():
                raise GitError("git_worktree_denied", "只能移除已接入的关联 worktree，不能移除主仓库。")
            child = self._context(str(target))
            if self._common(child) != self._common(context):
                raise GitError("git_worktree_denied", "目标属于其他主仓库。")
            if self._query(child, ["status", "--porcelain", "--ignored", "--untracked-files=all"]):
                raise GitError("git_dirty_worktree", "worktree 含修改、未跟踪或忽略文件，未删除。")
            details.update(path=str(target), child_state=self._state(child)[0])
        elif name in {"git_branch_rename", "git_branch_delete"}:
            branch = branch_name(args["branch"])
            details.update(branch=branch, target=self._ref(context, "refs/heads/" + branch))
            if name == "git_branch_rename":
                details["new_branch"] = branch_name(args["new_branch"])
            else:
                details["force"] = args.get("force", False)
                if branch == self._branch(context):
                    raise GitError("git_branch_denied", "不能删除当前分支。")
                details["commits"] = self._query(context, ["log", "--oneline", "HEAD.." + details["target"]]).decode()
        else:
            raise GitError("git_tool_denied", "未开放此工程操作。")
        return details

    def _integration_context(self, context, identity=None):
        env = {**context[1], "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"}
        prefix = context[2] + ["-c", "merge.verifySignatures=false", "-c", "rerere.enabled=false", "-c", "rebase.autoStash=false"]
        if identity:
            prefix += ["-c", "user.name=" + identity[0], "-c", "user.email=" + identity[1]]
        return context[0], env, prefix

    def _integration_result(self, context, name, code):
        operation = self._progress(context)
        conflicts = self._query(context, ["ls-files", "--unmerged"]).decode()
        if code and not operation and not conflicts:
            raise GitError("git_command_failed", "Git 操作未完成，请查询状态；未自动重试或回滚。")
        return {"success": code == 0, "operation": name, "repository": str(context[0]), "head": self._head(context),
            "state": "conflicts" if conflicts else "in_progress" if operation else "completed", "integration": operation,
            "content": self.read("git_status", str(context[0])).get("content", ""), "conflicts": conflicts,
            "message": "冲突已保留；请编辑并暂存后继续，或明确中止。" if code else "操作完成。"}

    def apply_workflow(self, context, plan):
        name, args, details = plan["name"], plan["arguments"], plan["details"]
        if name == "git_remote_branch_delete":
            network = self._network_context(context, details["url"])
            ref = "refs/heads/" + details["branch"]
            actual = self._query(network, ["ls-remote", "--heads", "--", details["url"], ref], network=True).decode().split()
            if not actual or actual[0] != details["target"]:
                raise GitError("git_remote_state_changed", "远程分支在确认期间已变化，未删除。")
            self._query(network, ["push", "--porcelain", "--no-verify", "--force-with-lease=" + ref + ":" + details["target"], "--", details["url"], ":" + ref], network=True)
            if self._query(network, ["ls-remote", "--heads", "--", details["url"], ref], network=True).strip():
                raise GitError("git_outcome_unknown", "删除远程分支后未核验一致，请先回读。")
        elif name == "git_pull":
            network = self._network_context(context, details["url"])
            self._query(network, ["fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance", "--", details["url"],
                "refs/heads/" + details["branch"] + ":refs/remotes/" + details["remote"] + "/" + details["branch"]], network=True)
            actual = self._ref(context, "refs/remotes/" + details["remote"] + "/" + details["branch"])
            if actual != details["target"]:
                raise GitError("git_remote_state_changed", "远程分支在确认期间已更新；已获取但未更新工作区，请重新核对。")
            self._clean(context)
            self._query(self._integration_context(context), ["merge", "--ff-only", "--no-edit", "--", actual])
        elif name == "git_restore":
            self._files(context[0], args["files"])
            self._query(context, ["restore", "--worktree", "--source=" + details["source"], "--", *details["paths"]])
        elif name in {"git_merge", "git_rebase", "git_cherry_pick", "git_revert"}:
            if self._identity(context) != details["identity"]:
                raise GitError("git_state_changed", "提交身份已变化。")
            operation = name.removeprefix("git_").replace("_", "-")
            options = ["--no-edit"] if operation in {"merge", "revert"} else []
            code, _ = self._invoke(self._integration_context(context, details["identity"]), [operation, *options, details["target"]], optional=True)
            return self._integration_result(context, name, code)
        elif name in {"git_conflict_continue", "git_conflict_abort"}:
            command = [details["integration"], "--continue" if name.endswith("continue") else "--abort"]
            code, _ = self._invoke(self._integration_context(context, details.get("identity")), command, optional=True)
            return self._integration_result(context, name, code)
        elif name == "git_stash_save":
            self._query(self._integration_context(context, self._identity(context)), ["stash", "push", "--include-untracked", "-m", args.get("message", "DianStar stash")])
        elif name in {"git_stash_apply", "git_stash_drop"}:
            command = ["stash", "apply", "--index", details["stash_sha"]] if name.endswith("apply") else ["stash", "drop", details["stash_ref"]]
            code, _ = self._invoke(context, command, optional=True)
            if name.endswith("apply"):
                return self._integration_result(context, name, code)
            if code:
                raise GitError("git_command_failed", "删除 stash 失败，请回读核对。")
        elif name == "git_checkpoint":
            return self._checkpoint(context, plan)
        elif name == "git_worktree_add":
            target = self._allow_destination(details["path"])
            if target.exists():
                raise GitError("git_state_changed", "worktree 目标已出现，未覆盖。")
            command = ["worktree", "add", *(["-b", details["branch"]] if args.get("create_branch", True) else []),
                       "--", str(target), details["target"] if args.get("create_branch", True) else details["branch"]]
            self._query(context, command)
            self.repositories = (*self.repositories, target)
            self.locks[str(target)] = self.locks[str(context[0])]
            self._adopt_worktree(target)
            return {"success": True, "operation": name, "repository": str(context[0]), "created_repository": str(target), "content": str(target)}
        elif name == "git_worktree_remove":
            target = self._allow_destination(details["path"])
            child = self._context(str(target))
            if self._state(child)[0] != details["child_state"] or self._query(child, ["status", "--porcelain", "--ignored", "--untracked-files=all"]):
                raise GitError("git_state_changed", "worktree 在确认后已变化。")
            self._query(context, ["worktree", "remove", "--", str(target)])
            self.repositories = tuple(root for root in self.repositories if root != target)
            self.managed_metadata.pop(str(target), None)
            return {"success": True, "operation": name, "repository": str(context[0]), "removed_repository": str(target), "content": "已移除干净 worktree。"}
        elif name == "git_branch_rename":
            self._query(context, ["branch", "--move", details["branch"], details["new_branch"]])
        elif name == "git_branch_delete":
            self._query(context, ["branch", "--delete", *( ["--force"] if details["force"] else []), "--", details["branch"]])
        else:
            raise GitError("git_tool_denied", "未开放此工程操作。")
        return {"success": True, "operation": name, "repository": str(context[0]), "head": self._head(context),
                "content": self.read("git_status", str(context[0])).get("content", "")}

    def _checkpoint(self, context, plan):
        # Build a private tree directly, without clearing/stashing the user's files.
        identity = plan["details"]["identity"]
        with tempfile.TemporaryDirectory(prefix="dianstar-checkpoint-", dir=self._git_dir(context)) as temp:
            env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
            self._query(context, ["read-tree", "--empty"], env_extra=env)
            tracked = self._query(context, ["ls-files", "--stage", "-z"]).split(b"\0")
            modes = {}
            for entry in tracked:
                if entry:
                    info, path = entry.split(b"\t", 1)
                    modes[path.decode()] = info.split()[0].decode()
            names = set(modes) | {p.decode() for p in self._query(context, ["ls-files", "--others", "--exclude-standard", "-z"]).split(b"\0") if p}
            excluded = []
            for name in sorted(names):
                path = context[0] / name
                if sensitive(name):
                    excluded.append(name); continue
                if not path.exists():
                    continue
                self._files(context[0], [name])
                mode = modes.get(name, "100644")
                if mode not in {"100644", "100755"}:
                    raise GitError("git_checkpoint_unsupported", "快照暂不包含符号链接或子模块。")
                blob = self._query(context, ["hash-object", "-w", "--no-filters", "--", str(path)]).decode().strip()
                self._query(context, ["update-index", "--add", "--cacheinfo", mode + "," + blob + "," + name], env_extra=env)
            tree = self._query(context, ["write-tree"], env_extra=env).decode().strip()
            head = self._head(context)
            commit = self._query(self._integration_context(context, identity), ["commit-tree", tree, *(["-p", head] if head else []), "-m", plan["arguments"].get("message", "DianStar checkpoint")]).decode().strip()
            reference = "refs/dianstar/checkpoints/" + str(time.time_ns())
            self._query(context, ["update-ref", reference, commit])
        return {"success": True, "operation": "git_checkpoint", "repository": str(context[0]), "checkpoint": reference,
                "commit": commit, "excluded": excluded, "content": "已保存工作区快照，HEAD、暂存区及工作文件保持不变；可用 git_restore 指定 source 和 files 选择恢复。"}
