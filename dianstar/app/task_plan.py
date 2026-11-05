"""Application-owned progress, evidence and lifecycle; UI is a subscriber."""
import json
import time

from dianstar.sessions.task_plans import TaskPlanStore, PlanConflict
from dianstar.tools.local.files.io import blocking


class TaskPlan:
    def __init__(self, conversation_id, question, scanner, *, notify=None, store=None):
        self.conversation_id, self.question, self.scanner = conversation_id, question, scanner
        self.store, self.notify = store or TaskPlanStore(), notify
        self.evidence, self.touched = {}, False
        self.saved_revision = None

    async def load(self):
        return await blocking(lambda cancel: self.store.load(self.conversation_id))

    def publish(self, plan):
        if self.notify:
            try:
                self.notify({"conversation_id": self.conversation_id, "plan": plan})
            except Exception:
                pass

    def observe(self, observation):
        action, result = observation.action, observation.result
        if action.name.startswith("todo_") or not action.call_id:
            return
        self.evidence.pop(action.call_id, None)
        if (result and not result.get("error") and result.get("success") is not False
                and result.get("complete") is not False and not result.get("timed_out")
                and not result.get("output_limit_exceeded") and result.get("exit_code") in {None, 0}):
            self.evidence[action.call_id] = action.name

    async def write(self, args):
        if not args["title"].isprintable() or any(not step["title"].isprintable() for step in args["steps"]):
            return {"error": "todo_invalid", "message": "待办标题须为单行可显示文字。"}
        previous = await self.load()
        revision = previous["revision"] if previous else 0
        if args["revision"] != revision:
            return {"error": "todo_conflict", "message": "待办已变化，请先 todo_read。"}
        if len({step["id"] for step in args["steps"]}) != len(args["steps"]):
            return {"error": "todo_invalid", "message": "步骤编号不能重复。"}
        if sum(step["status"] == "in_progress" for step in args["steps"]) > 1:
            return {"error": "todo_invalid", "message": "同一时间只保留一个当前步骤。"}
        safe = self.scanner.redact(json.dumps(args, ensure_ascii=False), env_style=True)
        if self.scanner.scan("todo-plan.txt", json.dumps(args, ensure_ascii=False).encode()) or safe != json.dumps(args, ensure_ascii=False):
            return {"error": "todo_sensitive", "message": "待办含敏感内容，请移除后重试。"}
        old = {step["id"]: step for step in previous["steps"]} if previous and previous["title"] == args["title"] else {}
        steps = json.loads(json.dumps(args["steps"]))
        for step in steps:
            if step["status"] == "completed":
                if old.get(step["id"]) == step:
                    continue
                if not step["evidence"] or any(key not in self.evidence for key in step["evidence"]):
                    return {"error": "todo_evidence_required", "message": "完成步骤必须引用本轮成功工具结果的 call_id；无法验证时使用 pending_review。"}
        plan = {"version": 1, "title": args["title"], "steps": steps, "revision": revision + 1,
            "source_request": previous["source_request"] if previous and args["title"] == previous["title"] else self.scanner.redact(self.question, env_style=True)[:2000],
            "updated": time.time(), "run_state": "active", "evidence_scope": "工具结果只证明对应操作成功，不证明整个业务目标正确，也不授予权限。"}
        try:
            await blocking(lambda cancel: self.store.save(self.conversation_id, revision, plan))
        except PlanConflict:
            return {"error": "todo_conflict", "message": "待办已变化，请先 todo_read。"}
        self.touched = True
        self.saved_revision = revision + 1
        self.publish(plan)
        return {"plan": plan}

    async def finish(self, state):
        if not self.touched:
            return
        plan = await self.load()
        if plan and plan["revision"] == self.saved_revision:
            revision = plan["revision"]
            plan["revision"] += 1
            plan["run_state"] = "completed" if all(step["status"] == "completed" for step in plan["steps"]) else state
            plan["updated"] = time.time()
            try:
                await blocking(lambda cancel: self.store.save(self.conversation_id, revision, plan))
            except PlanConflict:
                return
            self.publish(plan)
