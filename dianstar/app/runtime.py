# 编排一轮提问：调用模型与工具，并更新成功的对话历史。

import json
from pathlib import Path
from typing import Awaitable, Callable

from dianstar.config.models import load_model_settings
from dianstar.providers.deepseek import ENV_FILE, DeepSeekSession, create_client, usable_history
from dianstar.core.types import Observation, ToolAction
from dianstar.core.loop import run_loop
from dianstar.prompts.builder import build_system_prompt
from dianstar.config.search import load_search_settings
from dianstar.evidence.source_evidence import SourceEvidence
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.core.registry import ToolRegistration
from dianstar.tools.mcp.client import MCPTools
from dianstar.tools.mcp.schemas import MCP_STATUS_TOOL
from dianstar.config.mcp import MCPSettings, load_mcp_settings
from dianstar.tools.local.web import MAX_WEB_OPERATIONS, MAX_PAID_WEB_CALLS, WebTools


from dianstar.tools.local.registry import build_local_registry
from dianstar.app.permission_control import apply_permission_intent


async def run_turn(
    question: str,
    *,
    history: list[dict[str, object]] | None = None,
    env_file: Path = ENV_FILE,
    on_stream: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_paid_web: Callable[[dict], Awaitable[str]] | None = None,
    on_permission: Callable[[dict], Awaitable[bool]] | None = None,
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
        mcp_settings = MCPSettings()
        mcp_error = "MCP 配置无效，请在 /config 的 MCP 页面检查并保存。"
    async with client, WebTools(search_settings, env_file, on_paid_web, authorization=authorization) as web, \
            MCPTools(mcp_settings, env_file, authorization) as mcp:
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

            gateway = ToolGateway(authorization, tool_registry, on_permission)

            async def act(action: ToolAction) -> Observation:
                result = await gateway.execute(action)
                if action.name.startswith("mcp_github__"):
                    result = Observation(action, evidence.observe(action.name, action.arguments, result.result))
                session.observe(action, json.dumps(result.result, ensure_ascii=False))
                return result

            if mcp_error:
                mcp.status["configuration"] = {"state": "invalid", "message": mcp_error}
            if mcp_path.exists():
                async def act_mcp_status(action):
                    return Observation(action, mcp.information())
                tool_registry["mcp_status"] = ToolRegistration(MCP_STATUS_TOOL, "permissions.read", act_mcp_status)
                tool_registry.update(mcp.registry)
                session.tools = [entry.schema for entry in tool_registry.values()]
            await apply_permission_intent(question, client, settings.default_model, web,
                authorization, on_permission, env_file, tool_registry, act_web, session)
            # 允许逐页阅读到网页操作上限后，再留出决策空间生成最终回答。
            steps = MAX_WEB_OPERATIONS + MAX_PAID_WEB_CALLS + 4
            answer = await run_loop(session.think, act, max_steps=steps)
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
