"""Open an explicit user-approved project without exposing internal settings."""
from dataclasses import asdict
import json
from pathlib import Path
import re
import time

from dianstar.config.mcp import load_mcp_settings, normalize, save_mcp_settings
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.security.authorization import request_key
from dianstar.security.gateway import ToolPreparationError
from dianstar.app.mcp_startup import project_repositories
from dianstar.app.task_scope import TaskScope
from dianstar.security.system_paths import check_host_path


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


def registrations(config_path, reconnect, current_directory, *, question=""):
    prepared = {}
    requested = requested_project(question) or TaskScope.from_question(question, current_directory()).root

    async def preview(args):
        try:
            root = Path(args["path"])
            check_host_path(root, project=True)
            if (not root.is_absolute() or str(root).startswith(("\\\\", "//"))
                    or root.resolve(strict=True) != root.absolute() or not root.is_dir()):
                raise ValueError("Invalid project directory")
            root = root.resolve(strict=True)
            roots = project_repositories(root)
            if not roots or str(root) != roots[-1]:
                raise ValueError("Select the Git project root")
            before = load_mcp_settings(config_path)
            if before.git_enabled is False:
                raise ValueError("Backend policy disabled project access")
            current_roots = project_repositories(current_directory())
            already_available = before.git_enabled is None or all(path in before.git_repositories for path in roots)
            if current_roots and str(root) == current_roots[-1] and already_available:
                raise ToolPreparationError("project_already_open", "当前项目已可直接使用，请继续检查修改；无需调整工具配置。")
            temporary = requested == root and already_available
            after = before if temporary else normalize({**asdict(before), "git_repositories": list(dict.fromkeys((*before.git_repositories, *roots)))})
            fingerprint = config_path.read_bytes() if config_path.exists() else None
            prepared[request_key("open_project", args)] = (time.monotonic(), fingerprint, after, root, roots, temporary)
            return json.dumps({"操作": "打开项目", "项目位置": str(root),
                "关联主项目": list(roots[:-1]),
                "记住项目": not temporary,
                "说明": "仅在本轮打开用户指定项目，不保存路径；后续修改仍遵循当前审批策略。" if temporary else
                        "记住此项目，后续修改仍遵循当前审批策略；本次不提交、不推送代码。"}, ensure_ascii=False)
        except (ValueError, OSError, KeyError):
            raise ToolPreparationError("project_open_denied", "无法打开此项目，请确认是本地 Git 项目的根目录，并符合后台策略。") from None

    async def apply(action):
        item = prepared.pop(request_key("open_project", action.arguments), None)
        try:
            if not item or time.monotonic() - item[0] > 600 or (config_path.read_bytes() if config_path.exists() else None) != item[1]:
                return Observation(action, {"error": "project_configuration_changed"})
            root = item[3]
            if root.resolve(strict=True) != root.absolute() or project_repositories(root) != item[4]:
                return Observation(action, {"error": "project_path_changed"})
            settings = item[2] if item[5] else save_mcp_settings(item[2], config_path)
            try:
                await reconnect(settings, directory=root)
            except (ValueError, OSError):
                return Observation(action, {"error": "project_connection_failed", "project_saved": not item[5],
                    "message": "项目连接未完成，请先查询工具状态；不要重复申请扩大范围。"})
            return Observation(action, {"success": True, "project": str(root), "project_saved": not item[5],
                                       "message": "项目已打开，可继续检查修改；未提交或推送代码。"})
        except (ValueError, OSError):
            return Observation(action, {"error": "project_open_failed", "message": "项目打开失败，请查询工具状态；本次未提交或推送代码。"})

    schema = {"type": "function", "function": {"name": "open_project", "description":
        "打开现有本地 Git 项目；用户在本轮直接指定路径时仅临时接入，无需重复确认。其他项目或长期记住路径仍需确认，保留后台策略。当前项目不必重复打开。",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}}
    def automatic(args):
        item = prepared.get(request_key("open_project", args))
        return bool(item and item[5])

    return {"open_project": ToolRegistration(schema, "project.open", apply, preview, auto_approve=automatic)}
