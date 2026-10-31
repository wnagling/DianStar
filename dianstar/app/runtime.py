# 编排一轮提问：调用模型与工具，并更新成功的对话历史。

import json
from dataclasses import replace
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dianstar.config.models import load_model_settings
from dianstar.providers.deepseek import ENV_FILE, DeepSeekSession, create_client, usable_history
from dianstar.core.types import Observation, ToolAction
from dianstar.core.loop import run_loop
from dianstar.prompts.builder import build_system_prompt
from dianstar.config.search import load_search_settings
from dianstar.evidence.source_evidence import SourceEvidence
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.core.registry import ToolRegistration
from dianstar.tools.mcp.client import MCPTools
from dianstar.tools.mcp.schemas import MCP_STATUS_TOOL
from dianstar.config.mcp import MCPSettings, load_mcp_settings, save_mcp_settings
from dianstar.tools.local.web import WebTools


from dianstar.tools.local.registry import build_local_registry
from dianstar.app.permission_control import apply_permission_intent
from dianstar.app.mcp_configuration import registrations as mcp_configuration_tools
from dianstar.app.project_access import registrations as project_access_tools
from dianstar.app.mcp_startup import resolve_connections
from dianstar.tools.local.github_admin import GitHubAdmin
from dianstar.tools.local.workspace import WorkspaceTools


