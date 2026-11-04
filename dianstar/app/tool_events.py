"""User-readable facts from actual tool events; never inferred from reasoning."""
import json
import time
from pathlib import PureWindowsPath

from dianstar.security.command_summary import activity
from dianstar.security.approval_summary import GIT_ACTIONS, GITHUB_ACTIONS


TITLES = {"read": "读取文件", "glob": "查找文件", "grep": "搜索文件内容", "edit": "修改文件", "write": "创建或写入文件",
    "workspace_trash_put": "移入回收站", "workspace_trash_list": "查看回收站", "workspace_trash_restore": "恢复回收内容",
    "workspace_trash_purge": "永久清理本机副本", "workspace_storage_recover": "处理未完成的恢复", "workspace_storage_pin": "设置副本保留",
    "workspace_storage_cleanup": "清理到期副本", "workspace_scratch_create": "准备临时工作区", "workspace_scratch_open": "打开临时工作区",
    "workspace_scratch_list": "查看临时工作区",
    "patch": "应用文件修改", "move": "移动或重命名文件", "workspace_delete": "删除文件", "file_batch": "修改一批文件",
    "file_restore": "恢复文件", "file_history": "查看恢复记录", "review_changes": "检查本次改动", "record_review": "核对任务完成情况",
    "task_status": "查看任务进度", "open_project": "打开项目", "mcp_status": "检查工具连接", "tool_status": "查看工具状态", "search_web": "检索网页",
    "read_webpage": "读取网页", "get_weather": "查询天气", "calculate_number": "计算数值", "calculate_date": "计算日期",
    "workspace_environment_get": "查询项目开发环境", "workspace_environment_clear": "清除项目开发环境"}
READ_GIT = {"git_status": "查看项目状态", "git_diff": "查看文件差异", "git_log": "查看提交记录",
            "git_branches": "查看分支", "git_remotes": "查看远程仓库"}
PARAMETERS = {"repository", "argv", "command", "path", "source", "destination", "pattern", "query", "url", "owner", "repo",
    "paths", "id", "ids", "mode", "pinned", "small_only", "network",
    "branch", "base", "head", "remote", "files", "ref", "method", "pullNumber", "issue_number", "programs", "environment",
    "timeout_seconds", "expression", "location", "date", "offset", "limit", "decision", "transaction_id", "path_prepend"}


