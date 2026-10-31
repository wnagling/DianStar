"""Non-secret account selection; actual credentials stay in the OS vault."""
from functools import lru_cache
import json
import os
from pathlib import Path
import tempfile

from dianstar.platform.github_credentials import LOGIN, NAMESPACE, read_stored_token

MAX_PENDING_CLEANUP = 16


def account_file(root):
    return Path(root) / ".local/github_account.json"


def validate_account(data):
    if not isinstance(data, dict) or set(data) - {"mode", "login", "namespace", "pending_cleanup"}:
        raise ValueError("GitHub 登录状态无效。")
    fields = set(data) - {"pending_cleanup"}
    plain = data.get("mode") in {"signed_out", "legacy"} and fields == {"mode"}
    if not plain and (fields != {"mode", "login", "namespace"} or data.get("mode") not in {"managed", "signed_out"}
            or not isinstance(data.get("login"), str) or not LOGIN.fullmatch(data["login"])
            or not isinstance(data.get("namespace"), str) or not NAMESPACE.fullmatch(data["namespace"])):
        raise ValueError("GitHub 登录状态无效。")
    pending = data.get("pending_cleanup", [])
    if (not isinstance(pending, list) or len(pending) > MAX_PENDING_CLEANUP
            or (data.get("mode") == "managed" and len(pending) >= MAX_PENDING_CLEANUP)):
        raise ValueError("GitHub 待清理记录无效。")
    seen = set()
    for entry in pending:
        if (not isinstance(entry, dict) or set(entry) != {"login", "namespace"}
                or not isinstance(entry["login"], str) or not LOGIN.fullmatch(entry["login"])
                or not isinstance(entry["namespace"], str) or not NAMESPACE.fullmatch(entry["namespace"])
                or entry["namespace"] in seen
                or (data.get("mode") == "managed" and entry["namespace"] == data["namespace"])):
            raise ValueError("GitHub 待清理记录无效。")
        seen.add(entry["namespace"])
    if (data.get("mode") == "signed_out" and "namespace" in data
            and data["namespace"] not in seen and len(pending) >= MAX_PENDING_CLEANUP):
        raise ValueError("GitHub 待清理记录无效。")
    result = {key: value for key, value in data.items() if key != "pending_cleanup"}
    if pending:
        result["pending_cleanup"] = [dict(entry) for entry in pending]
    return result


def load_account(root):
    path = account_file(root)
    if not path.exists():
        return {"mode": "legacy"}
    if path.resolve() != path.absolute() or path.stat().st_size > 8192:
        raise ValueError("GitHub 登录状态无法读取，请重新登录。")
    try:
        return validate_account(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError):
        raise ValueError("GitHub 登录状态无法读取，请重新登录。") from None


def save_account(root, data):
    data = validate_account(data)
    path = account_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != path.absolute():
        raise ValueError("GitHub 登录状态保存位置无效。")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=True)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    cached_token.cache_clear()


@lru_cache(maxsize=16)
def cached_token(namespace, login):
    return read_stored_token(namespace, login)
