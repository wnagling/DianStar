"""Open an explicit user-approved project without exposing internal settings."""
from dataclasses import asdict
import json
from pathlib import Path
import time

from dianstar.config.mcp import load_mcp_settings, normalize, save_mcp_settings
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.security.authorization import request_key
from dianstar.security.gateway import ToolPreparationError
from dianstar.app.mcp_startup import project_repositories


def registrations(config_path, reconnect, current_directory):
    prepared = {}

    async def preview(args):
        try:
            root = Path(args["path"])
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
            after = normalize({**asdict(before), "git_repositories": list(dict.fromkeys((*before.git_repositories, *roots)))})
            fingerprint = config_path.read_bytes() if config_path.exists() else None
            prepared[request_key("open_project", args)] = (time.monotonic(), fingerprint, after, root, roots)
            return json.dumps({"操作": "打开项目", "项目位置": str(root),
                "关联主项目": list(roots[:-1]),
                "说明": "记住此项目，后续修改仍遵循当前审批策略；本次不提交、不推送代码。"}, ensure_ascii=False)
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
            settings = save_mcp_settings(item[2], config_path)
            try:
                await reconnect(settings, directory=root)
            except (ValueError, OSError):
                return Observation(action, {"error": "project_connection_failed", "project_saved": True,
                    "message": "已记住这个项目，但连接未完成，请先查询工具状态；不要重复申请扩大范围。"})
            return Observation(action, {"success": True, "project": str(root), "message": "项目已打开，可继续检查修改；未提交或推送代码。"})
        except (ValueError, OSError):
            return Observation(action, {"error": "project_open_failed", "message": "项目打开失败，请查询工具状态；本次未提交或推送代码。"})

    schema = {"type": "function", "function": {"name": "open_project", "description":
        "打开用户指定的现有本地 Git 项目，切换当前任务项目并记住路径；新项目先由用户确认，保留后台操作策略。当前项目不必重复打开。",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}}
    return {"open_project": ToolRegistration(schema, "project.open", apply, preview)}
