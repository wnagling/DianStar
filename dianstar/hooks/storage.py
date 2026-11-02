"""Local recovery metadata checks; file bodies are never published or returned."""
from dianstar.security.content_guard import Finding, protected_path
from .registry import HookRegistration


def metadata(context, path, data):
    if protected_path(path):
        return [Finding(context.scanner.redact(path), 0, "受保护路径不能单独移入回收站或覆盖恢复")]
    return context.scanner.scan(path + ".json", data)


def registrations():
    return (HookRegistration("storage.metadata", ("before_trash", "before_storage_restore", "before_storage_purge", "before_scratch"), metadata),)
