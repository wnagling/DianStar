"""Bounded display receipts, separate from model messages and execution grants."""
from copy import deepcopy


LIMIT = 256
TEXT_LIMITS = {"id": 200, "call_id": 200, "tool": 160, "title": 200, "target": 600,
               "parameters": 4000, "summary": 600, "output": 3000}
STATUSES = {"running", "success", "failed", "not_executed", "cancelled", "interrupted", "warning"}


def valid_activity(records):
    return (isinstance(records, list) and len(records) <= LIMIT and all(isinstance(item, dict)
        and set(item) == {*TEXT_LIMITS, "status", "duration_ms"}
        and all(isinstance(item[key], str) and len(item[key]) <= limit for key, limit in TEXT_LIMITS.items())
        and bool(item["id"]) and isinstance(item["status"], str) and item["status"] in STATUSES
        and (item["duration_ms"] is None or type(item["duration_ms"]) is int and 0 <= item["duration_ms"] <= 86400000)
        for item in records))


class ToolActivity:
    def __init__(self):
        self.records = []

    def consume(self, event):
        identifier = event.get("event_id") or str(event.get("request_id", "")) + ":" + str(event.get("call_id", event.get("tool", "")))
        identifier = str(identifier)[:200]
        item = next((item for item in self.records if item["id"] == identifier), None)
        if item is None:
            item = {key: "" for key in TEXT_LIMITS}
            item.update(id=identifier, status="running", duration_ms=None)
            self.records.append(item)
            del self.records[:-LIMIT]
        for key, limit in TEXT_LIMITS.items():
            source = event.get(key)
            if isinstance(source, str):
                item[key] = source[:limit]
        item["id"] = identifier
        item["title"] = item["title"] or event.get("activity") or "调用工具"
        item["title"] = str(item["title"])[:200]
        item["status"] = (event.get("status") if isinstance(event.get("status"), str) and event["status"] in STATUSES else
            "running" if event.get("phase") == "started" else "success" if event.get("success", False) else "failed")
        duration = event.get("duration_ms")
        if type(duration) is int and 0 <= duration <= 86400000:
            item["duration_ms"] = duration
        return item

    def interrupt(self):
        for item in self.records:
            if item["status"] == "running":
                item.update(status="interrupted", summary="本轮已结束，但未取得这项操作的完成结果，请核对任务记录。")

    def snapshot(self):
        return deepcopy(self.records)
