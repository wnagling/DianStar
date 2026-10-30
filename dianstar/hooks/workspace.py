"""Workspace write checks and bounded observation after command execution."""
import hashlib
import os
from pathlib import Path

from dianstar.security.content_guard import MAX_FILE, MAX_FILES, MAX_TOTAL, SecurityViolation, check_content, local_configuration
from .registry import HookRegistration


def before_write(context, path, data):
    return check_content(context.scanner, path, data,
                         allow_local=context.local_only and local_configuration(path))


def after_command(context, path, data):
    return check_content(context.scanner, path, data)


def registrations():
    return (
        HookRegistration("workspace.content", ("before_write",), before_write, priority=0),
        HookRegistration("workspace.audit", ("after_command",), after_command, priority=0),
    )


class WorkspaceAudit:
    SKIP = {".git", ".local", ".ssh", ".aws", "node_modules", ".venv", "__pycache__", "build", "dist", "target"}

    def __init__(self, root, hooks, protected=()):
        self.root, self.hooks, self.protected = root, hooks, tuple(protected)

    def capture(self, *, content=False):
        entries, total, complete = {}, 0, True
        try:
            paths = []
            for parent, directories, files in os.walk(self.root):
                directories[:] = sorted(name for name in directories if name not in self.SKIP
                    and (Path(parent) / name).resolve() == (Path(parent) / name).absolute())
                for name in files:
                    paths.append((Path(parent) / name, (Path(parent) / name).relative_to(self.root).as_posix()))
                    if len(paths) > MAX_FILES:
                        raise SecurityViolation(incomplete=True)
            paths.extend((path, ".local/[应用配置]/" + path.name) for path in self.protected)
            for path, label in paths:
                if not path.exists():
                    continue
                if path.resolve() != path.absolute() or not path.is_file():
                    complete = False
                    continue
                size = path.stat().st_size
                total += size
                if size > MAX_FILE or total > MAX_TOTAL:
                    complete = False
                    continue
                data = path.read_bytes()
                entries[label] = (hashlib.sha256(data).hexdigest(), data if content else b"")
        except (OSError, ValueError):
            complete = False
        return entries, complete

    def review(self, before):
        after, complete = self.capture(content=True)
        changed = [(path, item[1]) for path, item in after.items()
                   if path not in before[0] or before[0][path][0] != item[0]]
        # A removed local configuration is also an operation worth reporting.
        changed += [(path, b"") for path in before[0] if path not in after]
        try:
            report = self.hooks.run("after_command", changed)
            report["complete"] = bool(report["complete"] and complete and before[1])
        except SecurityViolation as error:
            report = {"event": "after_command", "complete": False, **error.result()}
        report["scope"] = "项目文件及应用配置；不包含依赖目录、Git 元数据和其他仓库外文件。"
        if not report["complete"] or report.get("findings"):
            report["message"] = "命令已经运行，检查发现风险或未完整完成；未自动删除文件、回滚修改或撤销网络操作。"
        return report
