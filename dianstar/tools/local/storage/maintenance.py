"""Explicit recovery and ownership-checked permanent cleanup of managed data."""
from copy import deepcopy
from pathlib import Path

from .tree import StorageError, snapshot, same, identity, remove_verified
from .journal import RETENTION


class MaintenanceMixin:
    @staticmethod
    def recovery_phase(record):
        return record.get("recovery_phase") or ("purge" if record.get("purge_trees") else
            "restore" if record.get("restore") and record["status"] not in {"ready", "restored"} else
            "leftovers" if record["status"] in {"ready", "restored"} else "deposit")

    def abandoned(self):
        return [record for record in self.journal.records("scratch") if record["conversation"] == self.conversation()
            and record["status"] == "active" and not self.live(record["id"])]

    def auxiliary(self, record):
        """Only ledger-owned vault/staging paths, never the original/destination."""
        paths = []
        if record["kind"] == "trash":
            payload = self.payload(record)
            if payload.exists():
                paths.append(payload)
            stage = self.source_stage(record)
            if stage.exists():
                self.stage_parent(stage, record["parent"])
                paths.append(stage)
            restore = record.get("restore")
            if restore:
                extra = self.checked(restore["stage"])
                if (extra.parent != Path(restore["destination"]).parent
                        or not extra.name.startswith(".dianstar-trash-restore-" + record["id"] + "-")):
                    raise StorageError("storage_record_invalid", "恢复临时路径与记录不匹配。")
                if extra.exists():
                    self.stage_parent(extra, restore["parent"])
                    paths.append(extra)
            for item in record.get("leftovers", []):
                extra = self.checked(item["path"])
                if extra in paths or not extra.exists():
                    continue
                if extra.is_relative_to(self.payload(record).parent):
                    paths.append(extra)
                elif extra.name.startswith(".dianstar-trash-") and record["id"] in extra.name:
                    self.scope(item.get("anchor", record["source"]))
                    self.stage_parent(extra, item["parent"])
                    paths.append(extra)
                else:
                    raise StorageError("storage_record_invalid", "残留副本不在这条恢复记录的管理范围。")
        else:
            path = Path(record["directory"])
            if path.exists():
                paths.append(self.scratch_path(record))
        return paths

    def permitted_record(self, record):
        if record["kind"] == "trash":
            self.scope(record["source"])
        else:
            if record["conversation"] != self.conversation():
                raise StorageError("scratch_scope_denied", "不能管理其他会话的临时目录。")
            # The project may currently be recycled. Its configured scope must
            # still contain the path, even though the directory can be absent.
            if Path(record["project"]) not in {Path(root) for root in self.context.mcp.git.repositories}:
                raise StorageError("storage_scope_denied", "临时目录的原项目不在当前允许范围。")

    def plan_purge(self, identifiers, cancel=None, *, expired=False):
        entries = []
        for identifier in identifiers:
            record = self.record(identifier)
            self.permitted_record(record)
            if record.get("pinned") or record["kind"] == "scratch" and self.live(identifier):
                raise StorageError("storage_retained", "标记保留或仍在使用的内容不能清理。")
            if record["status"] in {"depositing", "restoring"}:
                raise StorageError("storage_recovery_required", "未完成操作应先恢复或明确保留当前状态。")
            if expired and (record.get("expires") is None or record["expires"] > self.journal.clock()
                            or record["status"] not in {"ready", "released"} or record.get("leftovers")):
                continue
            trees = [{"path": str(path), "snapshot": snapshot(path, cancel)} for path in self.auxiliary(record)]
            if expired and record["kind"] == "trash" and trees and not same(trees[0]["snapshot"], record["vault_snapshot"]):
                continue  # Changed vault contents are retained, not silently erased.
            if expired and record["kind"] == "scratch" and trees:
                latest = max(node["mtime"] / 1e9 for node in trees[0]["snapshot"]["nodes"])
                if latest + RETENTION > self.journal.clock():
                    continue
            self.context.security.run("before_storage_purge", [("managed-backups", record["id"].encode())])
            entries.append({"record": record, "trees": trees})
        return {"entries": entries, "expired_only": expired}

    def purge(self, plan, cancel=None):
        result = []
        for abandoned in plan.get("abandoned", []):
            abandoned = deepcopy(abandoned)
            if not self.live(abandoned["id"]):
                abandoned.update(status="released", expires=self.journal.clock() + RETENTION)
                self.journal.save(abandoned)
        for item in plan["entries"]:
            record = deepcopy(item["record"])
            if record["kind"] == "scratch" and self.live(record["id"]):
                raise StorageError("scratch_in_use", "临时工作区已重新启用，未清理。")
            record.update(status="purging", purge_trees=item["trees"])
            self.journal.save(record)
            try:
                for tree in item["trees"]:
                    remove_verified(Path(tree["path"]), tree["snapshot"], cancel)
                record.update(status="purged", leftovers=[], purge_trees=[])
                self.journal.save(record)
                result.append({"id": record["id"], "status": "purged"})
            except Exception:
                result.append({"id": record["id"], "status": "recovery_required"})
        return {"success": all(item["status"] == "purged" for item in result), "entries": result,
                "message": "仅清理了计划中的本机副本；原文件位置和已恢复的目标未被删除。"}

    def cleanup_ids(self, limit=20, *, small_only=False):
        result = []
        for record in reversed(self.journal.records()):
            if (record["status"] not in {"ready", "released"} or record.get("pinned") or record.get("leftovers")
                    or record.get("expires") is None or record["expires"] > self.journal.clock()):
                continue
            try:
                self.permitted_record(record)
                if record["kind"] == "scratch" and self.live(record["id"]):
                    continue
            except (ValueError, OSError):
                continue
            if small_only:
                try:
                    shape = (snapshot(self.scratch_path(record), max_bytes=64 * 1024**2, max_nodes=5000)
                             if record["kind"] == "scratch" else record.get("vault_snapshot")) or {}
                except (ValueError, OSError):
                    continue
                if shape.get("size", 0) > 64 * 1024**2 or len(shape.get("nodes", [])) > 5000:
                    continue
            result.append(record["id"])
            if len(result) >= limit:
                break
        return result

    def plan_recover(self, args, cancel=None):
        record = self.record(args["id"])
        self.permitted_record(record)
        if record["kind"] == "scratch" and self.live(record["id"]):
            raise StorageError("scratch_in_use", "仍在使用的临时目录不能恢复清理操作。")
        if record["status"] not in {"depositing", "restoring", "purging", "conflict"} and not (
                record["status"] in {"ready", "restored"} and record.get("leftovers")):
            raise StorageError("storage_recovery_unneeded", "这条记录没有待续办的恢复操作。")
        phase = self.recovery_phase(record)
        event = "before_storage_purge" if phase in {"purge", "leftovers"} else "before_storage_restore"
        self.context.security.run(event, [("managed-backups", record["id"].encode())])
        siblings = ([item for item in self.journal.records("trash") if item.get("batch") == record.get("batch")
            and item["status"] in {"depositing", "conflict"} and self.recovery_phase(item) == "deposit"]
            if phase == "deposit" else [record])
        paths = set()
        for sibling in siblings:
            self.permitted_record(sibling)
            paths.update(str(path) for path in self.auxiliary(sibling))
            if sibling["kind"] == "trash":
                paths.add(sibling["source"])
        if record.get("restore"):
            paths.add(record["restore"]["destination"])
        return {"record": record, "siblings": siblings, "phase": phase, "mode": args.get("mode", "resume"),
                "trees": [{"path": path, "snapshot": snapshot(Path(path), cancel)} for path in sorted(paths)]}

    def recover(self, plan):
        record = deepcopy(plan["record"])
        if plan["mode"] == "keep_current":
            siblings = deepcopy(plan["siblings"])
            for item in siblings:
                item.update(status="preserved", pinned=True)
            self.journal.save_many(siblings)
            return {"success": True, "id": record["id"], "message": "保留所有当前文件和本机副本，未覆盖或删除内容。"}
        try:
            if plan["phase"] == "leftovers":
                completed_status = record.get("completed_status", record["status"])
                if completed_status == "ready" and not same(snapshot(self.payload(record)), record["vault_snapshot"]):
                    raise StorageError("storage_backup_changed", "完整回收副本发生变化，保留现场。")
                if completed_status == "restored" and any(Path(item["path"]) == self.payload(record)
                        for item in record.get("leftovers", [])):
                    restore = record["restore"]
                    destination = Path(restore["destination"])
                    self.stage_parent(destination, restore["parent"])
                    if not same(snapshot(destination), restore["snapshot"]):
                        raise StorageError("storage_restore_changed", "已恢复的目标发生变化，保留完整副本。")
                # Only redundant, previously recorded copies; original and
                # published destinations are never cleanup candidates.
                owned = set(self.auxiliary(record))
                for item in record.get("leftovers", []):
                    path = Path(item["path"])
                    if path.exists() and path not in owned:
                        raise StorageError("storage_record_invalid", "副本不属于这项记录。")
                    if path == self.payload(record) and completed_status == "ready":
                        raise StorageError("storage_record_invalid", "不能清理唯一回收副本。")
                    remove_verified(path, item["snapshot"])
                record["leftovers"] = []
                record.update(status=completed_status, recovery_phase=None)
                self.journal.save(record)
                return {"success": True, **self.public(record), "message": "已核对完整副本并清理操作残留；原位置和恢复目标未修改。"}
            if plan["phase"] == "deposit":
                # Roll back all still-pending siblings from the same batch.
                siblings = deepcopy(plan["siblings"])
                for sibling in reversed(siblings):
                    self.permitted_record(sibling)
                    self.rollback_deposit(sibling)
                return {"success": True, "entries": [self.public(item) for item in siblings], "message": "已恢复这批未完成回收的原位置；残留副本继续保留。"}
            if plan["phase"] == "purge":
                for tree in record["purge_trees"]:
                    remove_verified(Path(tree["path"]), tree["snapshot"])
                record.update(status="purged", purge_trees=[], leftovers=[])
            elif plan["phase"] == "restore":
                restore = record["restore"]
                target = Path(restore["destination"])
                self.scope(target)
                self.stage_parent(target, restore["parent"])
                expected = restore.get("snapshot") or (record["vault_snapshot"] if restore["method"] == "rename" else None)
                if expected is not None and same(snapshot(target), expected):
                    record["status"] = "restored"
                    if self.payload(record).exists():
                        record.setdefault("leftovers", []).append({"path": str(self.payload(record)), "snapshot": record["vault_snapshot"]})
                        record["pinned"] = True
                elif self.payload(record).exists() and same(snapshot(self.payload(record)), record["vault_snapshot"]) and not target.exists():
                    # Incomplete copies are retained. A new restore attempt uses
                    # a fresh staging name, never overwrites these bytes.
                    stage = Path(restore["stage"])
                    if stage.exists():
                        record.setdefault("leftovers", []).append({"path": str(stage), "anchor": restore["destination"], "parent": restore["parent"], "snapshot": snapshot(stage)})
                        record["pinned"] = True
                    record["status"] = "ready"
                else:
                    raise StorageError("storage_recovery_conflict", "恢复目标或副本发生变化，未覆盖。")
            record["recovery_phase"] = None
            self.journal.save(record)
            return {"success": True, **self.public(record), "message": "恢复状态已核对，保留了未确认可清理的副本。"}
        except (ValueError, OSError):
            if plan["phase"] == "leftovers":
                record["completed_status"] = record.get("completed_status", plan["record"]["status"])
            record["recovery_phase"] = plan["phase"]
            record.update(status="conflict", pinned=True)
            self.journal.save(record)
            return {"error": "storage_recovery_conflict", "id": record["id"], "message": "检测到恢复冲突，原位置和副本均未强制覆盖；可选择保留当前状态。"}
