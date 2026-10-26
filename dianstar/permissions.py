"""Application-owned authorization. Tool text and model arguments are never grants.

This is an execution boundary, not an OS sandbox or a semantic relevance detector.
New capabilities are denied until the application supplies an explicit scoped grant.
"""

import json
import uuid
import inspect
from dataclasses import dataclass
from typing import Callable, Awaitable
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from .loop import ToolAction, Observation


def request_key(name: str, arguments: dict) -> str:
    return json.dumps([name, arguments], ensure_ascii=False, sort_keys=True, allow_nan=False)


@dataclass
class Grant:
    request_id: str
    capability: str
    binding: str | None
    remaining: int


class Authorization:
    """One object per user request. Never deserialize grants from chat history."""

    DEFAULT_LIMITS = {"calculate": 64, "weather.read": 30,
                      "web.search": 60, "web.read": 60, "permissions.read": 16}

    def __init__(self):
        self.request_id = uuid.uuid4().hex
        self._grants: list[Grant] = []
        self._used: dict[str, int] = {}
        self._closed = False
        self._denied: set[str] = set()
        self._revisions: dict[str, int] = {}
        # No arguments, page text, paths or credentials in this diagnostic log.
        self.events: list[dict] = []

    def grant(self, capability: str, *, name: str | None = None,
              arguments: dict | None = None, uses: int = 1) -> None:
        if (self._closed or not isinstance(capability, str) or not capability
                or type(uses) is not int or uses < 1 or (name is None) != (arguments is None)):
            raise ValueError("Invalid authorization grant")
        binding = request_key(name, arguments) if name is not None else None
        # Permission mutation must always be bound to the exact operation.
        if capability == "permissions.change" and binding is None:
            raise ValueError("Permission changes require exact arguments")
        self._grants.append(Grant(self.request_id, capability, binding, uses))

    def revoke(self, capability: str) -> None:
        self._grants = [grant for grant in self._grants if grant.capability != capability]
        self._revisions[capability] = self.revision(capability) + 1

    def revision(self, capability: str) -> int:
        return self._revisions.get(capability, 0)

    def deny(self, capability: str) -> None:
        self.revoke(capability)
        self._denied.add(capability)

    def has_grant(self, capability: str) -> bool:
        return not self._closed and capability not in self._denied and any(grant.capability == capability and grant.remaining > 0
                                       for grant in self._grants)

    @property
    def active(self) -> bool:
        return not self._closed

    def reserve(self, capability: str, name: str, arguments: dict) -> dict | None:
        """Check and consume before awaiting execution, including failed attempts."""
        code = "authorization_required"
        if capability in self._denied:
            code = "authorization_denied"
        elif self._closed:
            code = "authorization_expired"
        else:
            key = request_key(name, arguments)
            for grant in self._grants:
                if (grant.request_id == self.request_id and grant.capability == capability
                        and grant.binding in {None, key} and grant.remaining > 0):
                    grant.remaining -= 1
                    self.events.append({"tool": name, "capability": capability, "decision": "allow"})
                    return None
            limit = self.DEFAULT_LIMITS.get(capability)
            if limit is not None:
                used = self._used.get(capability, 0)
                if used < limit:
                    self._used[capability] = used + 1
                    self.events.append({"tool": name, "capability": capability, "decision": "allow"})
                    return None
                code = "authorization_limit"
        self.events.append({"tool": name, "capability": capability, "decision": "deny", "reason": code})
        return {"error": code, "message": "操作未获当前范围内的有效授权，或额度已用完；未执行。"}

    def close(self) -> None:
        self._closed = True
        self._grants.clear()


def valid_arguments(schema: dict, arguments: dict) -> bool:
    """Validate bounded JSON locally, never resolve a remote schema reference."""
    try:
        parameters = schema["function"]["parameters"]
        if (parameters.get("type") != "object" or parameters.get("additionalProperties") is not False
                or not isinstance(arguments, dict) or not safe_schema(parameters)):
            return False
        payload = json.dumps(arguments, allow_nan=False)
        if len(payload) > 32768:
            return False
        Draft202012Validator.check_schema(parameters)
        return Draft202012Validator(parameters, registry=Registry()).is_valid(arguments) and nonempty_strings(arguments)
    except (KeyError, TypeError, ValueError, RecursionError, SchemaError, Unresolvable):
        return False


def safe_schema(value, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#/")):
                return False
            if not safe_schema(item, depth + 1):
                return False
    elif isinstance(value, list):
        return all(safe_schema(item, depth + 1) for item in value)
    return True


def nonempty_strings(value, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return all(isinstance(key, str) and nonempty_strings(item, depth + 1) for key, item in value.items())
    if isinstance(value, list):
        return all(nonempty_strings(item, depth + 1) for item in value)
    return value is None or type(value) in {int, float, bool}


@dataclass(frozen=True)
class ToolRegistration:
    schema: dict
    capability: str
    handler: Callable[[ToolAction], Awaitable[Observation]]
    # Only trusted registrations opt into asking. Missing policies fail closed.
    approval_description: Callable[[dict], str | Awaitable[str]] | None = None


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
