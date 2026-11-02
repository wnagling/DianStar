"""Review only application-owned receipts; never use Git HEAD as user baseline."""
import difflib
import hashlib
import json
import os

from dianstar.security.content_guard import protected_path
from .recovery import contents, image, matches
from .text import load


def inspect_changes(context, receipts, *, offset=0, limit=20000):
    combined, issues = {}, []
    for receipt in receipts:
        try:
            root = context.root({"repository": receipt["repository"]})
            with context.files.access(root, [], recover=False):
                record = context.files.journal.get(root, receipt["transaction_id"])
            if record["status"] not in {"committed", "rolled_back"}:
                issues.append({"transaction_id": record["id"], "state": record["status"]})
            for entry in record["entries"]:
                key = (str(root), os.path.normcase(entry["path"]))
                if key not in combined:
                    combined[key] = {"repository": str(root), "path": entry["path"], "before": entry["before"],
                                     "local_only": entry.get("local_only", False), "transaction_ids": []}
                value = combined[key]
                value["entry"] = entry
                value["after"] = ((entry.get("undo") or entry["before"]) if record["status"] == "rolled_back" else entry["after"])
                value["transaction_ids"].append(record["id"])
        except Exception:
            issues.append({"transaction_id": receipt["transaction_id"], "state": "inspection_unavailable"})
    files, position = [], 0
    for value in combined.values():
        try:
            root = context.root({"repository": value["repository"]})
            path = context.files.path(root, value["entry"])
            with context.files.access(root, [path], recover=False):
                current = load(path)
                context.files.path(root, value["entry"])
            consistent = matches(current, value["after"])
            before, after = contents(value["before"]), contents(value["after"])
            active = before != after
            if not consistent:
                issues.append({"path": value["path"], "state": "file_changed_since_tool"})
            if not active and consistent:
                continue
            diff = "".join(difflib.unified_diff((before or b"").decode("utf-8-sig").splitlines(True),
                (after or b"").decode("utf-8-sig").splitlines(True),
                fromfile=value["path"] if before is not None else "/dev/null", tofile=value["path"] if after is not None else "/dev/null"))
            safe = context.mask_file(diff, value["path"])
            if protected_path(value["path"]):
                safe = "受保护的本机配置，仅提供修改与版本状态，不提供正文。"
            start, end = position, position + len(safe)
            displayed = safe[max(0, offset - start):max(0, min(end, offset + limit) - start)] if end > offset and start < offset + limit else ""
            position = end
            files.append({"repository": value["repository"], "path": value["path"],
                "moved_from": value["entry"].get("moved_from"), "moved_to": value["entry"].get("moved_to"),
                "operation": "delete" if after is None else "create" if before is None else "edit",
                "transaction_ids": value["transaction_ids"], "integrity": "matched" if consistent else "changed",
                "current_sha256": hashlib.sha256(current.data).hexdigest() if current else None,
                "version": list(current.version) if current else None,
                "diff": displayed, "diff_start": start, "diff_length": len(safe), "diff_truncated": len(displayed) < len(safe)})
        except Exception:
            issues.append({"path": value["path"], "state": "inspection_unavailable"})
    signature = hashlib.sha256(json.dumps({"files": [{key: item[key] for key in ("repository", "path", "version")} for item in files],
        "issues": issues}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"files": files, "issues": issues, "version": signature, "untrusted": True,
            "offset": offset, "diff_total": position, "next_offset": min(offset + limit, position) if offset + limit < position else None,
            "scope": "本轮工具记录的前后快照；不包含用户原有改动，也不代表业务功能已验证。"}
