"""Bounded end-of-turn retention, using the same gateway as model tools."""
import uuid

from dianstar.core.types import ToolAction
from dianstar.tools.local.files.io import blocking


async def finish_storage(workspace, gateway, log):
    manager = workspace.storage.manager
    warnings = await blocking(lambda cancel: manager.close())
    if warnings:
        await blocking(lambda cancel: log.append({"event": "storage_release_incomplete", "ids": warnings}))
    name = "workspace_storage_cleanup"
    entry = gateway.registry.get(name)
    if entry is None or not gateway.authorization.active:
        return
    policy = gateway.policy_for(name, entry)
    if (not policy.enabled or gateway.authorization.is_denied(entry.capability)
            or policy.requires_approval and not gateway.uses_default_approval(name, entry)):
        return  # Background retention never opens an unsolicited approval dialog.
    if not await blocking(lambda cancel: bool(manager.cleanup_ids(1, small_only=True) or manager.abandoned())):
        return
    action = ToolAction(name, {"limit": 1, "small_only": True}, "retention_" + uuid.uuid4().hex)
    result = await gateway.execute(action)
    await blocking(lambda cancel: log.append({"event": "storage_maintenance", "call_id": action.call_id,
        "tool": action.name, "result": result.result}))
