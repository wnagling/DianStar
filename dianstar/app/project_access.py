"""Open local projects; version-control connections are optional enrichment."""
from dataclasses import replace
import json
from pathlib import Path
import re
import time

from dianstar.config.projects import load_project_settings, save_project_settings
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.security.authorization import request_key
from dianstar.security.gateway import ToolPreparationError
from dianstar.security.system_paths import check_host_path
from dianstar.app.task_scope import TaskScope


def requested_project(question):
    """Recognize a direct task with a literal path, never history or tool text."""
    command = re.match(r"^\s*(?:(?:请|帮我|麻烦|先)\s*)*(把|将|打开|进入|切换到|查看|检查|读取|分析|修改|修复|提交并推送|提交|推送)\s*(?:项目\s*)?", question)
    if not command or re.search(r"记住|解释|举例|例子|假设|是什么意思|(?:不要|别)(?:打开|读取|接入|切换)", question):
        return None
    tail = question[command.end():]
    match = re.match(r'''(?:"([^"\r\n]+)"|'([^'\r\n]+)'|“([^”\r\n]+)”|`([^`\r\n]+)`|([^\s，。；,;]+))''', tail)
    if not match:
        return None
    value = next(group for group in match.groups() if group is not None)
    remaining = tail[match.end():]
    if command[1] in {"把", "将"} and not re.search(r"打开|读取|查看|检查|分析|修改|修复|开发|提交|推送", remaining):
        return None
    root = Path(value)
    return root if root.is_absolute() else None


def registrations(config_path, projects, opened, current_directory, *, question=""):
    prepared = {}
    requested = requested_project(question) or TaskScope.from_question(question, current_directory()).root

    def fingerprint():
        return config_path.read_bytes() if config_path.exists() else None

    def checked(args, settings):
        root = check_host_path(Path(args["path"]), project=True)
        create = args.get("create", False)
        if create:
            if root.exists() or not root.parent.is_dir() or root.parent.resolve(strict=True) != root.parent.absolute():
                raise ValueError("Creation target exists or parent missing")
            if settings.workspace_roots and not any(root != Path(parent) and root.is_relative_to(Path(parent))
                    for parent in settings.workspace_roots):
                raise ValueError("Outside creation scope")
        elif not root.is_dir() or root.resolve(strict=True) != root.absolute():
            raise ValueError("Invalid local project")
        return root, create

    async def preview(args):
        try:
            settings = load_project_settings(config_path, fallback=projects.settings)
            root, create = checked(args, settings)
            if not settings.enabled or create and not settings.write_enabled:
                raise ValueError("Local project policy denied access")
            if not create and root == Path(current_directory()).absolute() and str(root) in projects.roots:
                raise ToolPreparationError("project_already_open", "当前项目已可直接使用，无需重复打开。")
            temporary = requested == root
            parent = root.parent.stat()
            prepared[request_key("open_project", args)] = (time.monotonic(), fingerprint(), root, create,
                temporary, settings, [parent.st_dev, parent.st_ino])
            return json.dumps({"操作": "创建并打开项目" if create else "打开项目", "项目位置": str(root),
                "记住项目": not temporary, "创建目录": create,
                "说明": "只处理这个本地目录；文件和命令按后台策略授权，不自动初始化 Git、安装依赖或提交推送。"}, ensure_ascii=False)
        except (ValueError, OSError, KeyError):
            raise ToolPreparationError("project_open_denied", "无法打开此本地目录，请检查路径、创建目标及后台项目策略；不要求是 Git 仓库。") from None

    async def apply(action):
        item = prepared.pop(request_key("open_project", action.arguments), None)
        try:
            if not item or time.monotonic()-item[0] > 600 or fingerprint() != item[1]:
                return Observation(action, {"error": "project_configuration_changed"})
            root, create = checked(action.arguments, item[5])
            parent = root.parent.stat()
            if root != item[2] or create != item[3] or [parent.st_dev, parent.st_ino] != item[6]:
                return Observation(action, {"error": "project_path_changed"})
            if not projects.settings.enabled or create and not projects.settings.write_enabled:
                return Observation(action, {"error": "project_open_denied"})
            # Re-reading a saved read-only policy applies even to a temporary
            # open. Opening a project never lifts an in-memory restriction.
            projects.settings = replace(item[5], write_enabled=item[5].write_enabled and projects.settings.write_enabled)
            if create:
                root.mkdir()
            saved, warning = False, None
            if not item[4]:
                try:
                    projects.settings = save_project_settings(replace(projects.settings, roots=tuple(dict.fromkeys((*projects.settings.roots, str(root))))), config_path)
                    saved = True
                except (ValueError, OSError):
                    warning = "目录已打开，但记住项目失败；当前任务仍可使用，请保留现有文件。"
            projects.include(root)
            try:
                connection_warning = await opened(root)
            except Exception:
                connection_warning = "本地项目已打开；可选外部连接未完成，本地工具仍可使用。"
            result = {"success": True, "project": str(root), "project_saved": saved, "created": create,
                "message": "本地项目已打开，文件和命令工具可继续使用；没有初始化 Git 或提交推送。"}
            if warning or connection_warning:
                result["warning"] = warning or connection_warning
            return Observation(action, result)
        except (ValueError, OSError):
            return Observation(action, {"error": "project_open_failed", "message": "项目打开未完整完成；请核对目录现状，不盲目重试创建。"})

    schema = {"type": "function", "function": {"name": "open_project", "description":
        "打开本地项目目录，不要求Git或MCP。目录不存在且用户要求创建时用create=true，父目录须存在，不自动git init。当前项目无需重复打开；用户明确指定的本次项目可按后台策略免重复确认，其他范围需确认。",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "create": {"type": "boolean"}},
            "required": ["path"], "additionalProperties": False}}}
    def automatic(args):
        item = prepared.get(request_key("open_project", args))
        return bool(item and item[4])
    return {"open_project": ToolRegistration(schema, "project.open", apply, preview, auto_approve=automatic)}
