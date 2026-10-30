"""Deterministic user-facing intent from validated arguments, never model labels."""
from pathlib import PurePosixPath
import json
import shlex
from urllib.parse import urlsplit


GIT_ACTIONS = {
    "git_publish": "提交并推送项目改动",
    "git_init": "初始化项目", "git_stage": "暂存文件", "git_unstage": "取消文件暂存",
    "git_commit": "提交项目修改", "git_push": "推送项目提交", "git_fetch": "获取远程更新",
    "git_pull": "更新本地项目", "git_create_branch": "创建分支", "git_switch_branch": "切换分支",
    "git_checkout_remote": "检出远程分支", "git_remote_set": "设置远程仓库地址",
    "git_remote_remove": "移除远程仓库地址", "git_set_upstream": "设置分支跟踪关系",
    "git_config_set": "修改项目配置", "git_restore": "恢复文件内容", "git_revert": "撤销指定提交",
    "git_merge": "合并分支或提交", "git_rebase": "调整当前分支的提交基础",
    "git_cherry_pick": "应用指定提交", "git_conflict_continue": "继续处理已解决的合并操作",
    "git_conflict_abort": "中止当前合并操作", "git_stash_save": "临时保存当前修改",
    "git_stash_apply": "恢复临时保存的修改", "git_stash_drop": "删除临时保存的修改",
    "git_checkpoint": "保存项目恢复点", "git_worktree_add": "创建独立工作目录",
    "git_worktree_remove": "删除独立工作目录", "git_branch_rename": "重命名分支",
    "git_branch_delete": "删除本地分支", "git_remote_branch_delete": "删除远程分支",
}
GITHUB_ACTIONS = {
    "create_pull_request": "创建合并请求", "update_pull_request": "修改合并请求",
    "issue_write": "创建或修改议题", "add_issue_comment": "发布议题评论",
    "pull_request_review_write": "处理代码审查", "add_comment_to_pending_review": "添加代码审查评论",
    "add_reply_to_pull_request_comment": "回复代码审查评论",
    "github_repository_settings_update": "修改仓库设置", "github_branch_protection_update": "修改分支保护规则",
    "github_collaborators_set": "设置协作者权限", "github_collaborators_remove": "移除协作者",
}


def plain(value):
    return "".join(c if ord(c) >= 32 else " " for c in str(value))


SETTING_LABELS = {"git_enabled": "本地项目连接", "git_repositories": "可访问的本地项目",
    "github_enabled": "GitHub 连接", "github_executable": "GitHub 连接组件",
    "github_repositories": "可访问的 GitHub 项目", "git_write_enabled": "Git 修改操作",
    "workspace_write_enabled": "文件修改与命令", "github_private_repositories": "私有项目访问",
    "github_write_repositories": "GitHub 修改范围", "github_admin_repositories": "仓库管理范围",
    "git_workspace_roots": "新项目保存范围", "github_use_git_credentials": "已有 Git 登录"}


def readable(value):
    if value is None:
        return "自动处理"
    if value is True:
        return "可用"
    if value is False:
        return "停用"
    if isinstance(value, list):
        return "、".join(map(plain, value)) or "无"
    return plain(value) or "未设置"


def configuration_changes(description):
    try:
        payload = json.loads(description)
        before, after = payload["修改前"], payload["修改后"]
        if not isinstance(before, dict) or not isinstance(after, dict):
            return []
        changes = []
        for key in sorted(set(before) | set(after)):
            if before.get(key) == after.get(key):
                continue
            old, new = before.get(key), after.get(key)
            if key == "git_repositories" and isinstance(old, list) and isinstance(new, list):
                changes += ["新增可访问项目：" + plain(path) for path in new if path not in old]
                changes += ["移除可访问项目：" + plain(path) for path in old if path not in new]
            else:
                changes.append(f"{SETTING_LABELS.get(key, key)}：{readable(old)} → {readable(new)}")
        return changes
    except (ValueError, KeyError, TypeError):
        return []


