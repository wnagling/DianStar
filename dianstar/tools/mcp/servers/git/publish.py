"""One approved, bounded stage/commit/ordinary-push plan."""
from pathlib import Path
import tempfile

from .operations import GitError, branch_name, digest
from dianstar.security.content_guard import MAX_FILE, MAX_TOTAL, SecurityViolation


class PublishMixin:
    def prepare_publish(self, context, args):
        if set(args) - {"files", "message", "branch", "remote", "set_upstream"}:
            raise GitError("git_invalid_arguments", "发布计划不接受强推、删除或额外操作。")
        paths = self._files(context[0], args.get("files"))
        message = args.get("message")
        if (not isinstance(message, str) or not message.strip() or len(message) > 4000
                or any(ord(char) < 32 and char not in "\n\t" for char in message)):
            raise GitError("git_invalid_message", "提交说明不能为空且最多 4000 字符。")
        if type(args.get("set_upstream", False)) is not bool:
            raise GitError("git_invalid_arguments", "set_upstream 必须为布尔值。")
        staged = self._query(context, ["diff", "--cached", "--name-only", "-z"]).split(b"\0")
        omitted = [p.decode() for p in staged if p and p.decode() not in args["files"]]
        if omitted:
            raise GitError("git_publish_scope_mismatch", "暂存区还有未包含在发布计划中的文件，请核对范围；暂存内容未改变。")
        remote = self._remote_name(args.get("remote", "origin"))
        target = branch_name(args.get("branch"))
        url = self._remote(context, remote, True)
        if args.get("set_upstream", False) and self._remote(context, remote) != url:
            raise GitError("git_remote_denied", "获取与推送地址不同，不能自动设置上游。")
        head = self._head(context)
        remote_head = self.security.remote_head(context, url, target)
        if remote_head and (not head or self._invoke(context,
                ["merge-base", "--is-ancestor", remote_head, head], optional=True)[0]):
            raise GitError("git_publish_not_fast_forward", "远程分支不能直接快进，或缺少远程记录；请先检查分支关系。")
        pending = ""
        if head:
            self.security.push(context, head, remote_head)
            pending = self._query(context, ["log", "--format=%h %s", head,
                                           *(["^" + remote_head] if remote_head else [])]).decode(errors="replace")
        selected, total = [], 0
        for file in args["files"]:
            path = context[0] / file
            size = path.stat().st_size if path.is_file() else 0
            total += size
            if size > MAX_FILE or total > MAX_TOTAL:
                raise SecurityViolation(incomplete=True)
            data = path.read_bytes() if path.is_file() else b""
            self.security.hooks.run("before_stage", [(file, data)])
            selected.append([file, digest(data), path.exists()])
        _, _, index_bytes = self._state(context)
        # Preview the final index without changing the user's staging area.
        with tempfile.TemporaryDirectory(prefix="dianstar-publish-", dir=self._git_dir(context)) as temporary:
            index = Path(temporary) / "index"
            private = (context[0], {**context[1], "GIT_INDEX_FILE": str(index)}, context[2])
            if index_bytes:
                index.write_bytes(index_bytes)
            else:
                self._query(private, ["read-tree", "--empty"])
            self._query(private, ["add", "--", *paths])
            self.security.index(private)
            self.security.hooks.run("before_commit", [("[提交说明]", message.encode())])
            changed = self._query(private, ["diff", "--cached", "--name-only", "-z"])
            if not changed:
                raise GitError("git_commit_denied", "所选文件没有可提交的改动；已有提交可直接申请普通推送。")
            tree = self._query(private, ["write-tree"]).decode().strip()
            final_index = index.read_bytes()
            stats = self._query(private, ["diff", "--cached", "--stat", "--no-ext-diff", "--no-textconv"]).decode(errors="replace")
        identity = self._identity(context)
        description = self.security.hooks.scanner.redact("\n".join([
            "是否提交并推送这些项目改动？", "项目位置：" + str(context[0]),
            "当前分支：" + self._branch(context), "目标：" + url + " → " + target,
            "作者：" + identity[0] + " <" + identity[1] + ">",
            "提交说明：" + message, "文件：\n" + "\n".join(args["files"]), stats,
            "将一并推送的已有本地提交：\n" + (pending or "无"),
            *(["推送成功后将当前分支关联到这个远程分支。"] if args.get("set_upstream", False) else []),
            "一次确认后，按这份文件范围完成暂存、提交、普通推送，并核对远程结果。",
            "未选文件保持原样；推送失败时保留已创建的本地提交并报告进度。",
        ]))
        return dict(head=head, branch=self._branch(context), identity=identity, index=final_index,
                    tree=tree, paths=paths, selected=selected, url=url, remote=remote, target=target,
                    security_remote_head=remote_head, description=description)

    def apply_publish(self, context, plan):
        args, details = plan["arguments"], plan["details"]
        completed, commit, phase = [], None, "preflight"
        try:
            if self.security.remote_head(context, details["url"], details["target"]) != details["security_remote_head"]:
                raise GitError("git_state_changed", "远程分支已变化，发布尚未开始，请重新检查。")
            if self._identity(context) != details["identity"]:
                raise GitError("git_state_changed", "提交身份已变化，未执行。")
            phase = "stage"
            self._apply(context, dict(name="git_stage", arguments={"files": args["files"]}, details=details))
            completed.append("stage")
            self.security.index(context)
            if self._query(context, ["write-tree"]).decode().strip() != details["tree"]:
                raise GitError("git_state_changed", "实际暂存内容与批准计划不同，未提交。")
            phase = "commit"
            result = self._apply(context, dict(name="git_commit", arguments={"message": args["message"]}, details=details))
            commit = result["commit"]
            completed.append("commit")
            push_details = {**details, "head": commit}
            phase = "push"
            result = self._apply(context, dict(name="git_push", arguments={"set_upstream": args.get("set_upstream", False)}, details=push_details))
            completed.append("push")
            return {**result, "operation": "git_publish", "completed_steps": completed}
        except (GitError, ValueError, OSError, UnicodeError) as error:
            return {**self._error(error), "operation": "git_publish", "completed_steps": completed,
                    "commit": commit, "remote_verified": False,
                    "failed_step": phase, "outcome_unknown": phase in {"commit", "push"},
                    "remote_url": self.security.hooks.scanner.redact(details["url"]),
                    "remote": details["remote"], "branch": details["target"], "repository": str(context[0]),
                    "recovery": "先检查本地与远程状态；已完成步骤保留，不自动重试或回滚。"}
