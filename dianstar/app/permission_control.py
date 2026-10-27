"""Application-owned permission changes, separate from research tools."""

from dianstar.core.types import Observation, ToolAction
from dianstar.config.search import load_search_settings
from dianstar.security.gateway import ToolGateway
from dianstar.core.registry import ToolRegistration
from dianstar.security.permission_intent import resolve_intent, ACTION_LABELS
from dianstar.tools.local.web import MAX_PAID_WEB_CALLS
from dianstar.tools.local.web_access import WEB_ACCESS_TOOL, manage_web_access


from dianstar.tools.local.registry import update_web_tools


async def apply_permission_intent(question, client, model, web, authorization,
                                  on_permission, env_file, tool_registry, act_web, session):
    intent = await resolve_intent(question, client, model)
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
                    update_web_tools(tool_registry, act_web, web.settings)
                    session.tools = [entry.schema for entry in tool_registry.values()]
        else:
            control_result = {"error": "permission_change_not_confirmed",
                "message": "权限变更未确认，原权限保持不变。"}
        session.add_control_result(intent.action, control_result)
