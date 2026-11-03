"""Single-file plans with read snapshots, content hooks and atomic publication."""
import difflib
import hashlib
import json
import os
import base64
import threading
from pathlib import Path
import time

from dianstar.security.authorization import request_key
from dianstar.security.content_guard import SecurityViolation
from dianstar.security.gateway import ToolPreparationError
from .text import FileError, MAX_FILE, encode, load, normalize_lines
from .io import blocking
from .recovery import contents, image, matches
from .transforms import transform_entries, transform_paths


class FileWriter:
    def __init__(self, context, reads):
        self.context, self.reads, self.prepared = context, reads, {}
        self.prepared_lock = threading.Lock()

    def plan(self, name, args, *, exact_data=None):
        path = self.context.path(args, local_only=args.get("local_only", False))
        before = load(path)
        if (before and self.context.mask_file(before.text, args["path"]) != before.text
                and any("[已遮挡" in args.get(field, "") for field in ("content", "old_string", "new_string"))):
            raise FileError("file_redacted_content", "不能将脱敏占位文字写回原文件；请精确修改未遮挡的部分，或明确提供完整的新内容。")
        if before is not None and name != "workspace_delete" and self.reads.get(str(path)) != before:
            raise FileError("file_read_required", "现有文件尚未读取或读取后已变化，请先用 read 重新读取，再修改。")
        if name == "edit":
            if before is None:
                raise FileError("file_not_found", "edit 只修改已有文件；新建请使用 write。")
            old = normalize_lines(args["old_string"], before.newline)
            new = normalize_lines(args["new_string"], before.newline)
            count = before.text.count(old) if old else 0
            if not count or (count != 1 and not args.get("replace_all", False)):
                raise FileError("file_match_failed", f"待替换内容匹配到 {count} 处；默认要求唯一匹配，请重读或明确 replace_all。")
            after = before.text.replace(old, new, -1 if args.get("replace_all", False) else 1)
            data = encode(after, before.bom)
        elif name == "workspace_delete":
            if before is None:
                raise FileError("file_not_found", "目标文件不存在。")
            after, data = "", b""
        else:
            after = args["content"]
            if before is not None:
                after = normalize_lines(after, before.newline)
            data = encode(after, before.bom if before is not None else after.startswith("\ufeff"))
        if exact_data is not None:
            data = exact_data
        if len(data) > MAX_FILE or b"\0" in data:
            raise FileError("file_too_large", "只写入大小受限的 UTF-8 文本文件。")
        return path, before, after, data

    def operations(self, name, args):
        if name != "file_batch":
            return [(name, args)]
        operations = []
        for item in args["operations"]:
            entry = {"repository": args["repository"], **item}
            operation = entry.pop("operation")
            operations.append(("workspace_delete" if operation == "delete" else operation, entry))
        return operations

    def entry(self, root, operation, args, *, exact_data=None):
        path, before, after, data = self.plan(operation, args, exact_data=exact_data)
        mode = path.stat().st_mode if before else None
        parents, parent = [], path.parent
        while True:
            if parent.exists():
                info = parent.stat()
                parents.append({"path": parent.relative_to(root).as_posix(), "identity": [info.st_dev, info.st_ino]})
            if parent == root:
                break
            parent = parent.parent
        return {"path": args["path"], "local_only": args.get("local_only", False),
                "before": image(before, mode), "after": None if operation == "workspace_delete" else {
                    "data": base64.b64encode(data).decode("ascii"), "version": None, "mode": mode,
                    "sha256": hashlib.sha256(data).hexdigest()}, "phase": "pending", "parents": parents}

    def preview_sync(self, name, args, cancel):
        self.context.projects.require(write=True)
        root = self.context.root(args)
        if name in {"patch", "move"}:
            paths = [self.context.path({"repository": args["repository"], "path": path}) for path in transform_paths(name, args)]
        else:
            paths = ([self.context.path(value, local_only=value.get("local_only", False))
                      for _, value in self.operations(name, args)] if name != "file_restore" else [])
        with self.context.files.access(root, paths, cancel=cancel, recover=name != "file_restore"):
            source = None
            if name == "file_restore":
                source = self.context.files.journal.get(root, args["transaction_id"])
                if args.get("keep_current", False):
                    if source["status"] not in {"prepared", "rolling_back", "conflict"}:
                        raise FileError("file_restore_unavailable", "这条记录没有需要确认保留的恢复冲突。")
                    plan = {"source": source, "entries": [], "keep_current": True}
                else:
                    self.context.files.validate(root, source)
                    resume = source["status"] in {"prepared", "rolling_back"}
                    if source["status"] != "committed" and not resume:
                        raise FileError("file_restore_unavailable", "仅已完成的修改可撤销；中断记录由恢复流程处理，冲突时可确认保留当前文件。")
                    entries = []
                    for original in reversed(source["entries"]):
                        path = self.context.files.path(root, original)
                        current = load(path)
                        if resume:
                            accepted = matches(current, original["before"]) or (original.get("undo") is not None and matches(current, original["undo"])) or (original.get("phase") != "pending" and matches(current, original["after"]))
                        else:
                            accepted = self.context.files.known_version(root, original["path"], current, original["after"])
                        if not accepted:
                            raise FileError("file_restore_conflict", "修改完成后文件已有其他变化，未覆盖；请查看当前文件再处理。")
                        target = dict(original["before"]) if original["before"] else None
                        if target is not None:
                            target["version"] = None
                        entries.append({"path": original["path"], "local_only": original.get("local_only", False),
                            "before": image(current, path.stat().st_mode if current else None), "after": target,
                            "phase": "pending", "parents": original.get("parents", []),
                            **({"moved_from": original["moved_to"]} if original.get("moved_to") else {}),
                            **({"moved_to": original["moved_from"]} if original.get("moved_from") else {}),
                            "restores_version": original["before"]["version"] if original["before"] else None})
                    plan = {"source": source, "entries": entries, "resume": resume}
            else:
                entries = (transform_entries(self, root, name, args) if name in {"patch", "move"} else
                           [self.entry(root, operation, value) for operation, value in self.operations(name, args)])
                if not 1 <= len(entries) <= 20:
                    raise FileError("file_batch_too_large", "一项文件事务最多涉及 20 个路径。")
                if len({os.path.normcase(entry["path"]) for entry in entries}) != len(entries):
                    raise FileError("file_duplicate_path", "同一批次不能多次修改同一路径，请先合并该文件的修改。")
                paths_set = [entry["path"] for entry in entries]
                if any(left != right and right.startswith(left + "/") for left in paths_set for right in paths_set):
                    raise FileError("file_path_conflict", "同一批次的文件路径不能互为父子目录。")
                plan = {"source": None, "entries": entries}
            diffs = []
            total = 0
            preview_size = 0
            move_sources = {entry["path"]: entry for entry in plan["entries"] if entry.get("moved_to")}
            for entry in plan["entries"]:
                old, new = contents(entry["before"]), contents(entry["after"])
                total += len(old or b"") + len(new or b"")
                if total > 32 * 1024 * 1024:
                    raise FileError("file_batch_too_large", "本批次快照总量超限，请缩小修改范围。")
                if new is not None and not plan.get("resume"):
                    self.context.security.run("before_write", [(entry["path"], new)], local_only=entry["local_only"])
                shown_old = contents(move_sources[entry["moved_from"]]["before"]) if entry.get("moved_from") else old
                entry_diff = "" if entry.get("moved_to") else "".join(difflib.unified_diff((shown_old or b"").decode("utf-8-sig").splitlines(True),
                    (new or b"").decode("utf-8-sig").splitlines(True), fromfile=entry.get("moved_from") or (entry["path"] if old is not None else "/dev/null"),
                    tofile=entry["path"] if new is not None else "/dev/null"))
                if entry.get("moved_from"):
                    entry_diff = f"移动：{entry['moved_from']} → {entry['path']}\n" + entry_diff
                preview_size += len(entry_diff)
                if preview_size > 24000:
                    raise FileError("file_preview_too_large", "完整差异超出预览限制，请缩小修改范围。")
                diffs.append(self.context.mask_file(entry_diff, entry["path"]))
            diff = "".join(diffs)
            if len(diff) > 24000:
                raise FileError("file_preview_too_large", "完整差异超出预览限制，请缩小修改范围。")
            if name == "file_restore":
                label = "保留当前文件并结束恢复冲突" if plan.get("keep_current") else "撤销一批文件修改"
            elif name == "file_batch":
                label = "修改一批项目文件"
            elif name == "patch":
                label = "应用项目文件补丁"
            elif name == "move":
                label = "移动或重命名项目文件"
            else:
                entry = plan["entries"][0]
                label = "删除项目文件" if entry["after"] is None else "修改项目文件" if entry["before"] else "创建项目文件"
            description = {"操作": label, "仓库": str(root), "文件": args.get("path", [entry["path"] for entry in (source or plan)["entries"]]),
                           "差异": self.context.mask(diff), "仅本地保存": any(entry["local_only"] for entry in plan["entries"])}
            if source:
                description["恢复记录"] = source["id"]
                description["说明"] = "保留所有当前文件，原始恢复副本继续保留。" if plan.get("keep_current") else "仅在文件仍为这批修改的结果时恢复；其他改动会阻止恢复。"
            if name in {"patch", "move"}:
                description["移动"] = [{"源": entry["moved_from"], "目标": entry["path"]} for entry in plan["entries"] if entry.get("moved_from")]
            with self.prepared_lock:
                key = request_key(name, args)
                if key in self.prepared and self.prepared[key][1] != plan:
                    raise FileError("file_preparation_in_progress", "相同文件计划正在等待处理，请先完成或取消已有请求。")
                self.prepared[key] = (time.monotonic(), plan)
            return json.dumps(description, ensure_ascii=False)

    async def preview(self, name, args):
        try:
            return await blocking(lambda cancel: self.preview_sync(name, args, cancel))
        except SecurityViolation as error:
            raise ToolPreparationError(error.code, error.message, findings=error.result()["findings"]) from None
        except FileError as error:
            raise ToolPreparationError(error.code, error.message) from None
        except Exception:
            raise ToolPreparationError("file_preparation_failed", "文件锁、恢复记录或修改预检未完成，未申请执行。") from None

    def apply_sync(self, action, cancel):
        self.context.projects.require(write=True)
        args, name = action.arguments, action.name
        with self.prepared_lock:
            item = self.prepared.pop(request_key(name, args), None)
        if item is None or time.monotonic() - item[0] > 600 or not self.context.projects.authorization.active:
            return {"error": "file_not_prepared"}
        plan = item[1]
        root = self.context.root(args)
        if plan.get("keep_current"):
            with self.context.files.locks.repository(root, cancel=cancel):
                current = self.context.files.journal.get(root, args["transaction_id"])
                if current != plan["source"] or current["status"] not in {"prepared", "rolling_back", "conflict"}:
                    raise FileError("file_changed", "恢复记录已变化，请重新确认。")
                current["status"] = "preserved"
                self.context.files.journal.save(current)
                return {"success": True, "transaction_id": current["id"], "status": "current_files_preserved"}
        if plan.get("resume"):
            with self.context.files.locks.repository(root, cancel=cancel):
                current = self.context.files.journal.get(root, args["transaction_id"])
                if current != plan["source"] or any(not matches(load(self.context.files.path(root, entry)), entry["before"]) for entry in plan["entries"]):
                    raise FileError("file_changed", "确认期间恢复状态或文件已变化，未恢复。")
                result = self.context.files.rollback(root, current)
                return {"success": result["status"] == "rolled_back", "recovery": result, "transaction_id": current["id"]}
        paths = [self.context.files.path(root, entry) for entry in plan["entries"]]
        with self.context.files.access(root, paths, write=True, cancel=cancel):
            for entry, path in zip(plan["entries"], paths):
                if not matches(load(path), entry["before"]):
                    raise FileError("file_changed", "确认期间文件已变化，未覆盖；请重新读取。")
                if entry["after"] is not None:
                    self.context.security.run("before_write", [(entry["path"], contents(entry["after"]))], local_only=entry["local_only"])
            result = self.context.files.commit(root, plan["entries"], self.context.projects.authorization.request_id, cancel)
            if result.get("success") and plan.get("source") is not None:
                try:
                    result["directories_preserved"] = self.context.files.cleanup(root, plan["source"], directories=True)
                    plan["source"]["restored_by"] = result["transaction_id"]
                    self.context.files.journal.save(plan["source"])
                except Exception:
                    result["cleanup_pending"] = True
            for path in paths:
                self.reads.pop(str(path), None)
            if result.get("success") and name == "move":
                result.update(source=args["source"], destination=args["destination"], moved=True)
            if result.get("success") and name not in {"file_batch", "file_restore", "patch", "move"}:
                entry = plan["entries"][0]
                if entry["after"] is None:
                    result["deleted"] = str(paths[0])
                else:
                    result.update(path=str(paths[0]), created=entry["before"] is None, sha256=entry["after"]["sha256"])
            return result

    async def apply(self, action):
        try:
            return await blocking(lambda cancel: self.apply_sync(action, cancel))
        except (FileError, SecurityViolation) as error:
            return error.result()
        except Exception:
            return {"error": "file_operation_failed", "message": "文件锁或恢复记录检查失败，未继续修改；请查看 file_history 核对状态。"}

    async def history(self, args):
        def read_history(cancel):
            root = self.context.root(args)
            with self.context.files.locks.repository(root, shared=True, cancel=cancel):
                records = self.context.files.journal.summaries(root, limit=args.get("limit", 20))
                return {"transactions": [{"transaction_id": item["id"], "status": item["status"], "created": item["created"],
                    "files": [self.context.mask(entry["path"]) for entry in item["entries"]],
                    "conflicts": [self.context.mask(path) for path in (item.get("conflicts") or [])],
                    "restored_by": item.get("restored_by")} for item in records]}
        return await blocking(read_history)
