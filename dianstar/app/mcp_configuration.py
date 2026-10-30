"""Exact, reviewable changes to repository scopes; credentials stay in the UI."""
from dataclasses import asdict
import json
import time

from dianstar.config.mcp import load_mcp_settings, normalize, save_mcp_settings
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.security.authorization import request_key
from dianstar.security.gateway import ToolPreparationError

FIELDS = {"git_enabled", "git_write_enabled", "workspace_write_enabled", "git_repositories", "git_workspace_roots", "github_enabled",
          "github_repositories", "github_write_repositories", "github_private_repositories", "github_admin_repositories"}

def schema(name, properties):
    return {"type": "function", "function": {"name": name,
        "description": "查看仓库接入配置。" if name.startswith("get_") else "修改仓库接入范围；展示完整前后配置，须由用户明确确认，保存后立即重连。不能设置凭据或服务器程序。",
        "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}}

def registrations(path, reconnect):
    prepared = {}
    async def read(action):
        try:
            value = {"configuration_file": str(path), "exists": path.exists(), "settings": asdict(load_mcp_settings(path))}
        except ValueError:
            value = {"error": "mcp_configuration_invalid", "configuration_file": str(path)}
        return Observation(action, value)
    async def preview(args):
        try:
            before = load_mcp_settings(path)
            changes = args["changes"]
            if not changes or set(changes) - FIELDS:
                raise ValueError("Unsupported configuration field")
            after = normalize({**asdict(before), **changes})
            fingerprint = path.read_bytes() if path.exists() else None
            prepared[request_key("configure_mcp", args)] = (time.monotonic(), fingerprint, after)
            return json.dumps({"修改前": asdict(before), "修改后": asdict(after), "说明": "允许范围扩大后，相应工具仍遵守逐次操作授权。"}, ensure_ascii=False, indent=2)
        except (OSError, ValueError, KeyError, TypeError):
            raise ToolPreparationError("mcp_configuration_invalid", "连接参数无效；程序组件和账号凭据由应用管理，不通过此工具修改。") from None
    async def apply(action):
        item = prepared.pop(request_key("configure_mcp", action.arguments), None)
        try:
            if not item or time.monotonic() - item[0] > 600 or (path.read_bytes() if path.exists() else None) != item[1]:
                return Observation(action, {"error": "mcp_configuration_changed"})
            settings = save_mcp_settings(item[2], path)
            try:
                await reconnect(settings)
                return Observation(action, {"success": True, "configuration_saved": True, "reconnected": True})
            except (OSError, ValueError):
                return Observation(action, {"success": True, "configuration_saved": True, "reconnected": False, "message": "配置已保存，重连失败，请查询 mcp_status。"})
        except (OSError, ValueError):
            return Observation(action, {"error": "mcp_configuration_save_failed"})
    changes = {name: {"type": ["boolean", "null"]} if name in {"git_enabled", "github_enabled", "git_write_enabled", "workspace_write_enabled"}
               else {"type": ["array", "null"], "items": {"type": "string"}, "maxItems": 10}
                    if name in {"github_write_repositories", "github_admin_repositories"}
               else {"type": "boolean"} if name.endswith("enabled") else {"type": "array", "items": {"type": "string"}, "maxItems": 10}
               for name in sorted(FIELDS)}
    return {
        "get_mcp_configuration": ToolRegistration(schema("get_mcp_configuration", {}), "permissions.read", read),
        "configure_mcp": ToolRegistration(schema("configure_mcp", {"changes": {"type": "object", "properties": changes,
            "minProperties": 1, "additionalProperties": False}}), "mcp.configuration.change", apply, preview),
    }
