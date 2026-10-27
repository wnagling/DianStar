"""Git preview and approval protocol; internal tokens never enter model tools."""
import json
from pathlib import Path
from dianstar.security.gateway import ToolPreparationError
from dianstar.security.authorization import request_key
from dianstar.tools.mcp.git_contract import WRITE_TOOLS as GIT_WRITE_TOOLS
from dianstar.tools.mcp.connection import MCPUnavailable


class GitAdapter:
    def __init__(self, settings, authorization, connections):
        self.settings = settings
        self.authorization = authorization
        self.connections = connections
        self.prepared = {}
        self.write_attempts = 0

    def validate_read(self, name, arguments):
        root = Path(arguments.get("repository", ""))
        if not root.is_absolute() or str(root.resolve(strict=True)) not in self.settings.git_repositories:
            return {"error": "mcp_repository_denied"}
        if name == "git_log" and (type(arguments.get("limit", 10)) is not int or not 1 <= arguments.get("limit", 10) <= 30):
            return {"error": "invalid_tool_arguments"}
        return None

    @staticmethod
    def _git_result(result):
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
        if value.get("error"):
            raise ToolPreparationError(value["error"], value.get("message", "Git 检查未通过，未执行。"))
        return value

    async def prepare_git(self, name, arguments):
        if not self.settings.git_write_enabled or not self.authorization.active or name not in GIT_WRITE_TOOLS:
            raise ToolPreparationError("git_write_disabled", "尚未允许申请 Git 写操作。")
        try:
            root = Path(arguments.get("repository", ""))
            if not root.is_absolute() or str(root.resolve(strict=True)) not in self.settings.git_repositories:
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
            result = self._git_result(await self.connections["git"].call("git_apply", {"token": token}))
            content = result.get("content", "")
            result["content"] = content[:24000]
            result["truncated"] = len(content) > 24000
            return result
        except ToolPreparationError as error:
            return error.result
        except (MCPUnavailable, TimeoutError):
            return {"error": "git_outcome_unknown", "message": "Git 执行连接中断，请先查询状态，不要自动重试。"}