async def run_turn(
    question: str,
    *,
    history: list[dict[str, object]] | None = None,
    env_file: Path = ENV_FILE,
    on_stream: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_paid_web: Callable[[dict], Awaitable[str]] | None = None,
    on_permission: Callable[[dict], Awaitable[bool]] | None = None,
    project_directory: Path | None = None,
    tool_policies: Mapping[str, ToolPolicy] | None = None,
    allow_backend_configuration: bool = False,
) -> str:
    """Run one bounded agent task and return the model's final text."""
    if not question.strip():
        raise ValueError("Please provide a question.")

    cleaned_history = usable_history(history)
    evidence = SourceEvidence(cleaned_history)
    settings = load_model_settings(env_file.parent / ".local" / "config.json")
    search_settings = load_search_settings(env_file.parent / ".local" / "search.json")
    if not settings.default_model:
        raise ValueError("当前默认 Key 尚未添加模型，请在 /config 的 model 中先添加模型。")
    instructions = build_system_prompt()
    client = create_client(env_file, key_id=settings.default_key_id)
    authorization = Authorization()
    mcp_path = env_file.parent / ".local" / "mcp.json"
    mcp_error = None
    try:
        mcp_settings = load_mcp_settings(mcp_path)
    except ValueError:
        mcp_settings = MCPSettings(git_enabled=False, github_enabled=False)
        mcp_error = "后台工具配置损坏，请恢复最近的配置备份后重试。"
    project_directory = (project_directory or Path.cwd()).absolute()
    active_settings, startup_status = resolve_connections(mcp_settings, env_file, project_directory)
    automatic_repositories = set(active_settings.git_repositories) - set(mcp_settings.git_repositories)
    async with client, WebTools(search_settings, env_file, on_paid_web, authorization=authorization) as web, \
            MCPTools(active_settings, env_file, authorization, allow_clone=mcp_settings.git_enabled is None,
                     confirm_destinations=mcp_settings.git_write_enabled is None) as mcp:
        mcp.status.update(startup_status)
        try:
            builtin_registry, act_web = build_local_registry(web, evidence, env_file, search_settings)
            # Built-ins and MCP tools are registered separately, then share one execution gateway.
            tool_registry = dict(builtin_registry)
            session = DeepSeekSession(
                client, question, cleaned_history, instructions,
                [registration.schema for registration in tool_registry.values()],
                on_stream=on_stream,
                on_thinking=on_thinking,
                model=settings.default_model,
            )

            gateway = ToolGateway(authorization, tool_registry, on_permission, policies=tool_policies)
            def save_repositories(repositories):
                nonlocal mcp_settings
                if load_mcp_settings(mcp_path) != mcp_settings:
                    raise ValueError("MCP configuration changed during execution")
                saved = tuple(p for p in repositories if p not in automatic_repositories)
                mcp_settings = save_mcp_settings(replace(mcp_settings, git_repositories=saved), mcp_path)
            mcp.on_repositories_changed = save_repositories
            async def reconnect_mcp(settings, *, directory=None):
                nonlocal mcp_settings, automatic_repositories, active_settings, project_directory
                if directory is not None:
                    project_directory = directory
                await mcp.__aexit__(None, None, None)
                for name in list(tool_registry):
                    if name.startswith(("mcp_git__", "mcp_github__", "github_repository_", "github_branch_", "github_collaborators", "workspace_")):
                        tool_registry.pop(name)
                active_settings, status = resolve_connections(settings, env_file, project_directory)
                automatic_repositories = set(active_settings.git_repositories) - set(settings.git_repositories)
                mcp.__init__(active_settings, env_file, authorization, allow_clone=settings.git_enabled is None,
                             confirm_destinations=settings.git_write_enabled is None)
                mcp.on_repositories_changed = save_repositories
                mcp_settings = settings
                await mcp.__aenter__()
                mcp.status.update(status)
                tool_registry.update(mcp.registry)
                tool_registry.update(GitHubAdmin(active_settings, env_file, authorization).registrations())
                tool_registry.update(WorkspaceTools(mcp, env_file).registrations())
                session.tools = gateway.available_schemas()
            # Internal settings are not a step in ordinary coding tasks.
            if allow_backend_configuration:
                tool_registry.update(mcp_configuration_tools(mcp_path, reconnect_mcp))
            tool_registry.update(project_access_tools(mcp_path, reconnect_mcp, lambda: project_directory))

            async def act(action: ToolAction) -> Observation:
                result = await gateway.execute(action)
                if action.name.startswith(("mcp_github__", "mcp_git__")):
                    result = Observation(action, evidence.observe(action.name, action.arguments, result.result))
                session.observe(action, json.dumps(result.result, ensure_ascii=False))
                return result

            if mcp_error:
                mcp.status["configuration"] = {"state": "invalid", "message": mcp_error}
            async def act_mcp_status(action):
                info = mcp.information()
                policies = gateway.policy_information()
                info["tool_policies"] = policies
                info["tools"] = [name for name in info["tools"] if policies[name]["enabled"]]
                modifications = [name for name, entry in tool_registry.items()
                    if entry.approval_description is not None and policies[name]["enabled"]
                    and name.startswith(("mcp_git__", "mcp_github__", "workspace_", "github_"))]
                info["read_only"] = not modifications
                info["git_repository_writes_enabled"] = any(name.startswith("mcp_git__") and name != "mcp_git__git_clone" for name in modifications)
                info["workspace_writes_enabled"] = any(name.startswith("workspace_") for name in modifications)
                for prefix, key in (("mcp_git__", "git_writes_require_approval"), ("mcp_github__", "github_writes_require_approval")):
                    info[key] = any(policies[name]["requires_approval"] for name in modifications if name.startswith(prefix))
                info["clone_destination_requires_approval"] = "mcp_git__git_clone" in modifications and policies["mcp_git__git_clone"]["requires_approval"]
                info["creation_destination_requires_approval"] = any(policies[name]["requires_approval"] for name in modifications
                    if name in {"mcp_git__git_clone", "mcp_git__git_init", "mcp_git__git_worktree_add"})
                info["configuration_file"] = str(mcp_path)
                info["configuration_exists"] = mcp_path.exists()
                info["project_directory"] = str(project_directory)
                info["connection_modes"] = {"git": "automatic" if mcp_settings.git_enabled is None else mcp_settings.git_enabled,
                                            "github": "automatic" if mcp_settings.github_enabled is None else mcp_settings.github_enabled}
                return Observation(action, info)
            tool_registry["mcp_status"] = ToolRegistration(MCP_STATUS_TOOL, "permissions.read", act_mcp_status)
            tool_registry.update(mcp.registry)
            tool_registry.update(GitHubAdmin(active_settings, env_file, authorization).registrations())
            tool_registry.update(WorkspaceTools(mcp, env_file).registrations())
            session.tools = gateway.available_schemas()
            await apply_permission_intent(question, client, settings.default_model, web,
                authorization, on_permission, env_file, tool_registry, act_web, session)
            session.tools = gateway.available_schemas()
            # Use the shared ReAct limit, independently of individual tool budgets.
            answer = await run_loop(session.think, act)
        finally:
            authorization.close()
        checked_answer = evidence.annotate_answer(answer)
        if checked_answer != answer:
            answer = checked_answer
            session.messages[-1]["content"] = answer
            if on_stream is not None:
                on_stream(answer)
    if history is not None:
        turn_messages = session.messages[len(cleaned_history) + 1:]
        if (not turn_messages or turn_messages[-1].get("role") != "assistant"
                or turn_messages[-1].get("content") != answer):
            turn_messages = [{"role": "user", "content": question},
                             {"role": "assistant", "content": answer}]
        history.extend(turn_messages)
    return answer
