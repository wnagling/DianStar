# 编排一轮提问：调用模型与工具，并更新成功的对话历史。

import json
from pathlib import Path
from typing import Awaitable, Callable

from .config import load_model_settings
from .deepseek import ENV_FILE, DeepSeekSession, create_client, usable_history
from .loop import Observation, ToolAction, run_loop
from .prompts import build_system_prompt
from .search_config import load_search_settings
from .source_evidence import SourceEvidence
from .permissions import Authorization, ToolGateway, ToolRegistration
from .permission_intent import resolve_intent, ACTION_LABELS
from .tools.web import FETCH_TOOL, SEARCH_TOOL, MAX_WEB_OPERATIONS, MAX_PAID_WEB_CALLS, WebTools
from .tools.web_access import WEB_ACCESS_TOOL, WEB_ACCESS_STATUS_TOOL, manage_web_access
from .tools.date_calculator import DATE_TOOL, DateCalculationError, calculate_date
from .tools.number_calculator import NUMBER_TOOL, NumberCalculationError, calculate_number
from .tools.weather import (
    WEATHER_TOOL, WeatherDateInvalid, WeatherLocationAmbiguous, WeatherLocationMismatch,
    WeatherLocationNotFound,
    WeatherUnavailable, get_weather,
)
from .tools.weather_contract import weather_data


async def ask_deepseek(
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
    def observe_error(action: ToolAction, code: str) -> Observation:
        result = {"error": code, "requested_location": action.arguments["location"],
                  "requested_date": action.arguments["date"]}
        return Observation(action, result)

    async def act_weather(action: ToolAction) -> Observation:
        try:
            result = await get_weather(action.arguments["location"], action.arguments["date"])
        except WeatherLocationAmbiguous:
            return observe_error(action, "location_ambiguous")
        except WeatherDateInvalid:
            return observe_error(action, "invalid_date")
        except WeatherLocationMismatch:
            return observe_error(action, "location_mismatch")
        except WeatherLocationNotFound:
            return observe_error(action, "location_not_found")
        except WeatherUnavailable:
            return observe_error(action, "weather_unavailable")
        return Observation(action, json.loads(weather_data(result)))

    async def act_date(action: ToolAction) -> Observation:
        try:
            result = calculate_date(**action.arguments)
        except DateCalculationError as error:
            result = {"error": error.code, "message": str(error), "requested": action.arguments}
        return Observation(action, result)

    async def act_number(action: ToolAction) -> Observation:
        try:
            result = calculate_number(**action.arguments)
        except NumberCalculationError as error:
            result = {"error": error.code, "message": str(error), "requested": action.arguments}
        return Observation(action, result)

    async def act_web(action: ToolAction) -> Observation:
        result = await web.execute(action.name, action.arguments)
        result = evidence.observe(action.name, action.arguments, result)
        return Observation(action, result)

    async def act_web_access(action: ToolAction) -> Observation:
        # Defense in depth: this handler cannot forward a mutation argument.
        result = await manage_web_access("status", env_file=env_file)
        return Observation(action, result)

    # 同一份注册表绑定模型可见的工具定义和实际执行函数。
    tool_registry = {
        WEATHER_TOOL["function"]["name"]: ToolRegistration(WEATHER_TOOL, "weather.read", act_weather),
        DATE_TOOL["function"]["name"]: ToolRegistration(DATE_TOOL, "calculate", act_date),
        NUMBER_TOOL["function"]["name"]: ToolRegistration(NUMBER_TOOL, "calculate", act_number),
        WEB_ACCESS_TOOL["function"]["name"]: ToolRegistration(WEB_ACCESS_STATUS_TOOL, "permissions.read", act_web_access),
    }
    if search_settings.provider != "disabled":
        tool_registry.update({schema["function"]["name"]: ToolRegistration(schema,
                              "web.search" if schema is SEARCH_TOOL else "web.read", act_web)
                              for schema in (SEARCH_TOOL, FETCH_TOOL)})
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
        session.observe(action, json.dumps(result.result, ensure_ascii=False))
        return result

    async with client, WebTools(search_settings, env_file, on_paid_web, authorization=authorization) as web:
        try:
            intent = await resolve_intent(question, client, settings.default_model)
            if intent is not None:
                # Restoring ask from deny is an expansion, even when the phrase
                # looks like a revocation. Decide against actual current state.
                requires_confirmation = intent.needs_confirmation or (
                    intent.action == "ask" and web.settings.paid_policy == "deny")
                confirmed = not requires_confirmation
                if requires_confirmation and on_permission is not None:
                    current_policy = {"ask": "每轮询问", "allow": "长期允许，可能产生费用", "deny": "禁止付费"}[web.settings.paid_policy]
                    confirmed = await on_permission({"action": intent.action,
                        "description": ACTION_LABELS[intent.action] + "\n当前付费策略：" + current_policy,
                        "request_id": authorization.request_id}) is True
                if confirmed:
                    if intent.action == "turn":
                        # A direct turn grant cannot override an existing deny.
                        if web.settings.paid_policy == "deny":
                            control_result = {"error": "paid_web_denied", "message": "当前长期策略禁止付费；本轮批准未覆盖禁止策略。"}
                        else:
                            authorization.grant("web.paid", uses=MAX_PAID_WEB_CALLS)
                            web._turn_approval = True
                            control_result = {"saved": False, "paid_approved": True, "scope": "本次提问"}
                    else:
                        control_action = ToolAction("manage_web_access", {"action": intent.action})
                        authorization.grant("permissions.change", name=control_action.name, arguments=control_action.arguments)
                        # A separate registry prevents the research model from
                        # seeing or using the mutation schema, even after a read.
                        async def control_handler(action):
                            async def approved_persistent(_request):
                                return "always"  # Exact change already confirmed above.
                            result = await manage_web_access(action.arguments["action"], env_file=env_file,
                                request_approval=approved_persistent if confirmed else None)
                            return Observation(action, result)
                        control_gateway = ToolGateway(authorization, {"manage_web_access":
                            ToolRegistration(WEB_ACCESS_TOOL, "permissions.change", control_handler)})
                        control_result = (await control_gateway.execute(control_action)).result
                        if control_result.get("saved"):
                            web.settings = load_search_settings(env_file.parent / ".local" / "search.json")
                            authorization.revoke("web.paid")
                            web._turn_approval = None
                            for schema in (SEARCH_TOOL, FETCH_TOOL):
                                name = schema["function"]["name"]
                                if web.settings.provider == "disabled":
                                    tool_registry.pop(name, None)
                                else:
                                    tool_registry[name] = ToolRegistration(schema,
                                        "web.search" if name == "search_web" else "web.read", act_web)
                            session.tools = [entry.schema for entry in tool_registry.values()]
                else:
                    control_result = {"error": "permission_change_not_confirmed",
                        "message": "权限变更未确认，原权限保持不变。"}
                session.add_control_result(intent.action, control_result)
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
