"""The tool execution boundary: validate, prepare, approve and reserve before execution."""
import inspect
import json
from typing import Callable, Awaitable
from ..core.types import ToolAction, Observation
from ..core.registry import ToolRegistration
from .authorization import Authorization, request_key
from .validation import valid_arguments


class ToolPreparationError(Exception):
    """Trusted preflight failed before approval; no execution grant was made."""
    def __init__(self, code: str, message: str):
        self.result = {"error": code, "message": message}
        super().__init__(message)


class ToolGateway:
    def __init__(self, authorization: Authorization, registry: dict[str, ToolRegistration],
                 request_approval: Callable[[dict], Awaitable[bool]] | None = None):
        self.authorization = authorization
        self.registry = registry
        self.request_approval = request_approval
        self._declined: set[str] = set()

    async def execute(self, action: ToolAction) -> Observation:
        registration = self.registry.get(action.name)
        if (registration is None or not registration.capability
                or registration.schema["function"]["name"] != action.name):
            return Observation(action, {"error": "tool_not_authorized", "message": "工具未注册或未声明权限，未执行。"})
        if not valid_arguments(registration.schema, action.arguments):
            return Observation(action, {"error": "invalid_tool_arguments", "message": "工具参数不符合定义，未执行。"})
        # Freeze the checked arguments before any await; handlers receive this copy.
        checked = json.loads(json.dumps(action.arguments, ensure_ascii=False))
        error = self.authorization.reserve(registration.capability, action.name, checked)
        binding = request_key(action.name, checked)
        if (error and error["error"] == "authorization_required"
                and registration.approval_description is not None and self.request_approval is not None
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
            approved = await self.request_approval({"request_id": self.authorization.request_id,
                "description": description})
            # A cancellation/revocation may occur while the dialog is open.
            if self.registry.get(action.name) is not registration:
                return Observation(action, {"error": "tool_authorization_changed", "message": "工具权限已改变，请重新申请；未执行。"})
            if not self.authorization.active:
                return Observation(action, {"error": "authorization_expired", "message": "本轮授权已结束，未执行。"})
            if self.authorization.revision(registration.capability) != revision:
                return Observation(action, {"error": "authorization_changed", "message": "等待确认期间权限已撤销或改变，未执行。"})
            if approved is True and self.authorization.active:
                self.authorization.grant(registration.capability, name=action.name, arguments=checked)
                error = self.authorization.reserve(registration.capability, action.name, checked)
            else:
                self._declined.add(binding)
        if error:
            return Observation(action, error)
        return await registration.handler(ToolAction(action.name, checked, action.call_id, action.raw_arguments))
