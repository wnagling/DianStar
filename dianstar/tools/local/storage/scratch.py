"""Task-scoped temporary workspaces, kept alive by OS leases across processes."""
import hashlib
from pathlib import Path
import uuid

from dianstar.platform.file_state import private_directory
from dianstar.security.file_locks import FileLockBusy
from .journal import RETENTION
from .tree import StorageError, identity, snapshot


class ScratchMixin:
    def conversation(self):
        value = self.context.environments.store.conversation_id
        return value or "request:" + self.context.mcp.authorization.request_id

    def live(self, identifier):
        if identifier in self.leases:
            return True
        try:
            with self.locks.hold("scratch-live:" + identifier, timeout=0):
                return False
        except FileLockBusy:
            return True

    def scratch_path(self, record):
        path = self.checked(record["directory"])
        if not path.is_relative_to(self.directory / "tmp") or path.name != record["id"]:
            raise StorageError("storage_record_invalid", "临时目录记录不在受管理位置。")
        if identity(path) != record["identity"]:
            raise StorageError("storage_changed", "临时目录已被替换，未打开或清理。")
        return path

    def activate(self, record):
        if record["id"] in self.leases:
            return record
        lease = self.locks.hold("scratch-live:" + record["id"], timeout=0)
        try:
            lease.__enter__()
        except FileLockBusy:
            raise StorageError("scratch_in_use", "临时工作区正被另一任务使用，可为本任务创建新的临时工作区。") from None
        try:
            directory = self.scratch_path(record)
            record.update(status="active", owner=self.context.mcp.authorization.request_id,
                          conversation=self.conversation(), expires=None)
            self.journal.save(record)
            self.leases[record["id"]] = lease
            self.scratch_roots[str(directory)] = record["id"]
            self.scratch_projects[str(directory)] = record["project"]
            return record
        except BaseException:
            lease.__exit__(None, None, None)
            raise

    def create_scratch(self, project):
        project = self.context.root({"repository": project})
        project = Path(self.scratch_projects.get(str(project), project))
        for identifier in self.leases:
            record = self.record(identifier, "scratch")
            if record["project"] == str(project):
                return self.scratch_result(record)
        self.context.security.run("before_scratch", [("scratch", str(project).encode())])
        record = self.new_record(uuid.uuid4().hex, "scratch")
        session = hashlib.sha256(self.conversation().encode()).hexdigest()[:24]
        path = self.directory / "tmp" / session / self.context.mcp.authorization.request_id / record["id"]
        private_directory(path)
        record.update(directory=str(path), project=str(project), conversation=self.conversation(), identity=identity(path))
        self.journal.save(record)
        return self.scratch_result(self.activate(record))

    def open_scratch(self, identifier):
        record = self.record(identifier, "scratch")
        if record["conversation"] != self.conversation() or record["status"] not in {"active", "released"}:
            raise StorageError("scratch_scope_denied", "这不是当前会话可用的临时工作区。")
        self.context.root({"repository": record["project"]})
        return self.scratch_result(self.activate(record))

    def scratch_result(self, record):
        return {"success": True, **self.public(record), "repository": record["directory"],
                "message": "临时工作区已就绪；read/write/shell/workspace_run 可将此路径作为 repository；Windows shell 为 PowerShell。任务结束后保留30天，不是 Git 仓库。"}

    def list_scratch(self, limit=20):
        return {"entries": [self.public(record) for record in self.journal.records("scratch")
            if record["conversation"] == self.conversation()][:limit], "retention_days": 30}

    def close(self):
        warnings = []
        try:
            with self.locks.hold("managed-storage"):
                for identifier in list(self.leases):
                    try:
                        record = self.record(identifier, "scratch")
                        record.update(status="released", expires=self.journal.clock() + RETENTION)
                        self.journal.save(record)
                    except Exception:
                        warnings.append(identifier)
        except Exception:
            warnings.extend(self.leases)
        finally:
            for lease in self.leases.values():
                lease.__exit__(None, None, None)
            self.leases.clear()
            self.scratch_roots.clear()
            self.scratch_projects.clear()
        return warnings
