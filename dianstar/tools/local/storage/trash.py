"""Recoverable file/directory removal, with durable intent before every move."""
from copy import deepcopy
import os
from pathlib import Path
import uuid

from dianstar.platform.file_state import private_directory
from .tree import StorageError, snapshot, same, identity, rename_new, copy_tree, remove_verified, MAX_BYTES, check_cancel


class TrashMixin:
    def plan_put(self, args, cancel=None):
        paths = [self.scope(value)[0] for value in args["paths"]]
        if len(set(paths)) != len(paths) or any(a != b and a.is_relative_to(b) for a in paths for b in paths):
            raise StorageError("storage_overlapping_paths", "同批目标不能重复，也不能同时包含父目录及其子项。")
        self.blocked(paths)
        records, total = [], 0
        for path in paths:
            before = snapshot(path, cancel)
            if before is None:
                raise StorageError("storage_source_missing", "回收目标不存在，请重新核对路径。")
            self.hook("before_trash", path, {"source": str(path), "size": before["size"]})
            total += before["size"]
            if total > MAX_BYTES:
                raise StorageError("storage_too_large", "本批总量超过4 GiB，请分批处理。")
            records.append({"source": str(path), "before": before, "parent": identity(path.parent)})
        return {"entries": records}

    def rollback_deposit(self, record):
        source, payload, stage = Path(record["source"]), self.payload(record), self.source_stage(record)
        self.stage_parent(stage, record["parent"])
        if stage.exists():
            if source.exists() or not same(snapshot(stage), record["before"]):
                raise StorageError("storage_recovery_conflict", "原位置或暂存原件发生变化，已保留全部副本。")
            rename_new(stage, source)
        elif record.get("method") == "rename" and payload.exists():
            if source.exists() or not same(snapshot(payload), record["before"]):
                raise StorageError("storage_recovery_conflict", "原位置或回收副本发生变化，未覆盖。")
            rename_new(payload, source)
        elif not source.exists():
            raise StorageError("storage_recovery_conflict", "未找到可验证的完整原件，请保留恢复记录。")
        # A failed cross-volume copy can be incomplete. Keep it privately,
        # rather than deleting bytes whose ownership/content cannot be proved.
        record["status"] = "rolled_back"
        if payload.exists():
            record["pinned"] = True
            record["leftovers"] = [{"path": str(payload), "snapshot": snapshot(payload)}]
        self.journal.save(record)

    def put(self, plan, cancel=None):
        records, batch = [], uuid.uuid4().hex
        for entry in plan["entries"]:
            record = self.new_record(uuid.uuid4().hex, "trash")
            record.update(deepcopy(entry), status="depositing", batch=batch, method="rename", leftovers=[])
            records.append(record)
        self.journal.save_many(records)
        committed = False
        try:
            for record in records:
                check_cancel(cancel)
                source = Path(record["source"])
                self.stage_parent(source, record["parent"])
                if not same(snapshot(source, cancel), record["before"]):
                    raise StorageError("storage_changed", "回收前文件发生变化，未继续移动。")
                private_directory(self.payload(record).parent)
                try:
                    rename_new(source, self.payload(record))
                except OSError as error:
                    if not self.cross_device(error):
                        raise
                    record["method"] = "copy"
                    self.journal.save(record)
                    copy_tree(source, self.payload(record), record["before"], cancel)
                    # Only park the source after a full verified copy exists.
                    rename_new(source, self.source_stage(record))
                    if not same(snapshot(self.source_stage(record), cancel), record["before"]):
                        raise StorageError("storage_changed", "复制后原件发生变化，已保留现场。")
                record["vault_snapshot"] = snapshot(self.payload(record), cancel)
                if not same(record["vault_snapshot"], record["before"], identities=record["method"] == "rename"):
                    raise StorageError("storage_changed", "回收副本校验失败，未确认完成。")
                self.journal.save(record)
            check_cancel(cancel)
            for record in records:
                record["status"] = "ready"
                record["expires"] = self.journal.clock() + 30 * 86400
                if record["method"] == "copy":
                    record["leftovers"] = [{"path": str(self.source_stage(record)), "parent": record["parent"], "snapshot": record["before"]}]
            self.journal.save_many(records)  # One durable batch commit before deleting originals.
            committed = True
        except Exception as error:
            for record in reversed(records):
                try:
                    self.rollback_deposit(record)
                except Exception:
                    record["status"], record["pinned"] = "conflict", True
                    try:
                        self.journal.save(record)
                    except Exception:
                        pass  # Earlier durable intent and returned IDs survive a failed status update.
            return {"error": getattr(error, "code", "trash_operation_failed"), "message": "回收未全部完成；已尝试恢复原位置，冲突副本继续保留。",
                    "entries": [self.public(record) for record in records]}
        if committed:
            for record in records:
                stage = self.source_stage(record)
                if stage.exists():
                    try:
                        self.stage_parent(stage, record["parent"])
                        remove_verified(stage, record["before"])
                        record["leftovers"] = []
                        self.journal.save(record)
                    except Exception:
                        record["leftovers"] = [{"path": str(stage), "parent": record["parent"], "snapshot": record["before"]}]
                        record["pinned"] = True
                        try:
                            self.journal.save(record)
                        except Exception:
                            pass  # Durable ready intent already contains the cleanup path.
        return {"success": True, "entries": [self.public(record) for record in records],
                "message": "已移入本机回收站，保留30天；可按恢复编号恢复，未永久删除。"}

    def plan_restore(self, args, cancel=None):
        record = self.record(args["id"], "trash")
        if record["status"] != "ready":
            raise StorageError("trash_not_restorable", "这条记录尚不可直接恢复，请查询状态或先处理未完成记录。")
        destination = self.scope(args.get("destination", record["source"]))[0]
        if destination.exists() or destination.is_symlink():
            raise StorageError("storage_destination_exists", "原位置已存在内容，未覆盖；可指定一个新的恢复位置。")
        parent = identity(destination.parent)
        if "destination" not in args and parent != record["parent"]:
            raise StorageError("storage_parent_changed", "原父目录已被替换，请明确指定新的恢复位置。")
        current = snapshot(self.payload(record), cancel)
        if not same(current, record["vault_snapshot"]):
            raise StorageError("storage_backup_changed", "回收副本与记录不一致，已停止恢复。")
        self.hook("before_storage_restore", destination, {"id": record["id"], "destination": str(destination)})
        self.blocked([destination], excluding=record["id"])
        return {"record": record, "destination": str(destination), "parent": parent}

    def restore(self, plan, cancel=None):
        record = deepcopy(plan["record"])
        destination = Path(plan["destination"])
        stage = destination.parent / (".dianstar-trash-restore-" + record["id"] + "-" + uuid.uuid4().hex)
        record.update(status="restoring", restore={"destination": str(destination), "parent": plan["parent"], "stage": str(stage), "method": "rename"})
        self.journal.save(record)
        try:
            self.stage_parent(destination, plan["parent"])
            check_cancel(cancel)
            try:
                rename_new(self.payload(record), destination)
                record["restore"]["snapshot"] = record["vault_snapshot"]
            except OSError as error:
                if not self.cross_device(error):
                    raise
                record["restore"]["method"] = "copy"
                self.journal.save(record)
                copied = copy_tree(self.payload(record), stage, record["vault_snapshot"], cancel)
                record["restore"]["snapshot"] = copied
                self.journal.save(record)  # Published copy identity is durable first.
                check_cancel(cancel)
                self.stage_parent(destination, plan["parent"])
                rename_new(stage, destination)
            if not same(snapshot(destination, cancel), record["restore"]["snapshot"]):
                raise StorageError("storage_changed", "恢复后的文件发生变化，请核对现场。")
            record["status"] = "restored"
            if self.payload(record).exists():
                record.setdefault("leftovers", []).append({"path": str(self.payload(record)), "snapshot": record["vault_snapshot"]})
            self.journal.save(record)
        except Exception as error:
            # Persistent restoring state allows a later request to distinguish
            # an already-published result from an incomplete copy.
            return {"error": getattr(error, "code", "trash_restore_incomplete"), "id": record["id"],
                    "message": "恢复未确认完成；原回收记录及临时副本已保留，请通过恢复编号核对。"}
        if self.payload(record).exists():
            try:
                remove_verified(self.payload(record), record["vault_snapshot"])
                record["leftovers"] = [item for item in record.get("leftovers", []) if Path(item["path"]) != self.payload(record)]
                self.journal.save(record)
            except Exception:
                record["pinned"] = True
                try:
                    self.journal.save(record)
                except Exception:
                    pass
        return {"success": True, "id": record["id"], "destination": str(destination), "message": "已恢复到指定位置，未覆盖已有内容。",
                "cleanup_pending": bool(record.get("leftovers"))}
