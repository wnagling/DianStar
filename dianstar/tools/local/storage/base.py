"""Application-owned scopes and one ordered storage mutation gate."""
from contextlib import contextmanager, ExitStack
import errno
import json
import os
from pathlib import Path
import re
import time

from dianstar.platform.file_state import private_directory
from dianstar.security.file_locks import FileLocks, canonical
from .journal import StorageJournal, RETENTION
from .tree import StorageError, identity, snapshot
from dianstar.security.system_paths import check_host_path


class StorageBase:
    def __init__(self, context, *, journal=None):
        self.context = context
        self.journal = journal or StorageJournal()
        self.directory = Path(self.journal.directory).absolute()
        self.locks = FileLocks(self.directory)
        self.scratch_roots, self.scratch_projects, self.leases = {}, {}, {}

    def record(self, identifier, kind=None):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise StorageError("storage_not_found", "恢复编号格式不正确。")
        record = self.journal.get(identifier)
        if kind is not None and record["kind"] != kind:
            raise StorageError("storage_wrong_kind", "恢复编号与存储类型不匹配。")
        return record

    def checked(self, value):
        path = Path(value)
        if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path == path.parent
                or path.resolve() != path.absolute() or any(ord(char) < 32 for char in str(path))):
            raise StorageError("storage_path_denied", "请提供不经过链接的本机绝对路径。")
        if self.context.security.scanner.matches(str(path)):
            raise StorageError("storage_path_denied", "路径包含受保护内容，未操作。")
        if os.name == "nt" and any(part.endswith((".", " ")) or ":" in part for part in path.parts[1:]):
            raise StorageError("storage_path_denied", "不接受 Windows 路径别名或备用数据流。")
        return path

    def scope(self, value):
        self.context.projects.require()
        path = self.checked(value)
        check_host_path(path)
        for protected in (self.directory, self.context.env_file):
            protected = Path(protected).absolute()
            if path == protected or path.is_relative_to(protected) or protected.is_relative_to(path):
                raise StorageError("storage_path_denied", "不能回收应用自身配置、运行数据或包含它们的父目录。")
        if path in {Path.home(), Path.home() / "Desktop", Path.cwd()} or Path.cwd().is_relative_to(path):
            raise StorageError("storage_path_denied", "不能移动用户主目录、整个桌面或正在使用的工作目录。")
        for variable in ("SYSTEMROOT", "WINDIR", "ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(variable) and path.is_relative_to(Path(os.environ[variable])):
                raise StorageError("storage_path_denied", "系统及程序安装目录不在回收范围。")
        roots = sorted((Path(root) for root in self.context.projects.roots if path.is_relative_to(Path(root))), key=lambda item: len(item.parts))
        configured = [Path(root) for root in self.context.projects.settings.workspace_roots]
        if not roots and not any(path != root and path.is_relative_to(root) for root in configured) and (configured or not self.context.projects.settings.allow_external_paths):
            raise StorageError("storage_scope_denied", "目标不在当前后台允许的项目或工作目录范围。")
        return path, roots[0] if roots else path

    def label(self, path):
        roots = [Path(root) for root in self.context.projects.roots if path.is_relative_to(Path(root)) and path != Path(root)]
        return path.relative_to(min(roots, key=lambda item: len(item.parts))).as_posix() if roots else path.name

    def hook(self, event, path, values):
        self.context.security.run(event, [(self.label(path), json.dumps(values, ensure_ascii=False).encode())])

    def payload(self, record):
        return self.directory / "trash" / record["id"] / "payload"

    def source_stage(self, record):
        source = self.checked(record["source"])
        return source.parent / (".dianstar-trash-" + record["id"])

    def stage_parent(self, path, expected):
        path = self.checked(path)
        if identity(path.parent) != expected:
            raise StorageError("storage_parent_changed", "操作目录的父目录已变化，请保留现场并重新核对。")

    @contextmanager
    def mutation(self, paths=(), cancel=None, managed=(), *, check_ready=True):
        # One order everywhere: short-lived manager gate, ancestor-first repo
        # gates, then SQLite. File tools never acquire this manager gate.
        with self.locks.hold("managed-storage", cancel=cancel), ExitStack() as stack:
            roots = sorted({canonical(self.scope(path)[1]) for path in paths})
            for path in managed:
                path = self.checked(path)
                record = self.record(path.name, "scratch")
                if path != Path(record["directory"]) or not path.is_relative_to(self.directory / "tmp"):
                    raise StorageError("storage_record_invalid", "临时目录锁范围不正确。")
                roots.append(canonical(path))
            roots = sorted(set(roots))
            outer = [Path(root) for root in roots if not any(Path(root) != Path(other) and Path(root).is_relative_to(Path(other)) for other in roots)]
            for root in outer:
                stack.enter_context(self.locks.repository(root, cancel=cancel))
                if check_ready:
                    self.locks.ensure_ready(root)
            yield

    @staticmethod
    def cross_device(error):
        return error.errno == errno.EXDEV or getattr(error, "winerror", None) == 17

    def new_record(self, identifier, kind):
        now = self.journal.clock()
        return {"id": identifier, "kind": kind, "status": "prepared", "created": now, "updated": now,
                "expires": now + RETENTION, "pinned": False, "revision": 0}

    def blocked(self, paths, *, excluding=None):
        for record in self.journal.records():
            if record["id"] == excluding or record["kind"] != "trash" or record["status"] not in {"depositing", "restoring", "conflict"}:
                continue
            targets = [Path(record["source"])]
            if record.get("restore"):
                targets.append(Path(record["restore"]["destination"]))
            if any(a.is_relative_to(b) or b.is_relative_to(a) for a in paths for b in targets):
                raise StorageError("storage_recovery_required", "这个位置有未完成的回收/恢复，请先查询回收站并处理对应恢复编号。")

    def public(self, record):
        result = {key: record.get(key) for key in ("id", "kind", "status", "created", "expires", "pinned", "revision")}
        for key in ("source", "directory", "project"):
            if key in record:
                result[key] = self.context.mask(record[key])
        shape = record.get("vault_snapshot") or record.get("before") or {}
        result.update({key: shape.get(key, 0) for key in ("size", "files", "directories")})
        result["cleanup_pending"] = bool(record.get("leftovers"))
        return result

    def list_trash(self, limit=20):
        result = []
        for record in self.journal.records("trash"):
            try:
                self.scope(record["source"])
            except (ValueError, OSError):
                continue
            result.append(self.public(record))
            if len(result) >= limit:
                break
        return {"entries": result, "retention_days": 30}
