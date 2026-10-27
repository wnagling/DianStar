"""Compose MCP transport, product adapters and tool registrations."""
import copy
from pathlib import Path
import httpx
from dianstar.core.types import Observation
from dianstar.config.mcp import MCPSettings
from dianstar.security.authorization import Authorization
from dianstar.core.registry import ToolRegistration
from dianstar.tools.mcp.connection import MCPUnavailable, StdioConnection
from dianstar.tools.mcp.schemas import GIT_READ_TOOLS, GIT_WRITE_TOOLS, adapted_schema
from dianstar.tools.mcp.launch import server_parameters
from dianstar.tools.mcp.git_adapter import GitAdapter
from dianstar.tools.mcp.github_adapter import GitHubAdapter
from dianstar.tools.mcp.github_contract import READ_TOOLS as GITHUB_READ_TOOLS, WRITE_TOOLS as GITHUB_WRITE_TOOLS
from dianstar.tools.mcp.github_writes import GitHubWrites
from dianstar.tools.mcp.results import normalize_result


class MCPTools:
    def __init__(self, settings: MCPSettings, env_file: Path, authorization: Authorization):
        self.settings, self.env_file, self.authorization = settings, env_file, authorization
        self.connections = {}
        self.status = {}
        self.registry = {}
        self.git = GitAdapter(settings, authorization, self.connections)
        self.github = GitHubAdapter(settings, env_file)
        self.github_writes = GitHubWrites(self.github, authorization, self.connections, self._public_repository)

    async def __aenter__(self):
        try:
            try:
                params = server_parameters(self.settings, self.env_file)
            except (ValueError, OSError):
                # One invalid executable must not break normal chat or local Git.
                from dataclasses import replace
                params = server_parameters(replace(self.settings, github_enabled=False), self.env_file)
                self.status["github"] = {"state": "unavailable", "message": "请检查 GitHub MCP 程序路径。"}
            for server, parameters in params.items():
                connection = StdioConnection(parameters)
                self.connections[server] = connection
                try:
                    await connection.start()
                    count = 0
                    seen = set()
                    for tool in connection.tools:
                        if tool.name in seen:
                            raise MCPUnavailable()
                        seen.add(tool.name)
                        if server == "git" and tool.name in GIT_WRITE_TOOLS and not self.settings.git_write_enabled:
                            continue
                        if server == "github" and tool.name in GITHUB_WRITE_TOOLS and not self.settings.github_write_repositories:
                            continue
                        schema = adapted_schema(server, tool)
                        if schema is None:
                            continue
                        name = schema["function"]["name"]
                        async def handler(action, server=server, original=tool.name):
                            return Observation(action, await self.execute(server, original, action.arguments))
                        if server == "git" and tool.name in GIT_WRITE_TOOLS:
                            async def prepare(arguments, original=tool.name):
                                return await self.prepare_git(original, arguments)
                            async def apply(action, original=tool.name):
                                return Observation(action, await self.apply_git(original, action.arguments))
                            self.registry[name] = ToolRegistration(schema, f"mcp.git.{tool.name}", apply, prepare)
                        elif server == "github" and tool.name in GITHUB_WRITE_TOOLS:
                            async def prepare(arguments, original=tool.name):
                                return await self.github_writes.prepare(original, arguments)
                            async def apply(action, original=tool.name):
                                return Observation(action, await self.github_writes.apply(original, action.arguments))
                            self.registry[name] = ToolRegistration(schema, f"mcp.github.{tool.name}", apply, prepare)
                        else:
                            self.registry[name] = ToolRegistration(schema, f"mcp.{server}.read", handler)
                        count += 1
                    self.authorization.grant(f"mcp.{server}.read", uses=30)
                    self.status[server] = {"state": "connected", "tools": count}
                    if server == "github":
                        expected = GITHUB_READ_TOOLS | (GITHUB_WRITE_TOOLS if self.settings.github_write_repositories else set())
                        self.status[server]["unavailable_tools"] = sorted(expected - {
                            name.removeprefix("mcp_github__") for name in self.registry if name.startswith("mcp_github__")})
                except Exception:
                    await connection.close()
                    self.registry = {name: entry for name, entry in self.registry.items()
                                     if not entry.capability.startswith(f"mcp.{server}.")}
                    self.status[server] = {"state": "unavailable", "message": "连接失败，请检查程序、凭据、网络或仓库配置。"}
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        self.git.prepared.clear()
        self.github_writes.prepared.clear()
        for connection in reversed(list(self.connections.values())):
            await connection.close()
        for server in self.connections:
            self.authorization.revoke(f"mcp.{server}.read")

    def information(self):
        status = copy.deepcopy(self.status)
        for server, connection in self.connections.items():
            if connection.changed or connection.task is None or connection.task.done():
                status[server] = {"state": "unavailable", "message": "服务已断开或工具定义已变化；下次提问重新连接。"}
        return {"servers": status,
                "git_repositories": list(self.settings.git_repositories),
                "github_repositories": list(self.settings.github_repositories),
                "github_private_repositories": list(self.settings.github_private_repositories),
                "github_write_repositories": list(self.settings.github_write_repositories),
                "tools": list(self.registry), "read_only": not ((self.settings.git_enabled and self.settings.git_write_enabled)
                    or (self.settings.github_enabled and self.settings.github_write_repositories)),
                "git_writes_require_approval": True, "github_writes_require_approval": True}

    async def prepare_git(self, name, arguments):
        return await self.git.prepare_git(name, arguments)

    async def apply_git(self, name, arguments):
        return await self.git.apply_git(name, arguments)

    async def _public_repository(self, owner, repo):
        return await self.github._public_repository(owner, repo)

    async def execute(self, server: str, name: str, arguments: dict) -> dict:
        if not self.authorization.active or server not in self.connections:
            return {"error": "mcp_not_authorized"}
        if name not in (GIT_READ_TOOLS if server == "git" else GITHUB_READ_TOOLS):
            return {"error": "mcp_tool_denied"}
        try:
            error = (self.git.validate_read(name, arguments) if server == "git" else
                     await self.github.validate_read(name, arguments, self._public_repository))
            if error:
                return error
            result = await self.connections[server].call(name, arguments)
            repositories = self.github.repositories(name, arguments) if server == "github" else ()
            return normalize_result(result, server, name, arguments, self.env_file, repositories=repositories)
        except (MCPUnavailable, OSError, ValueError, TypeError, TimeoutError, httpx.HTTPError):
            return {"error": "mcp_unavailable", "message": "MCP 查询失败或超时，请检查服务状态。"}
