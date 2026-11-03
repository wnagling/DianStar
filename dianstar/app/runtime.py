# 编排一轮提问：调用模型与工具，并更新成功的对话历史。

import json
import asyncio
import uuid
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
from dianstar.app.project_context import create_project_context
from dianstar.tools.local.github_admin import GitHubAdmin
from dianstar.tools.local.workspace import WorkspaceTools
from dianstar.tools.local.files import FILE_TOOL_NAMES, migrate_file_policies
from dianstar.core.scheduler import BatchExecutor
from dianstar.security.content_guard import ContentScanner, known_credentials
from dianstar.app.turn_log import TurnLog
from dianstar.app.change_review import ChangeReview
from dianstar.app.tool_resources import ToolResources
from dianstar.app.task_scope import TaskScope, task_git_defaults
from dianstar.app.tool_events import ToolEvents
from dianstar.app.storage_lifecycle import finish_storage
from dianstar.tools.local.files.io import blocking


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
    on_tool: Callable[[dict], None] | None = None,
    conversation_id: str | None = None,
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
    task_scope = TaskScope.from_question(question, project_directory)
    active_settings, startup_status = resolve_connections(mcp_settings, env_file, project_directory)
    automatic_repositories = set(active_settings.git_repositories) - set(mcp_settings.git_repositories)
    project_config_path = env_file.parent / ".local/projects.json"
    projects, project_status = create_project_context(authorization, project_directory, project_config_path,
        legacy=mcp_settings, legacy_invalid=mcp_error is not None)

    approval_lock = asyncio.Lock()
    async def confirm(request):
        async with approval_lock:
            return await on_permission(request)
    async def paid(request):
        async with approval_lock:
            return await on_paid_web(request)
    log = None
    workspace = None
    async with client, WebTools(search_settings, env_file, paid if on_paid_web else None, authorization=authorization) as web, \
            MCPTools(active_settings, env_file, authorization, allow_clone=mcp_settings.git_enabled is None,
                     confirm_destinations=mcp_settings.git_write_enabled is None) as mcp:
        mcp.status.update(startup_status)
        try:
            log = TurnLog(authorization.request_id, ContentScanner(known=known_credentials(env_file,
                extra=(mcp.github.token(),) if getattr(mcp, "github", None) else ())), conversation_id=conversation_id)
            projects.credentials = lambda: (mcp.github.token(),) if getattr(mcp, "github", None) else ()
            workspace = WorkspaceTools(projects, env_file, conversation_id=conversation_id, task_scope=task_scope)
            local_settings = projects.settings
            local_registrations = workspace.registrations()
            review = ChangeReview(lambda: workspace, log)
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

            gateway = ToolGateway(authorization, tool_registry, confirm if on_permission else None, policies=migrate_file_policies(tool_policies))
            def save_repositories(repositories):
                for repository in repositories:
                    try:
                        projects.include(repository)
                    except (ValueError, OSError):
                        pass
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
                    if name.startswith(("mcp_git__", "mcp_github__", "github_repository_", "github_branch_", "github_collaborators")):
                        tool_registry.pop(name)
                active_settings, status = resolve_connections(settings, env_file, project_directory)
                automatic_repositories = set(active_settings.git_repositories) - set(settings.git_repositories)
                mcp.__init__(active_settings, env_file, authorization, allow_clone=settings.git_enabled is None,
                             confirm_destinations=settings.git_write_enabled is None)
                mcp.on_repositories_changed = save_repositories
                mcp_settings = settings
                await mcp.__aenter__()
                mcp.status.update(status)
                tool_registry.update(task_git_defaults(mcp.registry, task_scope))
                tool_registry.update(GitHubAdmin(active_settings, env_file, authorization).registrations())
                workspace.refresh_credentials()
                session.tools = gateway.available_schemas()
            # Internal settings are not a step in ordinary coding tasks.
            if allow_backend_configuration:
                tool_registry.update(mcp_configuration_tools(mcp_path, reconnect_mcp))
            async def opened_project(directory):
                nonlocal project_directory, project_status, local_settings, local_registrations
                project_directory = directory
                project_status = {}
                if (projects.settings.enabled, projects.settings.write_enabled) != (local_settings.enabled, local_settings.write_enabled):
                    for name in local_registrations:
                        tool_registry.pop(name, None)
                    local_settings = projects.settings
                    local_registrations = workspace.registrations()
                    tool_registry.update(local_registrations)
                # Local reads/writes retain their snapshots and registration
                # while optional version-control connections are refreshed.
                try:
                    await reconnect_mcp(mcp_settings, directory=directory)
                except Exception:
                    session.tools = gateway.available_schemas()
                    return "本地项目已打开，可选 Git/GitHub 连接未完成；本地工具仍可使用。"
            tool_registry.update(project_access_tools(project_config_path, projects, opened_project,
                lambda: project_directory, question=question))

            async def act(action: ToolAction) -> Observation:
                result = await gateway.execute(action)
                if action.name.startswith(("mcp_github__", "mcp_git__")):
                    result = Observation(action, evidence.observe(action.name, action.arguments, result.result))
                return result

            started_registrations = {}
            tool_events = ToolEvents(log.scanner, authorization.request_id, conversation_id)
            def notify(event):
                if on_tool:
                    try:
                        on_tool(event)
                    except Exception:
                        pass
            async def started(action):
                started_registrations[action.call_id] = tool_registry.get(action.name)
                await blocking(lambda cancel: log.append({"event": "started", "call_id": action.call_id,
                    "tool": action.name, "arguments": action.arguments}))
                event = tool_events.started(action)
                notify({**event, "activity": event["title"]})

            async def completed(observation):
                review.observe(observation, started_registrations.get(observation.action.call_id))
                await blocking(lambda cancel: log.append({"event": "completed", "call_id": observation.action.call_id,
                    "tool": observation.action.name, "result": observation.result}))
                notify(tool_events.completed(observation))

            resources = ToolResources(tool_registry, lambda: workspace, mcp)
            executor = BatchExecutor(tool_registry, act, on_started=started, on_completed=completed, resources=resources)
            async def publish_review(remaining=None):
                entry = tool_registry.get("review_changes")
                if entry is None or not gateway.policy_for("review_changes", entry).enabled:
                    return False
                action = ToolAction("review_changes", {}, "review_" + uuid.uuid4().hex, "{}")
                await started(action)
                result = await act(action)
                await completed(result)
                session.application_result(action, {"origin": "application_change_review", "remaining_decisions": remaining,
                    **result.result})
                return True

            async def execute_batch(actions):
                generation = review.generation
                await blocking(lambda cancel: log.append({"event": "batch", "calls": [
                    {"call_id": action.call_id, "tool": action.name, "arguments": action.arguments} for action in actions]}))
                try:
                    results = await executor(actions)
                finally:
                    for observation in executor.last_results:
                        session.observe(observation.action, json.dumps(observation.result, ensure_ascii=False))
                if review.generation != generation:
                    await publish_review()
                return results

            async def before_finish(proposed, remaining):
                if await review.ready():
                    return True
                entry = tool_registry.get("review_changes")
                if entry is None or not gateway.policy_for("review_changes", entry).enabled:
                    return True  # Honest unverified footer; never bypass a denied read.
                session.reject_final(proposed)
                await publish_review(remaining)
                return False

            budget_exhausted = False
            async def exhausted(observations):
                nonlocal budget_exhausted
                budget_exhausted = True
                await blocking(lambda cancel: log.finish("budget_exhausted"))
                return "本轮已达到 16 次模型决策上限，未额外调用模型。已执行结果和恢复编号已保存。\n\n" + (review.summary() or f"已记录 {len(observations)} 项工具结果；任务记录：{log.id}。")

            if mcp_error:
                mcp.status["configuration"] = {"state": "invalid", "message": mcp_error}
            async def act_mcp_status(action):
                info = mcp.information()
                policies = gateway.policy_information()
                info["tool_policies"] = policies
                info["tools"] = [name for name in tool_registry if policies[name]["enabled"]]
                modifications = [name for name, entry in tool_registry.items()
                    if entry.approval_description is not None and policies[name]["enabled"]
                    and (name in FILE_TOOL_NAMES or name.startswith(("mcp_git__", "mcp_github__", "workspace_", "github_")))]
                info["read_only"] = not modifications
                info["git_repository_writes_enabled"] = any(name.startswith("mcp_git__") and name != "mcp_git__git_clone" for name in modifications)
                info["workspace_writes_enabled"] = any(name in FILE_TOOL_NAMES or name.startswith("workspace_") for name in modifications)
                for prefix, key in (("mcp_git__", "git_writes_require_approval"), ("mcp_github__", "github_writes_require_approval")):
                    info[key] = any(policies[name]["requires_approval"] for name in modifications if name.startswith(prefix))
                info["clone_destination_requires_approval"] = "mcp_git__git_clone" in modifications and policies["mcp_git__git_clone"]["requires_approval"]
                info["creation_destination_requires_approval"] = any(policies[name]["requires_approval"] for name in modifications
                    if name in {"mcp_git__git_clone", "mcp_git__git_init", "mcp_git__git_worktree_add"})
                info["configuration_file"] = str(mcp_path)
                info["configuration_exists"] = mcp_path.exists()
                info["project_directory"] = str(project_directory)
                info["local_project"] = {"roots": sorted(projects.roots), "enabled": projects.settings.enabled,
                    "write_enabled": projects.settings.write_enabled, "configuration_file": str(project_config_path),
                    "status": project_status}
                info["tool_groups"] = {"local": [name for name in info["tools"] if not name.startswith(("mcp_git__", "mcp_github__"))],
                    "git_mcp": [name for name in info["tools"] if name.startswith("mcp_git__")],
                    "github_mcp": [name for name in info["tools"] if name.startswith("mcp_github__")]}
                info["scratch_workspaces"] = list(workspace.storage.manager.scratch_roots)
                info["connection_modes"] = {"git": "automatic" if mcp_settings.git_enabled is None else mcp_settings.git_enabled,
                                            "github": "automatic" if mcp_settings.github_enabled is None else mcp_settings.github_enabled}
                return Observation(action, info)
            tool_registry["mcp_status"] = ToolRegistration(MCP_STATUS_TOOL, "permissions.read", act_mcp_status)
            tool_registry["tool_status"] = ToolRegistration({"type": "function", "function": {"name": "tool_status",
                "description": "查看本地项目、文件、命令及可选外部工具的实际状态与后台策略；只查询，不开启权限或修改连接。",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
                "permissions.read", act_mcp_status, parallel_safe=True)
            tool_registry.update(task_git_defaults(mcp.registry, task_scope))
            tool_registry.update(GitHubAdmin(active_settings, env_file, authorization).registrations())
            tool_registry.update(local_registrations)
            tool_registry.update(review.registrations())
            session.tools = gateway.available_schemas()
            previous = log.previous()
            if previous:
                session.application_result(ToolAction("task_status", {}, "previous_" + uuid.uuid4().hex), {
                    "origin": "application_task_history", "previous_tasks": previous,
                    "scope": "同会话的历史未完成记录，仅供核对；不授权重做操作，也不要求本轮发布运行测试。"})
            await apply_permission_intent(question, client, settings.default_model, web,
                authorization, confirm if on_permission else None, env_file, tool_registry, act_web, session)
            session.tools = gateway.available_schemas()
            # Use the shared ReAct limit, independently of individual tool budgets.
            answer = await run_loop(session.think, act, execute_batch=execute_batch,
                                    before_finish=before_finish, on_exhausted=exhausted)
            summary = review.summary()
            if summary and summary not in answer:
                answer += "\n\n" + summary
            session.finish_text(answer)
            try:
                await finish_storage(workspace, gateway, log)
            except Exception:
                # Retention failure preserves data and cannot turn a completed
                # coding task into a retry of its external side effects.
                await blocking(lambda cancel: log.append({"event": "storage_maintenance_incomplete",
                    "message": "到期维护未完成，副本继续保留。"}))
            await blocking(lambda cancel: log.finish("budget_exhausted" if budget_exhausted else
                "reviewed" if review.assessment and review.assessment["decision"] == "keep" else
                "completed" if review.net_unchanged else
                "unverified" if review.receipts or review.command_changes else "completed"))
        except BaseException as error:
            if log is not None:
                log.finish("cancelled" if isinstance(error, asyncio.CancelledError) else "failed")
            raise
        finally:
            if workspace is not None:
                await blocking(lambda cancel: workspace.storage.manager.close())
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
