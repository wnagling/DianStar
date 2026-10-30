"""Git preview and approval protocol; internal tokens never enter model tools."""
import json
from pathlib import Path
from dianstar.security.gateway import ToolPreparationError
from dianstar.security.authorization import request_key
from dianstar.tools.mcp.git_contract import WRITE_TOOLS as GIT_WRITE_TOOLS
from dianstar.tools.mcp.git_contract import BOOTSTRAP_TOOLS
from dianstar.tools.mcp.connection import MCPUnavailable


class GitAdapter:
    def __init__(self, settings, authorization, connections, *, allow_clone=False, confirm_destinations=False):
        self.settings = settings
        self.allow_clone = allow_clone
        self.confirm_destinations = confirm_destinations
        self.authorization = authorization
        self.connections = connections
        self.prepared = {}
        self.write_attempts = 0
        self.repositories = set(settings.git_repositories)

    def allowed(self, name, arguments):
        root = Path(arguments.get("repository", ""))
        if not root.is_absolute() or str(root).startswith(("\\\\", "//")):
            return False
        if name in BOOTSTRAP_TOOLS:
            scoped = any(root != Path(base) and root.is_relative_to(Path(base)) for base in self.settings.git_workspace_roots)
            if not self.settings.git_workspace_roots and (self.confirm_destinations or (name == "git_clone" and self.allow_clone)):
                scoped = root != root.parent
        else:
            scoped = str(root) in self.repositories
        return scoped and root.resolve() == root.absolute()

    def validate_read(self, name, arguments):
        root = Path(arguments.get("repository", ""))
        if not self.allowed(name, arguments):
            return {"error": "mcp_repository_denied"}
        if name == "git_log" and (type(arguments.get("limit", 10)) is not int or not 1 <= arguments.get("limit", 10) <= 30):
            return {"error": "invalid_tool_arguments"}
        return None

    @staticmethod
    def _git_result(result, *, allow_operation_error=False):
        value = getattr(result, "structuredContent", None)
        if value is None:
            for item in result.content:
                if getattr(item, "type", None) == "text":
                    try:
                        value = json.loads(item.text)
                    except ValueError:
                        continue
                    break
        if result.isError or not isinstance(value, dict):
            raise ToolPreparationError("git_preparation_failed", "Git 服务未返回可核验的操作预览。")
        if value.get("error") and not allow_operation_error:
            raise ToolPreparationError(value["error"], value.get("message", "Git 检查未通过，未执行。"),
                findings=value.get("findings") if isinstance(value.get("findings"), list) else None,
                source=value.get("source") if value.get("source") == "git_index" else None)
        return value

    async def prepare_git(self, name, arguments):
        writable = self.settings.git_write_enabled or (name == "git_clone" and self.allow_clone)
        if not writable or not self.authorization.active or name not in GIT_WRITE_TOOLS:
            raise ToolPreparationError("git_write_disabled", "尚未允许申请 Git 写操作。")
        try:
            root = Path(arguments.get("repository", ""))
            if not self.allowed(name, arguments):
                raise ToolPreparationError("mcp_repository_denied", "仓库不在配置允许范围内。")
            if self.write_attempts >= 30:
                raise ToolPreparationError("git_operation_limit", "本轮 Git 操作预览已达 30 次。")
            self.write_attempts += 1
            value = self._git_result(await self.connections["git"].call(name, arguments))
            if value.get("prepared") is not True or not isinstance(value.get("token"), str) or not isinstance(value.get("description"), str):
                raise ToolPreparationError("git_preparation_failed", "Git 操作预览无效。")
            self.prepared[request_key(name, arguments)] = value["token"]
            return "\n".join("".join(char for char in line if ord(char) >= 32 or char == "\t")
                             for line in value["description"].splitlines())
        except (OSError, ValueError, MCPUnavailable, TimeoutError):
            raise ToolPreparationError("git_preparation_failed", "Git 预览失败或超时，未申请执行。") from None

    async def apply_git(self, name, arguments):
        token = self.prepared.pop(request_key(name, arguments), None)
        if token is None or not self.authorization.active:
            return {"error": "git_not_prepared", "message": "没有有效的本轮已批准操作。"}
        try:
            result = self._git_result(await self.connections["git"].call("git_apply", {"token": token}),
                                      allow_operation_error=True)
            content = result.get("content", "")
            result["content"] = content[:24000]
            result["truncated"] = len(content) > 24000
            created = result.get("created_repository")
            if result.get("success") and created:
                if not self.allowed("git_clone" if name == "git_clone" else "git_init", {"repository": created}):
                    return {"error": "git_created_repository_outside_scope"}
                self.repositories.add(created)
            if result.get("removed_repository"):
                self.repositories.discard(result["removed_repository"])
            return result
        except ToolPreparationError as error:
            return error.result
        except (MCPUnavailable, TimeoutError):
            return {"error": "git_outcome_unknown", "message": "Git 执行连接中断，请先查询状态，不要自动重试。"}
