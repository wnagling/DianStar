"""Model-facing managed storage contracts, always executed through the gateway."""
from copy import deepcopy
import json
import sqlite3
from pathlib import Path
import time

from dianstar.core.registry import ToolRegistration
from dianstar.core.resources import ResourceClaim
from dianstar.core.types import Observation
from dianstar.security.authorization import request_key
from dianstar.security.content_guard import SecurityViolation
from dianstar.security.gateway import ToolPreparationError
from ..files.io import blocking
from .manager import StorageManager
from .tree import StorageError

PREFIX = "workspace_"
NAMES = frozenset(PREFIX + name for name in ("trash_put", "trash_list", "trash_restore", "trash_purge", "storage_recover", "storage_pin",
    "storage_cleanup", "scratch_create", "scratch_open", "scratch_list"))


class StorageTools:
    def __init__(self, context, manager=None):
        self.context = context
        self.manager = manager or StorageManager(context)
        self.manager.context = context
        self.prepared = {}

    def roots(self, name, args):
        paths, managed = [], []
        if name == "trash_put":
            paths = args["paths"]
        elif name in {"scratch_create"}:
            # Scratch creation changes managed data, not the original project.
            return [], []
        else:
            identifiers = args.get("ids", [args["id"]] if "id" in args else [])
            if name == "storage_cleanup":
                identifiers = self.manager.cleanup_ids(args.get("limit", 20), small_only=args.get("small_only", False))
                identifiers += [record["id"] for record in self.manager.abandoned()]
            for identifier in identifiers:
                record = self.manager.record(identifier)
                if name == "storage_recover" and record.get("batch") and self.manager.recovery_phase(record) == "deposit":
                    records = [item for item in self.manager.journal.records("trash") if item.get("batch") == record["batch"]
                        and item["status"] in {"depositing", "conflict"} and self.manager.recovery_phase(item) == "deposit"]
                else:
                    records = [record]
                for item in records:
                    if item["kind"] == "scratch":
                        managed.append(item["directory"])
                    else:
                        paths.append(item["source"])
                        if item.get("restore"):
                            paths.append(item["restore"]["destination"])
                if name == "trash_restore":
                    paths.append(args.get("destination", record["source"]))
        return paths, managed

    def plan(self, name, args, cancel=None):
        manager = self.manager
        if name == "trash_put":
            return manager.plan_put(args, cancel)
        if name == "trash_restore":
            return manager.plan_restore(args, cancel)
        if name == "trash_purge":
            return manager.plan_purge(args["ids"], cancel)
        if name == "storage_cleanup":
            return {**manager.plan_purge(manager.cleanup_ids(args.get("limit", 20), small_only=args.get("small_only", False)), cancel, expired=True),
                    "abandoned": manager.abandoned()}
        if name == "storage_recover":
            return manager.plan_recover(args, cancel)
        if name in {"scratch_open", "storage_pin"}:
            record = manager.record(args["id"])
            manager.permitted_record(record)
            if name == "scratch_open":
                if record["kind"] != "scratch" or record["status"] not in {"active", "released"}:
                    raise StorageError("scratch_unavailable", "这条临时工作区记录不可打开。")
                manager.scratch_path(record)
                self.context.security.run("before_scratch", [("scratch", record["project"].encode())])
            return {"record": record}
        if name == "scratch_create":
            root = self.context.root(args)
            self.context.security.run("before_scratch", [("scratch", str(root).encode())])
            return {"repository": str(root)}
        raise StorageError("storage_unknown_action", "不支持这项存储操作。")

    def describe(self, name, plan):
        manager = self.manager
        if name == "trash_put":
            lines = ["是否将以下内容移入 DianStar 回收站？", *[item["source"] for item in plan["entries"]],
                f"合计 {sum(item['before']['files'] for item in plan['entries'])} 个文件，{sum(item['before']['size'] for item in plan['entries'])} 字节。",
                "保留30天，可恢复；包括目录内 Git 历史和未提交内容，不永久删除。"]
        elif name == "trash_restore":
            lines = ["是否从回收站恢复这项内容？", "原位置：" + plan["record"]["source"], "恢复到：" + plan["destination"], "目标已存在时停止，不覆盖新内容。"]
        elif name in {"trash_purge", "storage_cleanup"}:
            lines = ["是否永久清理以下本机副本？" if name == "trash_purge" else "清理已超过30天的到期本机副本", *[
                (item["record"].get("source") or item["record"].get("directory")) + "（" + item["record"]["id"] + "）" for item in plan["entries"]],
                "只删除回收/临时副本，不删除原位置或已恢复的文件；清理后无法通过这些副本恢复。"]
        elif name == "storage_recover":
            title = {"deposit": "是否将这批未完成回收的内容恢复到原位置？", "restore": "是否核对并续办这项恢复？",
                "purge": "是否继续永久清理这些本机副本？", "leftovers": "是否清理已核对完整版本的冗余副本？"}[plan["phase"]]
            lines = ["是否保留这批操作的当前现场与全部副本？" if plan["mode"] == "keep_current" else title,
                *[(item.get("source") or item.get("directory", "")) + "（" + item["id"] + "）" for item in plan["siblings"]],
                "只保留，不覆盖或删除文件。" if plan["mode"] == "keep_current" else
                "清理后不能通过这些副本恢复；原位置和已恢复文件不删除。" if plan["phase"] == "purge" else
                "按持久记录核对并续办；有冲突则保留现场，不强制覆盖。"]
        elif name == "storage_pin":
            lines = ["是否" + ("保留" if plan.get("pinned", True) else "取消保留") + "这条本机存储记录？", "编号：" + plan["record"]["id"], "保留中的记录不会被到期清理。"]
        else:
            lines = ["准备本次任务的临时工作区", "项目：" + plan.get("repository", plan.get("record", {}).get("project", "")),
                "不修改项目文件；临时文件按会话与任务隔离，结束后保留30天。"]
        return self.context.mask("\n\n".join(lines))

    @staticmethod
    def error(error):
        if isinstance(error, SecurityViolation):
            return error.result()
        return {"error": getattr(error, "code", "storage_operation_failed"),
                "message": getattr(error, "message", "本机存储检查或操作未完成；请查询记录核对状态，未强制覆盖文件。")}

    async def preview(self, full_name, args):
        name = full_name.removeprefix(PREFIX)
        try:
            self.context.projects.require(write=True)
            def prepare(cancel):
                paths, managed = self.roots(name, args)
                with self.manager.mutation(paths, cancel, managed, check_ready=name != "scratch_open"):
                    return self.plan(name, args, cancel)
            plan = await blocking(prepare)
            self.prepared[request_key(full_name, args)] = (time.monotonic(), plan)
            return self.describe(name, {**plan, "pinned": args.get("pinned", True)})
        except (ValueError, OSError, sqlite3.Error, SecurityViolation) as error:
            result = self.error(error)
            raise ToolPreparationError(result["error"], result["message"], findings=result.get("findings")) from None

    async def apply(self, action):
        name, args = action.name.removeprefix(PREFIX), action.arguments
        saved = self.prepared.pop(request_key(action.name, args), None)
        if not saved or time.monotonic()-saved[0] > 600:
            return Observation(action, {"error": "storage_not_prepared", "message": "操作计划已失效，请重新查询。"})
        try:
            def execute(cancel):
                self.context.projects.require(write=True)
                paths, managed = self.roots(name, args)
                with self.manager.mutation(paths, cancel, managed, check_ready=name != "scratch_open"):
                    if cancel.is_set() or not self.context.projects.authorization.active:
                        raise StorageError("storage_cancelled", "操作已取消，未继续。")
                    current = self.plan(name, args, cancel)
                    if current != saved[1]:
                        raise StorageError("storage_changed", "确认期间内容或恢复记录已变化，未执行，请重新核对。")
                    if name == "trash_put":
                        return self.manager.put(current, cancel)
                    if name == "trash_restore":
                        return self.manager.restore(current, cancel)
                    if name in {"trash_purge", "storage_cleanup"}:
                        return self.manager.purge(current, cancel)
                    if name == "storage_recover":
                        return self.manager.recover(current)
                    if name == "storage_pin":
                        record = deepcopy(current["record"])
                        record["pinned"] = args["pinned"]
                        self.manager.journal.save(record)
                        return {"success": True, **self.manager.public(record)}
                    if name == "scratch_create":
                        return self.manager.create_scratch(args["repository"])
                    return self.manager.open_scratch(args["id"])
            result = await blocking(execute)
        except (ValueError, OSError, sqlite3.Error, SecurityViolation) as error:
            result = self.error(error)
        return Observation(action, result)

    async def query(self, action):
        try:
            self.context.projects.require()
            function = self.manager.list_trash if action.name == PREFIX + "trash_list" else self.manager.list_scratch
            result = await blocking(lambda cancel: function(action.arguments.get("limit", 20)))
        except (ValueError, OSError, sqlite3.Error) as error:
            result = self.error(error)
        return Observation(action, result)

    def registrations(self):
        if not self.context.projects.settings.enabled:
            return {}
        identifier = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
        path = {"type": "string", "minLength": 1, "description": "明确的本机绝对路径，不支持通配符。"}
        limit = {"type": "integer", "minimum": 1, "maximum": 20}
        definitions = {
            "trash_put": ("将明确的文件或整个目录移入本机回收站，包含二进制与.git；保留30天，可恢复。普通删除优先使用，多个目标一次确认。禁止链接和应用自身数据。", {"paths": {"type": "array", "items": path, "minItems": 1, "maxItems": 20}}, ["paths"]),
            "trash_list": ("查询当前允许范围内的回收记录、恢复编号、状态与到期时间，不读取备份正文。", {"limit": limit}, []),
            "trash_restore": ("按回收编号恢复文件或目录；原位置存在时停止，可明确提供新destination，不覆盖。与file_restore的文件事务编号不同。", {"id": identifier, "destination": path}, ["id"]),
            "trash_purge": ("永久清理明确编号的回收/临时副本，无法撤销，未满30天也可能清空；仅用户明确要求永久清理时使用，普通删除用trash_put。保留或活跃数据不可清理。", {"ids": {"type": "array", "items": identifier, "minItems": 1, "maxItems": 20, "uniqueItems": True}}, ["ids"]),
            "storage_recover": ("核对重启后未完成的回收/恢复/清理记录并安全续办；keep_current仅保留现状与副本，不改文件。冲突不强制覆盖。", {"id": identifier, "mode": {"type": "string", "enum": ["resume", "keep_current"]}}, ["id"]),
            "storage_pin": ("为本机存储记录设置或取消保留，保留后不参与到期清理。", {"id": identifier, "pinned": {"type": "boolean"}}, ["id", "pinned"]),
            "storage_cleanup": ("仅清理已满30天且无活动任务、恢复冲突或保留标记的本机副本，不提前清空。small_only用于有界闲时维护。", {"limit": limit, "small_only": {"type": "boolean"}}, []),
            "scratch_create": ("为项目创建本次任务的临时工作区，用于验证脚本和中间文件，不改源码。返回repository可交给read/write/bash等；任务结束后保留30天。", {"repository": path}, ["repository"]),
            "scratch_open": ("重新打开当前会话以前的临时工作区；不会访问其他会话，另一任务仍在使用时拒绝。", {"id": identifier}, ["id"]),
            "scratch_list": ("列出当前会话的临时工作区、原项目、状态和保留期限，不展示文件正文。", {"limit": limit}, [])}
        result = {}
        for name, (description, properties, required) in definitions.items():
            query = name in {"trash_list", "scratch_list"}
            if not query and not self.context.projects.settings.write_enabled:
                continue
            full_name = PREFIX + name
            schema = {"type": "function", "function": {"name": full_name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}
            async def preview(args, full_name=full_name):
                return await self.preview(full_name, args)
            automatic = (lambda args: True) if name in {"scratch_create", "scratch_open"} else (lambda args: args.get("small_only", False)) if name == "storage_cleanup" else None
            dependencies = ("workspace_delete",) if name in {"trash_put", "trash_purge", "storage_cleanup"} else ("write", "workspace_delete") if name == "trash_restore" else ()
            result[full_name] = ToolRegistration(schema, "workspace.read" if query else "workspace.write", self.query if query else self.apply,
                None if query else preview, required_tools=dependencies, auto_approve=automatic,
                effects=() if query else ("filesystem.storage",),
                resources=lambda action, query=query: (ResourceClaim("runtime", write=not query), ResourceClaim("managed-storage", write=not query)))
        return result
