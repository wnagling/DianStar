"""Git operation receipts are not evidence of reading repository contents."""
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import quote, urlsplit

from dianstar.tools.local.web_reader import WebError, public_url


def _repository_key(value):
    if not isinstance(value, str):
        return None
    windows = PureWindowsPath(value)
    if windows.is_absolute():
        return windows.as_posix().lower()
    posix = PurePosixPath(value)
    return posix.as_posix() if posix.is_absolute() else None


def _web_key(value):
    if not isinstance(value, str):
        return None
    try:
        url = public_url(value).copy_with(fragment=None)
        if url.query:
            return None
        parts = url.path.strip("/").split("/")
        if url.host == "github.com" and len(parts) >= 2:
            parts[:2] = [part.lower() for part in parts[:2]]
            url = url.copy_with(path="/" + "/".join(parts))
        return str(url)
    except (ValueError, WebError):
        return None


def git_target_url(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        value = "https://github.com/" + value.split(":", 1)[1]
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "ssh" and parsed.hostname == "github.com" and parsed.username == "git":
            if parsed.password or parsed.query or parsed.fragment or parsed.port not in {None, 22}:
                return None
            value = "https://github.com" + parsed.path
        key = _web_key(value)
        if key is None:
            return None
        parsed = urlsplit(key)
        path = parsed.path.rstrip("/").removesuffix(".git")
        if parsed.hostname == "github.com" and not re.fullmatch(r"/[a-z0-9_.-]+/[a-z0-9_.-]+", path):
            return None
        return key[:len(key) - len(parsed.path)] + path
    except ValueError:
        return None


class GitOperationEvidence:
    def __init__(self):
        self.targets = {}
        self.references = {}

    def _target(self, url):
        return self.targets.setdefault(url, {"configured": False, "checks": {}})

    def observe(self, name, arguments, result, *, historical=False):
        if result.get("server") != "git":
            return result
        result = {key: value for key, value in result.items() if key != "operation_evidence"}
        if name == "mcp_git__git_remotes" and result.get("tool") == "git_remotes":
            if result.get("error") or result.get("truncated"):
                return result
            try:
                items = json.loads(result.get("content", ""))
            except (TypeError, ValueError):
                return result
            if not isinstance(items, list) or len(items) > 30:
                return result
            urls = []
            for item in items:
                if not isinstance(item, dict) or item.get("error"):
                    continue
                for field in ("fetch", "push"):
                    url = git_target_url(item.get(field))
                    if url:
                        target = self._target(url)
                        target["configured"] = True
                        target["historical"] = historical
                        urls.append(url)
            if urls:
                return {**result, "operation_evidence": {"kind": "git_configuration", "targets": sorted(set(urls)),
                        "remote_verified": False, "web_content_read": False, "historical": historical}}
            return result
        operation = name.removeprefix("mcp_git__")
        if (name not in {"mcp_git__git_push", "mcp_git__git_publish"}
                or result.get("tool") != operation or result.get("operation") != operation
                or _repository_key(arguments.get("repository")) is None
                or _repository_key(result.get("repository")) != _repository_key(arguments["repository"])
                or result.get("remote") != arguments.get("remote", "origin")
                or result.get("branch") != arguments.get("branch")
                or not isinstance(result.get("branch"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,150}", result["branch"])
                or not isinstance(result.get("remote_url"), str) or not result["remote_url"]):
            return result
        commit = result.get("commit")
        verified = (not result.get("error") and result.get("success") is True
                    and result.get("remote_verified") is True and isinstance(commit, str)
                    and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", commit) is not None)
        receipt = {"kind": "git_target", "remote_url": result["remote_url"], "branch": result["branch"],
                   "commit": commit if verified else None, "remote_verified": verified,
                   "web_content_read": False, "historical": historical}
        url = git_target_url(result["remote_url"])
        if url:
            self._target(url)["checks"][result["branch"]] = receipt
            if urlsplit(url).hostname == "github.com":
                branch_url = url + "/tree/" + quote(result["branch"], safe="/")
                self.references[_web_key(branch_url)] = (url, result["branch"])
                if verified:
                    self.references[_web_key(url + "/commit/" + commit)] = receipt
        return {**result, "operation_evidence": receipt}

    @staticmethod
    def _description(receipt):
        if receipt["remote_verified"]:
            label = f"{receipt['branch']} 分支的远程提交已通过 Git 核对（{receipt['commit'][:12]}）"
        else:
            label = f"{receipt['branch']} 分支的操作目标已记录，远程结果尚未核对成功"
        if receipt["historical"]:
            label += "；来自历史 Git 操作，非本轮新核对"
        return label

    def describe(self, value):
        reference = self.references.get(_web_key(value))
        if isinstance(reference, dict):
            return self._description(reference) + "；这是当次操作记录，不表示当前分支状态或仓库内容已读"
        if isinstance(reference, tuple):
            receipt = self.targets[reference[0]]["checks"][reference[1]]
            return self._description(receipt) + "；不表示已读取该仓库内容"
        target = self.targets.get(git_target_url(value))
        if target is None:
            return None
        if target["checks"]:
            return "；".join(self._description(receipt) for receipt in target["checks"].values()) + "；不表示已读取该仓库内容"
        label = "地址来自 Git 远程配置，不能据此认定推送成功或仓库内容已读"
        if target.get("historical"):
            label += "；来自历史配置读取记录"
        return label
