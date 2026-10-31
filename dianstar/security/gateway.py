"""The tool execution boundary: validate, prepare, approve and reserve before execution."""
import inspect
import json
from types import MappingProxyType
from typing import Callable, Awaitable, Mapping
from ..core.types import ToolAction, Observation
from ..core.registry import ToolRegistration
from .authorization import Authorization, request_key
from .validation import valid_arguments
from .tool_policy import ToolPolicy
from .approval_summary import approval_summary


class ToolPreparationError(Exception):
    """Trusted preflight failed before approval; no execution grant was made."""
    def __init__(self, code: str, message: str, *, findings=None, source=None):
        self.result = {"error": code, "message": message}
        if findings:
            self.result["findings"] = findings[:30]
        if source is not None:
            self.result["source"] = source
        super().__init__(message)


class ToolGateway:
    def __init__(self, authorization: Authorization, registry: dict[str, ToolRegistration],
                 request_approval: Callable[[dict], Awaitable[bool]] | None = None,
                 *, policies: Mapping[str, ToolPolicy] | None = None):
        self.authorization = authorization
        self.registry = registry
        self.request_approval = request_approval
        checked_policies = dict(policies or {})
        if any(not isinstance(name, str) or not isinstance(policy, ToolPolicy)
               for name, policy in checked_policies.items()):
            raise ValueError("Invalid backend tool policy")
        self.policies = MappingProxyType(checked_policies)
        self._declined: set[str] = set()

    def policy_for(self, name, registration):
        # Exact-tool rules are more specific than capability rules.
        policy = self.policies.get(name, self.policies.get(registration.capability, ToolPolicy()))
        for dependency in registration.required_tools:
            entry = self.registry.get(dependency)
            if (entry is None or entry.required_tools
                    or not self.policies.get(dependency, self.policies.get(entry.capability, ToolPolicy())).enabled):
                return ToolPolicy(enabled=False, requires_approval=policy.requires_approval)
        return policy

    def available_schemas(self):
        return [entry.schema for name, entry in self.registry.items() if self.policy_for(name, entry).enabled]

    def uses_default_approval(self, name, registration):
        return name not in self.policies and registration.capability not in self.policies

    def policy_information(self):
        return {name: {"enabled": self.policy_for(name, entry).enabled,
                       "requires_approval": bool(entry.approval_description) and self.policy_for(name, entry).requires_approval,
                       **({"conditional_approval": True} if entry.auto_approve and self.uses_default_approval(name, entry) else {})}
                for name, entry in self.registry.items()}

    async def execute(self, action: ToolAction) -> Observation:
        registration = self.registry.get(action.name)
        if (registration is None or not registration.capability
                or registration.schema["function"]["name"] != action.name):
            return Observation(action, {"error": "tool_not_authorized", "message": "工具未注册或未声明权限，未执行。"})
        policy = self.policy_for(action.name, registration)
        if not policy.enabled:
            return Observation(action, {"error": "tool_disabled", "message": "当前工具策略不允许此操作，未执行。"})
        if not valid_arguments(registration.schema, action.arguments):
            return Observation(action, {"error": "invalid_tool_arguments", "message": "工具参数不符合定义，未执行。"})
        dependencies = [(name, self.registry[name], self.authorization.revision(self.registry[name].capability))
                        for name in registration.required_tools]
        if any(self.authorization.is_denied(entry.capability) for _, entry, _ in dependencies):
            return Observation(action, {"error": "authorization_denied", "message": "发布所需操作已被明确禁止，未执行。"})
        # Freeze the checked arguments before any await; handlers receive this copy.
        checked = json.loads(json.dumps(action.arguments, ensure_ascii=False))
        error = self.authorization.reserve(registration.capability, action.name, checked)
        binding = request_key(action.name, checked)
        if (error and error["error"] == "authorization_required"
                and registration.approval_description is not None
                and (self.request_approval is not None or not policy.requires_approval or registration.auto_approve is not None)
                and binding not in self._declined):
            revision = self.authorization.revision(registration.capability)
            try:
                description = registration.approval_description(dict(checked))
                if inspect.isawaitable(description):
                    description = await description
            except ToolPreparationError as error:
                return Observation(action, error.result)
            if not isinstance(description, str) or not description.strip():
                return Observation(action, {"error": "tool_preparation_failed"})
            if (not self.authorization.active or self.authorization.revision(registration.capability) != revision
                    or self.registry.get(action.name) is not registration):
                return Observation(action, {"error": "authorization_changed"})
            requires_approval = policy.requires_approval
            if registration.auto_approve is not None and self.uses_default_approval(action.name, registration):
                try:
                    automatic = registration.auto_approve(dict(checked))
                    if type(automatic) is not bool:
                        if inspect.iscoroutine(automatic):
                            automatic.close()
                        raise ValueError("Invalid automatic approval decision")
                except Exception:
                    return Observation(action, {"error": "tool_preparation_failed"})
                requires_approval = not automatic
            if requires_approval:
                if self.request_approval is None:
                    return Observation(action, error)
                approved = await self.request_approval({"request_id": self.authorization.request_id,
                    "description": description, "summary": approval_summary(action.name, checked, description)})
            else:
                # Trusted policies/classifiers can waive prompts, never preflight.
                approved = True
            # A cancellation/revocation may occur while the dialog is open.
            if self.registry.get(action.name) is not registration:
                return Observation(action, {"error": "tool_authorization_changed", "message": "工具权限已改变，请重新申请；未执行。"})
            if not self.authorization.active:
                return Observation(action, {"error": "authorization_expired", "message": "本轮授权已结束，未执行。"})
            if self.authorization.revision(registration.capability) != revision:
                return Observation(action, {"error": "authorization_changed", "message": "等待确认期间权限已撤销或改变，未执行。"})
            if any(self.registry.get(name) is not entry or self.authorization.revision(entry.capability) != revision
                   for name, entry, revision in dependencies):
                return Observation(action, {"error": "authorization_changed", "message": "计划中的操作权限已改变，未执行。"})
            if approved is True and self.authorization.active:
                self.authorization.grant(registration.capability, name=action.name, arguments=checked)
                error = self.authorization.reserve(registration.capability, action.name, checked)
            else:
                self._declined.add(binding)
        if error:
            return Observation(action, error)
        if any(self.registry.get(name) is not entry or self.authorization.revision(entry.capability) != revision
               for name, entry, revision in dependencies):
            return Observation(action, {"error": "authorization_changed", "message": "计划中的操作权限已改变，未执行。"})
        return await registration.handler(ToolAction(action.name, checked, action.call_id, action.raw_arguments))