class ToolEvents:
    def __init__(self, scanner, request_id, conversation_id):
        self.scanner, self.request_id, self.conversation_id = scanner, request_id, conversation_id
        self.pending, self.sequence = {}, 0

    def text(self, value, limit):
        text = self.scanner.redact(str(value), env_style=True, preserve_lines=True)
        text = "".join(char for char in text if ord(char) >= 32 or char in "\n\t")
        return text[:limit] + ("…" if len(text) > limit else "")

    def common(self, action):
        name, args = action.name, action.arguments if isinstance(action.arguments, dict) else {}
        original = name.removeprefix("mcp_git__").removeprefix("mcp_github__")
        try:
            description = activity(action)
        except (KeyError, TypeError, ValueError, AttributeError):
            description = None
        title = description or TITLES.get(name) or GIT_ACTIONS.get(original) or GITHUB_ACTIONS.get(original) or READ_GIT.get(original)
        title = title or ("执行 GitHub 操作" if name.startswith(("github_", "mcp_github__")) else "调用工具")
        root = args.get("repository", "")
        target = str(args.get("path") or args.get("destination") or args.get("paths") or args.get("id") or args.get("url") or args.get("query") or
            (str(args["owner"]) + "/" + str(args.get("repo", "")) if args.get("owner") else PureWindowsPath(str(root)).name))
        def safe(value, depth=0):
            if depth > 6:
                return "[层级内容省略]"
            if isinstance(value, str):
                return self.text(value, 2500)
            if isinstance(value, dict):
                return {self.text(key, 100): safe(item, depth+1) for key, item in list(value.items())[:24]}
            if isinstance(value, list):
                return [safe(item, depth+1) for item in value[:30]]
            return value if value is None or type(value) in {bool, int, float} else "[省略]"
        parameters = {key: safe(value) for key, value in args.items() if key in PARAMETERS}
        # File bodies and arbitrary extra fields never enter the display copy.
        shown = json.dumps(parameters, ensure_ascii=False, indent=2)
        return {"request_id": self.request_id, "conversation_id": self.conversation_id, "tool": name,
            "call_id": self.text(action.call_id or "", 180), "title": self.text(title, 180), "target": self.text(target.replace("\n", " "), 500),
            "parameters": self.text(shown, 3800)}

    def started(self, action):
        self.sequence += 1
        identifier = self.request_id + ":" + str(self.sequence)
        self.pending[(action.call_id, action.name)] = (identifier, time.monotonic())
        return {**self.common(action), "event_id": identifier, "phase": "started", "status": "running"}

    def completed(self, observation):
        action, result = observation.action, observation.result
        identifier, started = self.pending.pop((action.call_id, action.name), (self.request_id + ":missing", time.monotonic()))
        error = result.get("error")
        status = "failed" if error or result.get("success") is False else "success"
        if error == "tool_cancelled":
            status = "cancelled"
        elif error in {"authorization_required", "authorization_denied", "authorization_expired", "authorization_changed",
                       "tool_disabled", "tool_not_authorized", "invalid_tool_arguments", "host_path_denied", "dangerous_command_denied",
                       "command_isolation_unavailable", "command_runtime_inaccessible", "shell_incompatible_with_isolation",
                       "local_project_disabled", "local_project_readonly", "powershell_arguments_unsupported", "powershell7_unavailable"}:
            status = "not_executed"
        review = result.get("security_review", {})
        if status == "success" and (result.get("isolation_cleanup_pending") or isinstance(review, dict) and (review.get("complete") is False or review.get("findings"))):
            status = "warning"
        if error:
            summary = result.get("message") or "操作未完成，请展开查看错误。"
        elif result.get("timed_out"):
            summary = "操作超时，请核对已经发生的改动。"
        elif result.get("exit_code") is not None:
            summary = "程序退出码 " + str(result["exit_code"])
            if isinstance(result.get("content"), str) and result["content"].strip():
                summary += " · " + self.text(result["content"], 150).splitlines()[0]
        elif result.get("message"):
            summary = result["message"]
        elif action.name == "read" and result.get("type") == "file":
            summary = "文件读取完成"
        elif action.name == "workspace_environment_inspect" and isinstance(result.get("programs"), list):
            labels = {"available": "可用", "not_found_in_checked_locations": "本次未找到", "found_but_unusable": "发现但不可用"}
            summary = "；".join(str(item.get("program", "")) + "：" + labels.get(item.get("state"), "检查未完成")
                for item in result["programs"] if isinstance(item, dict))
        elif any(isinstance(result.get(key), list) for key in ("results", "matches", "files", "entries")):
            summary = "返回 " + str(next(len(result[key]) for key in ("results", "matches", "files", "entries") if isinstance(result.get(key), list))) + " 项结果"
        elif isinstance(result.get("result"), (str, int, float)):
            summary = "结果：" + str(result["result"])
        elif status == "success":
            summary = "操作完成"
        else:
            summary = "操作未成功，请查看结果。"
        if status == "warning":
            summary += "；命令权限清理待处理，已保留恢复记录" if result.get("isolation_cleanup_pending") else "；事后检查有风险或未完整完成"
        output = result.get("content", "")
        if not isinstance(output, str):
            output = ""
        if action.name not in {"workspace_run", "bash", "shell"}:
            output = ""  # Keep file bodies and web pages in their actual tool records.
        if action.name.startswith(("workspace_trash_", "workspace_scratch_", "workspace_storage_")):
            # Storage receipts contain metadata only; recovery IDs remain
            # available in details without exposing directory/file bodies.
            keys = {"id", "status", "source", "destination", "directory", "expires", "pinned", "cleanup_pending"}
            entries = result.get("entries", [result])
            if isinstance(entries, list):
                output = json.dumps([{key: item[key] for key in keys if key in item} for item in entries[:20]
                    if isinstance(item, dict)], ensure_ascii=False, indent=2)
        if error:
            output = str(error) + ("\n" + output if output else "")
        duration = result.get("duration_ms", round((time.monotonic() - started) * 1000))
        duration = duration if type(duration) is int and 0 <= duration <= 86400000 else None
        return {**self.common(action), "event_id": identifier, "phase": "completed", "status": status,
            "success": not error and result.get("success", True), "summary": self.text(summary, 550),
            "output": self.text(output, 2800), "duration_ms": duration}


def historical_activity(messages, scanner):
    """Read old paired messages as display history, without executing tools."""
    from dianstar.core.types import ToolAction, Observation
    from dianstar.sessions.tool_activity import ToolActivity
    turns, pending = [], {}
    presenter = records = None
    for message in messages:
        if message.get("role") == "user":
            records = ToolActivity()
            turns.append(records)
            presenter = ToolEvents(scanner, "history-" + str(len(turns)), None)
            pending = {}
        elif presenter is not None and message.get("role") == "assistant":
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                try:
                    function = call["function"]
                    if not isinstance(function["name"], str) or not isinstance(call["id"], str):
                        continue
                    if len(function["arguments"]) > 32768:
                        continue
                    arguments = json.loads(function["arguments"])
                    if not isinstance(arguments, dict):
                        continue
                    action = ToolAction(function["name"], arguments, call["id"])
                    pending[call["id"]] = action
                except (KeyError, TypeError, ValueError):
                    continue
        elif presenter is not None and message.get("role") == "tool":
            if not isinstance(message.get("tool_call_id"), str):
                continue
            action = pending.pop(message.get("tool_call_id"), None)
            if action is None:
                continue
            records.consume(presenter.started(action))
            try:
                content = message["content"]
                result = json.loads(content) if isinstance(content, str) and len(content) <= 100000 else None
                if not isinstance(result, dict):
                    raise ValueError("Invalid result")
            except (KeyError, TypeError, ValueError):
                result = {"error": "historical_result_unavailable", "message": "历史结果无法完整显示，可查看原任务记录。"}
            event = presenter.completed(Observation(action, result))
            if "duration_ms" not in result:
                event["duration_ms"] = None
            records.consume(event)
    return [records.snapshot() for records in turns]
