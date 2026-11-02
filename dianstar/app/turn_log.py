"""Per-result durable receipts, independent of model completion or chat saving."""
from contextlib import closing
import json
import time

from dianstar.platform.file_state import private_directory, private_file, protect, state_directory
from dianstar.sessions.task_database import connect_tasks


class TurnLog:
    def __init__(self, identifier, scanner, *, conversation_id=None):
        self.id, self.scanner, self.conversation_id = identifier, scanner, conversation_id
        self.path = private_directory(state_directory()) / "tasks.sqlite3"
        with private_file(self.path):
            pass
        with closing(self.connect()) as connection, connection:
            connection.execute("INSERT INTO turns VALUES (?, ?, ?, 'running')", (identifier, conversation_id, time.time()))

    def connect(self):
        return connect_tasks(self.path.parent)

    def safe(self, value):
        if isinstance(value, str):
            return self.scanner.redact(value, env_style=True, preserve_lines=True)
        if isinstance(value, dict):
            return {str(key): self.safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.safe(item) for item in value]
        return value

    def append(self, event):
        safe = self.safe(event)
        encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > 512 * 1024:
            safe = {"event": event.get("event"), "call_id": event.get("call_id"), "tool": event.get("tool"),
                    "result_truncated": True, "message": "结果超过任务记录大小限制，原工具的恢复记录继续保留。"}
            encoded = json.dumps(safe, ensure_ascii=False).encode()
        payload = protect(encoded)
        with closing(self.connect()) as connection, connection:
            connection.execute("INSERT INTO events(turn_id,payload) VALUES (?,?)", (self.id, payload))

    def finish(self, state):
        with closing(self.connect()) as connection, connection:
            connection.execute("UPDATE turns SET state=? WHERE id=?", (state, self.id))

    def events(self):
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT payload FROM events WHERE turn_id=? ORDER BY seq", (self.id,)).fetchall()
        return [json.loads(protect(row[0], decrypt=True)) for row in rows]

    def previous(self):
        if not self.conversation_id:
            return []
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT id,state FROM turns WHERE conversation_id=? AND id!=? "
                "AND state IN ('running','failed','cancelled','budget_exhausted','unverified') ORDER BY created DESC LIMIT 5",
                (self.conversation_id, self.id)).fetchall()
        return [{"task_id": identifier, "state": state, "historical": True} for identifier, state in rows]

    def status(self, identifier, offset=0, limit=5):
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT conversation_id,state FROM turns WHERE id=?", (identifier,)).fetchone()
            if row is None or (identifier != self.id and (not self.conversation_id or row[0] != self.conversation_id)):
                return {"error": "task_not_found"}
            rows = connection.execute("SELECT payload FROM events WHERE turn_id=? ORDER BY seq LIMIT ? OFFSET ?",
                (identifier, limit + 1, offset)).fetchall()
        events, size = [], 0
        for (payload,) in rows[:limit]:
            event = json.loads(protect(payload, decrypt=True))
            encoded = json.dumps(event, ensure_ascii=False)
            if size + len(encoded) > 20000:
                event = {"event": event.get("event"), "call_id": event.get("call_id"), "tool": event.get("tool"), "details_omitted": True}
                encoded = json.dumps(event)
            events.append(event)
            size += len(encoded)
        return {"task_id": identifier, "state": row[1], "historical": identifier != self.id, "events": events,
                "next_offset": offset + len(events) if len(rows) > limit else None,
                "scope": "历史结果不是当前状态，也不恢复旧授权。"}
