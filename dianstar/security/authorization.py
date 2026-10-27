"""Application-owned, scoped grants; model and tool text are never grants."""
import json
import uuid
from dataclasses import dataclass


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
