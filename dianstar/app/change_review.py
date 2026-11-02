"""Track actual evidence and schedule model reflection without inventing passes."""
import json

from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.tools.local.files.io import blocking
from dianstar.tools.local.files.inspection import inspect_changes


REVIEW_TOOLS = {"review_changes", "record_review", "task_status"}


class ChangeReview:
    def __init__(self, context, log):
        self.context, self.log = context, log
        self.receipts, self.evidence = [], {}
        self.generation = 0
        self.inspection, self.assessment = None, None
        self.coverage = []
        self.command_changes = []
        self.net_unchanged = False

    def observe(self, observation, registration):
        action, result = observation.action, observation.result
        if action.call_id and (action.name != "review_changes" or result.get("error")):
            self.evidence.pop(action.call_id, None)
        if registration is None:
            return
        manager = getattr(self.context(), "storage", None)
        scratch = bool(manager and action.arguments.get("repository") in manager.manager.scratch_roots)
        if "project.files" in registration.effects and not scratch:
            identifier = result.get("transaction_id") or result.get("recovery", {}).get("transaction_id")
            if identifier:
                receipt = {"repository": action.arguments["repository"], "transaction_id": identifier}
                if receipt not in self.receipts:
                    self.receipts.append(receipt)
                self.generation += 1
                self.assessment = None
                self.net_unchanged = False
        if "project.command" in registration.effects and not scratch:
            audit = result.get("security_review", {})
            if audit.get("changed_files") or audit.get("complete") is False:
                self.command_changes.append({"call_id": action.call_id, "repository": action.arguments.get("repository"),
                    "files": audit.get("changed_files", []), "complete": audit.get("complete", False),
                    "scope": "命令副作用，不能视为文件工具已保存的精确可恢复差异。"})
                self.generation += 1
                self.assessment = None
                self.net_unchanged = False
        if "project.git" in registration.effects and result.get("success"):
            self.command_changes.append({"call_id": action.call_id, "repository": action.arguments.get("repository"),
                "tool": action.name, "scope": "Git 工作区变更；需结合本地 Git diff 核对，不属于文件工具的自动补偿事务。"})
            self.generation += 1
            self.assessment = None
            self.net_unchanged = False
        if result.get("error"):
            return
        kind = None
        if action.name == "read" and result.get("type") == "file":
            kind = "readback"
        elif action.name in {"grep", "glob"}:
            kind = "search" if not result.get("truncated") and result.get("complete", True) else "partial_search"
        elif action.name in {"workspace_run", "bash", "shell"} and result.get("validation_kind"):
            kind = result["validation_kind"]
        elif action.name in {"workspace_run", "bash", "shell"} and result.get("execution_kind") in {"go_run", "go_build"}:
            kind = result["execution_kind"]
        elif action.name in {"mcp_git__git_diff", "mcp_git__git_diff_refs"}:
            kind = "git_diff"
        if kind and action.call_id:
            if scratch:
                kind = "scratch_" + kind
            self.evidence[action.call_id] = {"call_id": action.call_id, "tool": action.name, "kind": kind,
                "generation": self.generation, "success": result.get("success", True),
                "repository": action.arguments.get("repository"), "path": action.arguments.get("path"),
                "exit_code": result.get("exit_code"), "check_key": json.dumps([action.arguments.get("repository"),
                    action.arguments.get("argv", action.arguments.get("command")), kind], ensure_ascii=False)}

    async def inspect(self, call_id=None, *, offset=0, limit=20000):
        generation = self.generation
        snapshot = await blocking(lambda cancel: inspect_changes(self.context(), self.receipts, offset=offset, limit=limit))
        snapshot["generation"] = generation
        snapshot["evidence"] = [item for item in self.evidence.values() if item["generation"] == generation][-32:]
        snapshot["instructions"] = ("根据任务目标判断继续检查、修复、恢复或交付。写入成功/哈希一致不证明逻辑正确；"
            "验证结果只能引用实际成功工具的 call_id。无充分验证时选择 uncertain 并说明限制。"
            "修复或恢复后重新 review_changes；单纯提交推送不附加测试。")
        snapshot["command_changes"] = self.command_changes
        if self.inspection is None or self.inspection["generation"] != generation or self.inspection["version"] != snapshot["version"]:
            self.coverage = []
        self.coverage.append((offset, min(offset + limit, snapshot["diff_total"])))
        self.inspection = snapshot
        if call_id:
            self.evidence[call_id] = {"call_id": call_id, "tool": "review_changes", "kind": "diff",
                "generation": generation, "success": not snapshot["issues"]}
            snapshot["evidence_call_id"] = call_id
            snapshot["evidence"] = [item for item in self.evidence.values() if item["generation"] == generation][-32:]
        self.log.append({"event": "inspection", "generation": generation, "snapshot": snapshot})
        return snapshot

    async def record(self, action):
        args = action.arguments
        if self.inspection is None or self.inspection["generation"] != self.generation:
            return {"error": "review_required", "message": "请先 review_changes 获取当前修改和差异。"}
        fresh = await blocking(lambda cancel: inspect_changes(self.context(), self.receipts))
        if fresh["version"] != self.inspection["version"]:
            self.inspection = None
            return {"error": "review_stale", "message": "检查后文件已变化，请重新检查。"}
        selected = [self.evidence.get(identifier) for identifier in args["evidence_call_ids"]]
        if any(item is None or item["generation"] != self.generation or not item["success"] for item in selected):
            return {"error": "invalid_review_evidence", "message": "只能引用本轮当前修改之后实际成功的工具结果，不能凭文字声明通过。",
                    "available_evidence": [item for item in self.evidence.values() if item["generation"] == self.generation and item["success"]][-32:]}
        if args["decision"] == "keep" and any(not item["success"] for item in self.current_checks()):
            return {"error": "verification_failed", "message": "当前修改后的检查仍有失败，请修复、恢复或选择 uncertain 说明未完成验收。"}
        covered = 0
        for start, end in sorted(self.coverage):
            if start <= covered:
                covered = max(covered, end)
        if args["decision"] == "keep" and (fresh["issues"] or not selected or covered < fresh["diff_total"]):
            return {"error": "review_incomplete", "message": "存在冲突、截断或缺少证据，不能确认完成；请继续检查或选择 uncertain。"}
        self.assessment = {"generation": self.generation, "version": fresh["version"], **args,
                           "verification": "evidence_recorded" if selected else "not_verified"}
        self.log.append({"event": "reflection", "assessment": self.assessment})
        return {"recorded": True, "decision": args["decision"], "verification": self.assessment["verification"],
                "message": "记录的是模型判断和实际证据，不自动将业务功能标为测试通过。"}

    async def ready(self):
        if not self.receipts and not self.command_changes:
            return True
        fresh = await blocking(lambda cancel: inspect_changes(self.context(), self.receipts))
        if not fresh["files"] and not fresh["issues"] and not self.command_changes:
            self.net_unchanged = True
            return True
        self.net_unchanged = False
        return bool(self.assessment and self.assessment["generation"] == self.generation
                    and self.assessment["version"] == fresh["version"] and self.assessment["decision"] in {"keep", "uncertain"})

    def summary(self):
        if not self.receipts and not self.command_changes:
            return ""
        if self.net_unchanged:
            return f"修改核对：本轮文件改动已恢复至原状态。任务记录：{self.log.id}。"
        checks = self.current_checks()
        tested = [item for item in checks if item["kind"] in {"unittest", "pytest"}]
        ran = [item for item in checks if item["kind"] == "go_run"]
        status = "已记录模型复核" if self.assessment and self.assessment["decision"] == "keep" else "尚未完成验收"
        details = "相关测试命令退出成功" if tested and all(item["success"] for item in tested) else "未取得当前修改后的测试通过记录"
        if tested and any(not item["success"] for item in tested):
            details = "当前修改后的测试存在失败"
        elif not tested and ran:
            details = "程序运行命令退出成功，输出由模型核对" if all(item["success"] for item in ran) else "程序运行存在失败"
        return f"修改核对：{status}；{details}。任务记录：{self.log.id}。"

    def current_checks(self):
        checks = {}
        for item in self.evidence.values():
            if item["generation"] == self.generation and item["kind"] not in {"diff", "readback", "search", "partial_search", "git_diff"}:
                checks[item.get("check_key", item["call_id"])] = item
        return list(checks.values())

    def registrations(self):
        async def inspect(action):
            return Observation(action, await self.inspect(action.call_id, **action.arguments))
        async def record(action):
            return Observation(action, await self.record(action))
        async def status(action):
            if action.arguments.get("task_id"):
                return Observation(action, await blocking(lambda cancel: self.log.status(
                    action.arguments["task_id"], action.arguments.get("offset", 0), action.arguments.get("limit", 5))))
            return Observation(action, {"task_id": self.log.id, "generation": self.generation, "receipts": self.receipts,
                "evidence": list(self.evidence.values())[-32:], "assessment": self.assessment, "previous_tasks": self.log.previous()})
        definitions = {
            "review_changes": ({"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 24000}}, [],
                "读取本轮真实修改记录的脱敏 diff、版本、恢复编号与验证证据，供 Reflect 判断；不修改文件。diff 按字符分页，next_offset 非空时继续读取。", inspect),
            "record_review": ({"decision": {"type": "string", "enum": ["keep", "repair", "restore", "uncertain"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                "evidence_call_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True, "maxItems": 32}},
                ["decision", "reason", "evidence_call_ids"], "根据 review_changes 和真实证据记录 Reflect 判断。不会执行恢复，也不能凭声明将测试标为通过。", record),
            "task_status": ({"task_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}, "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, [],
                "查看当前任务编号、证据和复核状态，或按 task_id 分页读取同会话的历史任务结果；不恢复旧授权。", status),
        }
        return {name: ToolRegistration({"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}},
            "workspace.read", handler, required_tools=("read",)) for name, (properties, required, description, handler) in definitions.items()}
