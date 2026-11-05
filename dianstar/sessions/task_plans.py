"""Private, revision-bound current plan per conversation; no execution grants."""
from contextlib import closing
import json

from dianstar.platform.file_state import state_directory, protect
from .task_database import connect_tasks


class PlanConflict(ValueError):
    pass


class TaskPlanStore:
    def __init__(self, directory=None):
        self.directory = directory or state_directory()

    def load(self, conversation_id):
        if not conversation_id or not (self.directory / "tasks.sqlite3").exists():
            return None
        with closing(connect_tasks(self.directory)) as connection:
            row = connection.execute("SELECT payload FROM todo_plans WHERE conversation_id=?", (conversation_id,)).fetchone()
        if not row:
            return None
        plan = json.loads(protect(row[0], decrypt=True))
        statuses = {"pending", "in_progress", "pending_review", "completed", "blocked", "waiting_user"}
        if (not isinstance(plan, dict) or plan.get("version") != 1
                or type(plan.get("revision")) is not int or plan["revision"] < 1
                or not isinstance(plan.get("title"), str) or not isinstance(plan.get("source_request"), str)
                or not isinstance(plan.get("steps"), list) or not 2 <= len(plan["steps"]) <= 12
                or any(not isinstance(step, dict) or not isinstance(step.get("id"), str)
                    or not isinstance(step.get("title"), str) or not isinstance(step.get("status"), str) or step["status"] not in statuses
                    or not isinstance(step.get("evidence"), list) for step in plan["steps"])):
            raise ValueError("待办记录格式损坏，未恢复。")
        return plan

    def save(self, conversation_id, expected_revision, plan):
        payload = protect(json.dumps(plan, ensure_ascii=False, allow_nan=False).encode())
        with closing(connect_tasks(self.directory)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision FROM todo_plans WHERE conversation_id=?", (conversation_id,)).fetchone()
            if (row[0] if row else 0) != expected_revision:
                raise PlanConflict("待办已变化，请重新读取后更新。")
            connection.execute("INSERT INTO todo_plans VALUES (?,?,?) ON CONFLICT(conversation_id) "
                "DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                (conversation_id, expected_revision + 1, payload))
