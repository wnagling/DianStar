"""Application-side GitHub previews; only the gateway can approve execution."""
import asyncio
import json
import time
from urllib.parse import quote

import httpx

from dianstar.security.authorization import request_key
from dianstar.security.gateway import ToolPreparationError
from dianstar.tools.mcp.connection import MCPUnavailable
from dianstar.tools.mcp.results import normalize_result


class GitHubWrites:
    def __init__(self, adapter, authorization, connections, public_check):
        self.adapter, self.authorization, self.connections = adapter, authorization, connections
        self.public_check = public_check
        self.prepared = {}
        self.uncertain = set()
        self.attempts = 0

    @staticmethod
    def target_state(value):
        """Keep reviewable target state, excluding volatile API links/statistics."""
        if isinstance(value, list):
            return [GitHubWrites.target_state(item) for item in value]
        if not isinstance(value, dict):
            raise ValueError("invalid target state")
        fields = {"id", "number", "title", "body", "state", "draft", "updated_at", "commit_id",
                  "path", "line", "original_line", "side", "diff_hunk", "maintainer_can_modify"}
        state = {key: value[key] for key in fields if key in value}
        for key in ("head", "base"):
            if isinstance(value.get(key), dict):
                state[key] = {field: value[key][field] for field in ("ref", "sha") if field in value[key]}
        if isinstance(value.get("user"), dict):
            state["author"] = value["user"].get("login")
        for key, field in (("requested_reviewers", "login"), ("requested_teams", "slug"), ("assignees", "login"), ("labels", "name")):
            if isinstance(value.get(key), list):
                state[key] = [item.get(field) if isinstance(item, dict) else item for item in value[key]]
        return state

    async def snapshot(self, name, arguments):
        """Read current targets without invoking any mutating MCP method."""
        owner, repo = arguments["owner"], arguments["repo"]
        root = f"/repos/{owner}/{repo}"
        read = self.adapter.request_json
        if name == "create_pull_request":
            base, head = arguments["base"], arguments["head"]
            if ":" in head:
                comparison_path = f"{root}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
                comparison = await read(comparison_path, {"per_page": 1})
                total = comparison["total_commits"]
                last = await read(comparison_path, {"per_page": 1, "page": total}) if total > 1 else comparison
                head_sha = last["commits"][-1]["sha"] if last["commits"] else comparison["merge_base_commit"]["sha"]
                commits = {"base": comparison["base_commit"]["sha"], "head": head_sha}
            else:
                commits = {key: (await read(f"{root}/commits/{quote(ref, safe='')}"))["sha"]
                           for key, ref in (("base", base), ("head", head))}
            existing = await read(root + "/pulls", {
                "state": "open", "base": base, "head": head if ":" in head else f"{owner}:{head}", "per_page": 100})
            return {"commits": commits, "open_pull_requests": self.target_state(existing)}
        if name == "issue_write" and arguments["method"] == "create":
            parent = arguments.get("parent_issue_number")
            if parent is None:
                return {"operation": "create_issue"}
            parent_root = f"/repos/{arguments.get('parent_owner', owner)}/{arguments.get('parent_repo', repo)}"
            return self.target_state(await read(f"{parent_root}/issues/{int(parent)}"))
        if name == "add_reply_to_pull_request_comment":
            comment = await read(f"{root}/pulls/comments/{int(arguments['commentId'])}")
            expected = f"https://api.github.com{root}/pulls/"
            target = comment.get("pull_request_url", "")
            if not target.startswith(expected) or not target[len(expected):].isdigit():
                raise ValueError("comment target mismatch")
            if arguments.get("pullNumber") is not None and int(target[len(expected):]) != arguments["pullNumber"]:
                raise ValueError("comment is on another PR")
            return self.target_state(comment)
        if name in {"issue_write", "add_issue_comment"}:
            return self.target_state(await read(f"{root}/issues/{int(arguments['issue_number'])}"))
        pull_number = int(arguments["pullNumber"])
        pull = self.target_state(await read(f"{root}/pulls/{pull_number}"))
        if name == "pull_request_review_write" and arguments["method"] in {"resolve_thread", "unresolve_thread"}:
            thread = await self.adapter.review_thread(arguments["threadId"])
            if (thread["pullRequest"]["number"] != pull_number or
                    thread["pullRequest"]["repository"]["nameWithOwner"].lower() != f"{owner}/{repo}".lower()):
                raise ValueError("thread is on another PR")
            return {"pull_request": pull, "thread": thread}
        if name in {"pull_request_review_write", "add_comment_to_pending_review"}:
            login = (await read("/user"))["login"]
            reviews = await self.read_pages(f"{root}/pulls/{pull_number}/reviews")
            pending = [review for review in reviews if review.get("state") == "PENDING"
                       and review.get("user", {}).get("login") == login]
            pending.sort(key=lambda review: review["id"])
            latest = pending[-1] if pending else None
            needs_pending = name == "add_comment_to_pending_review" or arguments.get("method") in {
                "submit_pending", "delete_pending", "submit", "delete"}
            if needs_pending and latest is None:
                raise ValueError("no pending review")
            state = self.target_state(latest) if latest is not None else None
            if latest is not None:
                comments = await self.read_pages(f"{root}/pulls/{pull_number}/reviews/{latest['id']}/comments")
                state["comments"] = self.target_state(comments)
            return {"pull_request": pull, "pending_review": state}
        return pull

    async def read_pages(self, path):
        values = []
        for page in range(1, 11):
            batch = await self.adapter.request_json(path, {"per_page": 100, "page": page})
            if not isinstance(batch, list):
                raise ValueError("invalid paginated result")
            values.extend(batch)
            if len(batch) < 100:
                return values
        raise ValueError("preview pagination limit")

    async def prepare(self, name, arguments):
        if not self.authorization.active or self.attempts >= 30:
            raise ToolPreparationError("github_operation_limit", "当前提问已结束或 GitHub 写操作预览已达上限。")
        key = request_key(name, arguments)
        self.prepared.pop(key, None)
        if key in self.uncertain:
            raise ToolPreparationError("github_outcome_unknown", "上次写请求结果不明，请先查询实际状态；本轮不自动重试该操作。")
        self.attempts += 1
        try:
            error = await self.adapter.validate_write(name, arguments, self.public_check)
            if error:
                raise ToolPreparationError(error["error"], "GitHub 仓库、方法、凭据或写操作范围检查未通过。")
            before = await self.snapshot(name, arguments)
            description = json.dumps({"操作": name, "将提交的完整参数": arguments, "当前状态": before}, ensure_ascii=False, indent=2)
            if len(description) > 24000:
                raise ToolPreparationError("github_preview_too_large", "完整预览超过 24000 字符，请缩小操作范围。")
            self.prepared[key] = (time.monotonic(), json.dumps(before, sort_keys=True, ensure_ascii=False))
            return description
        except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
            raise ToolPreparationError("github_preparation_failed", "无法核验 GitHub 当前状态，未申请执行。") from None

    async def apply(self, name, arguments):
        key = request_key(name, arguments)
        prepared = self.prepared.pop(key, None)
        if prepared is None or not self.authorization.active or time.monotonic() - prepared[0] > 600:
            return {"error": "github_not_prepared"}
        try:
            error = await self.adapter.validate_write(name, arguments, self.public_check)
            if error:
                return error
            current = await self.snapshot(name, arguments)
            if json.dumps(current, sort_keys=True, ensure_ascii=False) != prepared[1]:
                return {"error": "github_state_changed", "message": "等待确认期间目标状态已变化，请重新核对并申请。"}
        except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
            return {"error": "github_preparation_failed"}
        try:
            result = await self.connections["github"].call(name, arguments)
            normalized = normalize_result(result, "github", name, arguments, self.adapter.env_file)
            if normalized.get("error"):
                self.uncertain.add(key)
                return {"error": "github_outcome_unknown", "message": "未得到可核验的写操作结果，请先查询目标状态，不要自动重试。"}
            return normalized
        except (MCPUnavailable, OSError, ValueError, TypeError, TimeoutError, httpx.HTTPError):
            self.uncertain.add(key)
            return {"error": "github_outcome_unknown", "message": "GitHub 写请求结果不明，请先查询实际状态，不要自动重试。"}
        except asyncio.CancelledError:
            self.uncertain.add(key)
            raise
