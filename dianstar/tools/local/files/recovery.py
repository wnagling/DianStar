"""Single- and multi-file transactions with restartable, conflict-safe undo."""
import base64
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import uuid

from dianstar.security.file_locks import FileLocks, FileLockBusy, canonical
from .journal import FileJournal
from .text import FileError, load


def image(snapshot, mode=None):
    if snapshot is None:
        return None
    return {"data": base64.b64encode(snapshot.data).decode("ascii"), "version": list(snapshot.version),
            "mode": mode, "sha256": hashlib.sha256(snapshot.data).hexdigest()}


def contents(value):
    if value is None:
        return None
    data = base64.b64decode(value["data"], validate=True)
    if len(data) > 2 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != value["sha256"]:
        raise FileError("file_journal_invalid", "恢复副本校验失败，未修改项目文件。")
    return data


def matches(snapshot, value):
    return snapshot is None if value is None else (snapshot is not None and
        list(snapshot.version) == value["version"] and snapshot.data == contents(value))


def sync_directory(path):
    # On Windows file handles are flushed before rename. Directory fsync is not
    # portable there; do not claim survival of every power/storage failure.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class FileRecovery:
    def __init__(self, context, directory=None):
        self.context = context
        self.journal = FileJournal(directory)
        self.locks = FileLocks(directory)

    def path(self, root, entry):
        path = self.context.path({"repository": str(root), "path": entry["path"]}, local_only=entry.get("local_only", False))
        for anchor in entry.get("parents", []):
            parent = root if anchor["path"] == "." else self.context.path({"repository": str(root), "path": anchor["path"]})
            if not parent.exists() or [parent.stat().st_dev, parent.stat().st_ino] != anchor["identity"]:
                raise FileError("file_parent_changed", "目标文件的父目录已被替换，停止操作并保留恢复记录。")
        return path

    @contextmanager
    def access(self, root, paths, *, write=False, cancel=None, recover=True):
        while True:
            with self.locks.repository(root, shared=True, cancel=cancel):
                records = self.journal.summaries(root, unfinished=True) if recover or write else []
                interrupted = recover and any(record["status"] != "conflict" and not self.active(record["id"]) for record in records)
                if not interrupted:
                    modes = [(path, not write) for path in paths]
                    if write:
                        for path in paths:
                            for parent in path.parents:
                                if parent == root or not parent.is_relative_to(root):
                                    break
                                modes.append((parent, parent.exists()))
                    with self.locks.resources(modes, cancel=cancel):
                        if write:
                            requested = {canonical(path) for path in paths}
                            # Re-read after acquiring file locks; another writer
                            # may have completed or left a conflict while waiting.
                            for record in self.journal.summaries(root, unfinished=True):
                                if record["status"] == "conflict" and any(canonical(root / entry["path"]) in requested for entry in record["entries"]):
                                    raise FileError("file_recovery_conflict", "目标文件有未解决的恢复冲突；请先查看 file_history，恢复或确认保留当前文件。")
                        yield
                    return
            # Never upgrade a shared lock. Active operations leave first, then
            # exclusive recovery can safely compensate abandoned transactions.
            with self.locks.repository(root, cancel=cancel):
                self.recover_pending(root)

    def active(self, identifier):
        try:
            with self.locks.hold("transaction:" + identifier, timeout=0):
                return False
        except FileLockBusy:
            return True

    def pending(self, root):
        return [{"transaction_id": record["id"], "status": record["status"]}
                for record in self.journal.summaries(root, unfinished=True)]

    def known_version(self, root, relative, snapshot, expected):
        if matches(snapshot, expected):
            return True
        if snapshot is None or expected is None or snapshot.data != contents(expected):
            return False
        # Undo changes inode/mtime. Only a durable chain of OUR restores can
        # explain that change; equal text written by an editor is insufficient.
        lineage = {}
        for record in self.journal.summaries(root, limit=1000):
            if record["status"] != "committed":
                continue
            for entry in record["entries"]:
                if (os.path.normcase(entry["path"]) == os.path.normcase(relative) and entry.get("after_version") is not None
                        and entry.get("restores_version") is not None):
                    lineage[tuple(entry["after_version"])] = entry["restores_version"]
        current, visited = list(snapshot.version), set()
        while tuple(current) in lineage and tuple(current) not in visited:
            visited.add(tuple(current))
            current = lineage[tuple(current)]
            if current == expected["version"]:
                return True
        return False

    def validate(self, root, record):
        if record["root"] != canonical(root) or not 1 <= len(record["entries"]) <= 20:
            raise FileError("file_journal_invalid", "恢复记录范围不正确。")
        paths = []
        for entry in record["entries"]:
            path = self.path(root, entry)
            paths.append(canonical(path))
            for label in ("before", "after", "undo"):
                contents(entry.get(label))
            for label in ("temp", "undo_temp"):
                if entry.get(label):
                    temporary = self.context.path({"repository": str(root), "path": entry[label]}, internal_temporary=True)
                    if temporary.parent != path.parent or not temporary.name.startswith(".dianstar-tmp-" + record["id"] + "-"):
                        raise FileError("file_journal_invalid", "恢复临时文件范围不正确。")
        if len(set(paths)) != len(paths):
            raise FileError("file_journal_invalid", "恢复记录包含重复路径。")

    def recover_pending(self, root):
        for summary in reversed(self.journal.summaries(root, unfinished=True)):
            if summary["status"] != "conflict":
                self.rollback(root, self.journal.get(root, summary["id"]))

    @contextmanager
    def command_access(self, root, cancel):
        with self.locks.repository(root, cancel=cancel):
            self.recover_pending(root)
            self.locks.ensure_ready(root)
            yield

    def exclusive_operation(self, root):
        from .io import held
        return held(lambda cancel: self.command_access(root, cancel))

    def directories(self, root, path, record):
        missing = []
        parent = path.parent
        while parent != root and not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            self.context.path({"repository": str(root), "path": directory.relative_to(root).as_posix()})
            directory.mkdir()
            info = directory.stat()
            record["directories"].append({"path": directory.relative_to(root).as_posix(), "identity": [info.st_dev, info.st_ino]})
            self.journal.save(record)
            sync_directory(directory.parent)

    def temporary(self, root, entry, record, data, *, undo=False):
        path = self.path(root, entry)
        self.directories(root, path, record)
        label = "undo_temp" if undo else "temp"
        if entry.get(label):
            self.cleanup(root, record)
            previous = self.context.path({"repository": str(root), "path": entry[label]}, internal_temporary=True)
            if previous.exists():
                raise FileError("file_temporary_conflict", "原恢复临时文件已变化，保留现场并停止恢复。")
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".dianstar-tmp-" + record["id"] + "-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            entry[label] = temporary.relative_to(root).as_posix()
            info = os.fstat(stream.fileno())
            entry[label + "_identity"] = [info.st_dev, info.st_ino]
            # Persist its name before writing: even ENOSPC leaves a discoverable,
            # bounded local temporary file, never an unrecorded target change.
            self.journal.save(record)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        mode = entry["before"]["mode"] if entry["before"] else (entry.get("after") or {}).get("mode")
        if mode is not None:
            temporary.chmod(mode)
        snapshot = load(temporary)
        if snapshot is None or snapshot.data != data:
            raise FileError("file_temporary_incomplete", "临时文件内容与计划不一致，未替换原文件。")
        saved = image(snapshot, mode)
        if undo:
            entry["undo"] = saved
        else:
            entry["after"] = saved
        self.journal.save(record)  # Expected inode/version durable BEFORE publication.
        sync_directory(temporary.parent)
        return temporary

    def cleanup(self, root, record, *, directories=False):
        warnings = []
        for entry in record["entries"]:
            for label, image_label in (("temp", "after"), ("undo_temp", "undo")):
                if not entry.get(label):
                    continue
                try:
                    path = self.context.path({"repository": str(root), "path": entry[label]}, internal_temporary=True)
                    if not path.exists():
                        continue
                    info = path.lstat()
                    expected = entry.get(image_label)
                    incomplete = expected is None or expected.get("version") is None
                    owned = stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and [info.st_dev, info.st_ino] == entry.get(label + "_identity")
                    if (incomplete and owned) or (not incomplete and matches(load(path), expected)):
                        path.unlink()
                    else:
                        warnings.append(entry["path"])
                except (OSError, ValueError):
                    warnings.append(entry["path"])
        if directories:
            for item in reversed(record["directories"]):
                try:
                    path = self.context.path({"repository": str(root), "path": item["path"]})
                    if path.exists() and [path.stat().st_dev, path.stat().st_ino] == item["identity"]:
                        path.rmdir()  # Never recursive: unrelated new files survive.
                        sync_directory(path.parent)
                except (OSError, ValueError):
                    warnings.append(item["path"])
        return sorted(set(warnings))

    def rollback(self, root, record):
        self.validate(root, record)
        conflicts = []
        for entry in record["entries"]:
            current = load(self.path(root, entry))
            if not (matches(current, entry["before"]) or
                    (entry.get("undo") is not None and matches(current, entry["undo"])) or
                    (entry.get("phase") != "pending" and matches(current, entry["after"]))):
                conflicts.append(entry["path"])
        if conflicts:
            record["status"], record["conflicts"] = "conflict", conflicts
            self.journal.save(record)
            return {"status": "conflict", "transaction_id": record["id"], "files": conflicts}
        record["status"] = "rolling_back"
        self.journal.save(record)
        for entry in reversed(record["entries"]):
            path = self.path(root, entry)
            current = load(path)
            if matches(current, entry["before"]) or (entry.get("undo") is not None and matches(current, entry["undo"])):
                entry["phase"] = "undone"
                self.journal.save(record)
                continue
            if not matches(current, entry["after"]):
                record["status"], record["conflicts"] = "conflict", [entry["path"]]
                self.journal.save(record)
                return {"status": "conflict", "transaction_id": record["id"], "files": record["conflicts"]}
            if entry["before"] is None:
                path.unlink()
            else:
                temporary = self.temporary(root, entry, record, contents(entry["before"]), undo=True)
                self.path(root, entry)
                if not matches(load(path), entry["after"]):
                    raise FileError("file_recovery_conflict", "恢复期间文件变化，已停止；原始副本继续保留。")
                if current is None:
                    if os.name == "nt":
                        os.rename(temporary, path)
                    else:
                        os.link(temporary, path)
                else:
                    os.replace(temporary, path)
            sync_directory(path.parent)
            entry["phase"] = "undone"
            self.journal.save(record)
        record["status"] = "rolled_back"
        record["cleanup_pending"] = self.cleanup(root, record, directories=True)
        self.journal.save(record)
        return {"status": "rolled_back", "transaction_id": record["id"], "directories_preserved": record["cleanup_pending"]}

    def commit(self, root, entries, request_id, cancel):
        identifier = uuid.uuid4().hex
        with self.locks.hold("transaction:" + identifier, cancel=cancel):
            return self._commit(root, entries, request_id, cancel, identifier)

    def _commit(self, root, entries, request_id, cancel, identifier):
        record = self.journal.create(root, request_id, entries, identifier=identifier)
        committed = False
        try:
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    raise FileError("file_cancelled", "文件任务已取消。")
                path = self.path(root, entry)
                if not matches(load(path), entry["before"]):
                    raise FileError("file_changed", "文件在写入前已变化，未继续修改。")
                if entry["after"] is None:
                    entry["phase"] = "ready"
                    self.journal.save(record)
                    path.unlink()
                else:
                    temporary = self.temporary(root, entry, record, contents(entry["after"]))
                    entry["phase"] = "ready"
                    self.journal.save(record)
                    self.path(root, entry)
                    if cancel is not None and cancel.is_set():
                        raise FileError("file_cancelled", "文件任务已取消。")
                    if not matches(load(path), entry["before"]):
                        raise FileError("file_changed", "文件在替换前已变化，未覆盖新版本。")
                    if entry["before"] is not None:
                        os.replace(temporary, path)
                    elif os.name == "nt":
                        os.rename(temporary, path)
                    else:
                        os.link(temporary, path)
                sync_directory(path.parent)
                self.path(root, entry)
                if not matches(load(path), entry["after"]):
                    raise FileError("file_changed", "写入后文件已有其他变化，已停止本批操作并保留恢复记录。")
                entry["phase"] = "done"
                self.journal.save(record)
            for entry in entries:
                if not matches(load(self.path(root, entry)), entry["after"]):
                    raise FileError("file_changed", "提交文件记录前发现外部修改，已停止本批操作。")
            if cancel is not None and cancel.is_set():
                raise FileError("file_cancelled", "文件任务已取消。")
            record["status"] = "committed"
            self.journal.save(record)
            committed = True
            record["cleanup_pending"] = self.cleanup(root, record)
            self.journal.save(record)
            return {"success": True, "transaction_id": record["id"], "files": [entry["path"] for entry in entries],
                    "cleanup_pending": record["cleanup_pending"]}
        except Exception as error:
            # A durable commit is never undone because a later cleanup/report
            # failed. Its ID remains discoverable through file_history.
            if committed:
                return {"success": True, "transaction_id": record["id"], "cleanup_pending": True}
            try:
                recovery = self.rollback(root, record)
            except Exception:
                recovery = {"status": "recovery_required", "transaction_id": record["id"]}
            return {"error": getattr(error, "code", "file_operation_failed"),
                    "message": "文件任务未完成；请依据恢复状态处理，未自动覆盖外部改动。", "recovery": recovery}
