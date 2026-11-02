"""Explicit, approved public environment settings scoped by application identity."""
import json
import time

from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.platform.command_environment import BUILD_VARIABLES, profile_values
from dianstar.security.authorization import request_key
from dianstar.security.content_guard import SecurityViolation
from dianstar.security.gateway import ToolPreparationError
from dianstar.sessions.environments import EnvironmentProfileError, EnvironmentStore
from .files.io import blocking


ENVIRONMENT_TOOLS = frozenset({"workspace_environment_get", "workspace_environment_set", "workspace_environment_clear"})


class EnvironmentTools:
    def __init__(self, context, conversation_id):
        self.context = context
        self.store = EnvironmentStore(conversation_id)
        self.prepared = {}

    def inspect_values(self, values, *, check_directories=True):
        values = profile_values(values["environment"], values["path_prepend"], check_directories=check_directories)
        self.context.security.run("before_command", [("command-environment.cfg",
            ("\n".join(f"{key}={value}" for key, value in values["environment"].items()) + "\n" +
             "\n".join(values["path_prepend"])).encode())])
        return values

    def current(self, root):
        if not self.store.conversation_id:
            return {"revision": 0, "environment": {}, "path_prepend": [], "configured": False}
        profile = self.store.load(root)
        try:
            self.inspect_values(profile)
        except SecurityViolation:
            raise
        except (ValueError, OSError):
            raise EnvironmentProfileError("project_environment_stale", "已保存的开发环境路径失效；请查询并更新或清除配置，再重新定位程序。") from None
        return profile

    def prepare(self, name, args):
        root = self.context.root(args)
        previous = self.store.load(root)
        if previous["revision"] != args["expected_revision"]:
            raise EnvironmentProfileError("project_environment_changed", "会话环境版本已变化，请重新查询；未保存。")
        values = None if name == "workspace_environment_clear" else self.inspect_values(args)
        return {"revision": previous["revision"], "values": values}

    @staticmethod
    def error(error):
        if isinstance(error, SecurityViolation):
            return error.result()
        if isinstance(error, EnvironmentProfileError):
            return {"error": error.code, "message": error.message}
        return {"error": "project_environment_invalid", "message": "会话开发环境检查失败；仅接受非敏感构建变量和存在的本机绝对搜索目录。"}

    async def get(self, action):
        try:
            root = self.context.root(action.arguments)
            profile = await blocking(lambda cancel: self.store.load(root))
            # A missing SDK must remain queryable/clearable, without exposing a
            # value that is now known to be a credential.
            self.inspect_values(profile, check_directories=False)
            try:
                profile_values(profile["environment"], profile["path_prepend"])
                valid = True
            except (ValueError, OSError):
                valid = False
            result = {**profile, "repository": str(root), "scope": "conversation_project", "valid": valid,
                "message": "只保存公开开发环境；不保存完整宿主 PATH、凭据、shell 状态或操作授权。"}
        except (ValueError, OSError, SecurityViolation) as error:
            result = self.error(error)
        return Observation(action, result)

    async def preview(self, name, args):
        try:
            state = await blocking(lambda cancel: self.prepare(name, args))
            self.prepared[request_key(name, args)] = (time.monotonic(), state)
            return json.dumps({"操作": "清除会话项目环境" if state["values"] is None else "保存会话项目环境",
                "项目位置": args["repository"], "作用范围": "仅当前会话中的这个项目，继续此会话时仍可复用。",
                "版本": state["revision"], "保存内容": state["values"],
                "说明": "整体替换旧配置；不改系统环境，不安装程序，也不授予后续命令执行权限。"}, ensure_ascii=False)
        except (ValueError, OSError, SecurityViolation) as error:
            result = self.error(error)
            raise ToolPreparationError(result["error"], result["message"], findings=result.get("findings")) from None

    async def apply(self, action):
        item = self.prepared.pop(request_key(action.name, action.arguments), None)
        if not item or time.monotonic() - item[0] > 600 or not self.context.mcp.authorization.active:
            return Observation(action, {"error": "workspace_not_prepared"})
        try:
            def save(cancel):
                root = self.context.root(action.arguments)
                # Same OS repository gate as commands, before the short DB
                # lock. Never upgrade a shared lock or hold one over approval.
                with self.context.files.locks.repository(root, cancel=cancel):
                    if cancel.is_set() or not self.context.mcp.authorization.active:
                        raise EnvironmentProfileError("authorization_expired", "操作已取消，未保存环境。")
                    current = self.prepare(action.name, action.arguments)
                    if current != item[1]:
                        raise EnvironmentProfileError("project_environment_changed", "确认后环境发生变化，未保存。")
                    revision = self.store.replace(root, current["revision"], current["values"])
                    return {"success": True, "revision": revision, "configured": current["values"] is not None,
                        "scope": "conversation_project", "repository": str(root)}
            result = await blocking(save)
        except (ValueError, OSError, SecurityViolation) as error:
            result = self.error(error)
        return Observation(action, result)

    def registrations(self):
        if not self.context.mcp.settings.git_enabled or not self.store.conversation_id:
            return {}
        definitions = {
            "workspace_environment_get": ("查询当前会话在这个项目中保存的开发环境与版本；不读取完整宿主环境。", {}),
            "workspace_environment_set": ("保存或整体替换当前会话的项目开发环境，跨轮复用；先查询版本。仅公开变量，PATH 用 path_prepend。不是安装或命令授权。", {
                "environment": {"type": "object", "propertyNames": {"enum": sorted(BUILD_VARIABLES)}, "maxProperties": 24,
                    "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 4096}},
                "path_prepend": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4096}, "maxItems": 16},
                "expected_revision": {"type": "integer", "minimum": 0}}),
            "workspace_environment_clear": ("清除当前会话在这个项目中保存的开发环境，后续回到宿主环境；先查询版本，不修改系统配置。", {
                "expected_revision": {"type": "integer", "minimum": 0}})}
        result = {}
        for name, (description, properties) in definitions.items():
            mutation = name != "workspace_environment_get"
            if mutation and not self.context.mcp.settings.workspace_write_enabled:
                continue
            properties = {"repository": {"type": "string", "minLength": 1}, **properties}
            schema = {"type": "function", "function": {"name": name, "description": description, "parameters": {
                "type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}}
            async def preview(args, name=name):
                return await self.preview(name, args)
            result[name] = ToolRegistration(schema, "workspace.write" if mutation else "workspace.read",
                self.apply if mutation else self.get, preview if mutation else None,
                required_tools=("workspace_run",) if mutation else (), effects=("project.environment",) if mutation else ())
        return result