def approval_details(description):
    changes = configuration_changes(description)
    if changes:
        return "本次只变更以下内容：\n\n" + "\n\n".join(changes) + "\n\n这一步不会提交或推送代码。"
    try:
        payload = json.loads(description)
    except (ValueError, TypeError):
        return description
    labels = {**SETTING_LABELS, "repository": "项目位置", "destination": "保存位置", "url": "项目来源",
              "branch": "分支", "depth": "历史深度", "existing": "目标已存在"}
    def rows(value, indent=0):
        prefix = "  " * indent
        if isinstance(value, dict):
            result = []
            for key, item in value.items():
                label = labels.get(key, key)
                result.append(prefix + str(label) + ("：" if isinstance(item, dict) else "：" + readable(item)))
                if isinstance(item, dict):
                    result.extend(rows(item, indent + 1))
            return result
        return [prefix + readable(value)]
    return "\n".join(rows(payload))


def approval_summary(name, args, description):
    operation = name.removeprefix("mcp_git__").removeprefix("mcp_github__")
    repository = plain(args.get("repository", ""))
    if operation == "git_publish":
        target = next((plain(line) for line in description.splitlines() if line.startswith("目标：")), "")
        files = args["files"]
        selection = "、".join(map(plain, files[:3])) + (f"，另有 {len(files) - 3} 个文件" if len(files) > 3 else "")
        return "\n\n".join(["是否提交并推送这批项目改动？", "项目位置：" + repository, target,
                            "提交说明：" + plain(args["message"]), f"文件范围：{len(files)} 个文件（{selection}）",
                            "确认一次，完成暂存、提交、普通推送和结果核验。完整文件范围、统计及一并推送的已有提交可查看详情。"])
    if name == "open_project":
        path = plain(args["path"])
        project = PurePosixPath(path.replace("\\", "/")).name
        related = []
        try:
            related = json.loads(description).get("关联主项目", [])
        except (ValueError, AttributeError):
            pass
        association = "\n关联主项目：" + "、".join(map(plain, related)) if related else ""
        return f"是否打开 {project} 项目？\n\n项目位置：{path}{association}\n\n记住这个项目，方便后续使用。\n本次不会提交或推送代码，后续修改仍需按策略确认。"
    if operation == "git_clone":
        url = plain(args["url"])
        project = PurePosixPath((urlsplit(url).path or url).replace("\\", "/")).name.removesuffix(".git")
        lines = [f"是否将 {project} 项目克隆到以下位置？", f"保存位置：{repository}", f"项目来源：{url}"]
        if args.get("branch"):
            lines.append("选择分支：" + plain(args["branch"]))
        depth = args.get("depth", 0)
        lines.append(f"下载最近 {depth} 层提交历史。" if depth else "下载完整提交历史。")
        lines.append("将创建新目录，不覆盖已有项目。")
        return lines[0] + "\n\n" + "\n".join(lines[1:])
    if operation in GIT_ACTIONS:
        lines = [f"是否{GIT_ACTIONS[operation]}？", "项目位置：" + repository]
        for key, label in (("files", "文件"), ("path", "目标位置"), ("branch", "分支"), ("new_branch", "新分支名称"),
                           ("remote", "远程名称"), ("remote_branch", "跟踪分支"), ("url", "远程地址"),
                           ("ref", "分支或提交"), ("source", "恢复来源"), ("start_point", "起点"), ("stash", "临时修改"),
                           ("key", "配置项"), ("value", "配置值"), ("message", "说明")):
            if key in args and args[key] != "":
                value = "、".join(map(plain, args[key])) if isinstance(args[key], list) else plain(args[key])
                lines.append(label + "：" + value)
        for line in description.splitlines():
            if line.startswith(("地址：", "暂存文件：", "当前分支：", "作者：")):
                lines.append(line)
        if args.get("force_with_lease"):
            lines.append("此操作会改写远程分支历史；仅在远程状态未变化时执行。")
        if args.get("force"):
            lines.append("将强制删除，可能丢失尚未合并的提交。")
        if operation in {"git_restore", "git_stash_apply", "git_revert"}:
            lines.append("这会改变当前文件内容，请确认恢复目标。")
        return "\n\n".join(lines)
    if name in {"workspace_edit", "workspace_delete", "workspace_run"}:
        if name == "workspace_run":
            return "\n\n".join(("是否在这个项目中运行命令？", "项目位置：" + repository,
                "运行内容：" + plain(shlex.join(args["argv"])), "命令以你的本机账户运行，可能修改文件或访问网络。"))
        verb = "删除" if name == "workspace_delete" else "创建" if args.get("mode") == "create" else "修改"
        local_note = "\n\n仅在本机保存运行配置，可能包含凭据；内容已遮挡，不能因此提交或推送。" if args.get("local_only") else ""
        return f"是否{verb}这个项目文件？\n\n项目位置：{repository}\n\n文件：{plain(args['path'])}" + local_note + (
            "\n\n删除后，未保存到版本记录的内容可能无法恢复。" if name == "workspace_delete" else "\n\n可展开下方详情查看具体修改。")
    if operation in GITHUB_ACTIONS:
        label = GITHUB_ACTIONS[operation]
        if operation == "issue_write":
            label = "创建议题" if args.get("method") == "create" else "修改议题"
        if operation == "pull_request_review_write":
            label = {"create": "创建代码审查", "submit_pending": "提交代码审查", "submit": "提交代码审查",
                     "delete_pending": "删除未提交的代码审查", "delete": "删除未提交的代码审查",
                     "resolve_thread": "将讨论标记为已解决", "unresolve_thread": "重新打开讨论"}.get(args.get("method"), label)
        lines = [f"是否在 GitHub 上{label}？", f"项目：{plain(args.get('owner', ''))}/{plain(args.get('repo', ''))}"]
        for key, title in (("title", "标题"), ("issue_number", "议题编号"), ("pullNumber", "合并请求编号"),
                           ("base", "合并目标分支"), ("head", "来源分支"), ("branch", "分支"),
                           ("username", "协作者"), ("permission", "权限级别")):
            if key in args:
                lines.append(title + "：" + plain(args[key]))
        for key, title, values in (("state", "状态", {"open": "打开", "closed": "关闭"}),
                                   ("event", "审查结论", {"APPROVE": "同意", "REQUEST_CHANGES": "请求修改", "COMMENT": "仅评论"})):
            if key in args:
                lines.append(title + "：" + values.get(str(args[key]), plain(args[key])))
        if args.get("changes"):
            labels = {"visibility": "仓库可见性", "description": "项目介绍", "default_branch": "默认分支",
                      "enforce_admins": "管理员规则", "archived": "归档状态", "homepage": "项目主页",
                      "required_status_checks": "合并前检查", "required_pull_request_reviews": "代码审查要求",
                      "restrictions": "可推送人员范围", "allow_force_pushes": "允许强制推送", "allow_deletions": "允许删除分支"}
            for key, value in args["changes"].items():
                shown = "将更新（展开详情查看）" if isinstance(value, (dict, list)) else (
                    "开启" if value is True else "关闭" if value is False else "清除" if value is None else
                    {"public": "公开", "private": "私有"}.get(str(value), plain(value)))
                lines.append(f"{labels.get(key, plain(key))}：{shown}")
        for key in ("body", "bodyText"):
            if args.get(key):
                lines.append("发布内容：" + plain(args[key]))
        return "\n\n".join(lines)
    if name == "configure_mcp":
        changes = configuration_changes(description)
        return "是否更新以下项目设置？\n\n" + "\n\n".join(changes or ["具体变更见详情"]) + "\n\n这一步不会提交或推送代码。"
    # Existing plain-language permissions remain unchanged; unknown machine
    # proposals are shown explicitly rather than given an invented benign label.
    return description
