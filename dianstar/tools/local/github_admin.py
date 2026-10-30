"""GitHub repository administration via documented REST endpoints.

These are local tools, separate from the official MCP tool namespace.
"""
import json
import time
from urllib.parse import quote
import httpx

from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.security.authorization import request_key
from dianstar.security.gateway import ToolPreparationError
from dianstar.tools.mcp.github_adapter import GitHubAdapter


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}

BOOL = {"type": "boolean"}
STR = {"type": "string"}
NAMES = {"type": "array", "items": STR, "maxItems": 100}
ACTORS = obj({"users": NAMES, "teams": NAMES, "apps": NAMES})
SETTINGS = obj({**{key: BOOL for key in ("has_issues", "has_projects", "has_wiki", "has_discussions", "allow_squash_merge",
    "allow_merge_commit", "allow_rebase_merge", "allow_auto_merge", "delete_branch_on_merge", "allow_update_branch", "archived")},
    "description": STR, "homepage": STR, "default_branch": {"type": "string", "minLength": 1},
    "visibility": {"type": "string", "enum": ["public", "private", "internal"]}})
NULL = {"type": "null"}
PROTECTION = obj({
    "required_status_checks": {"anyOf": [NULL, obj({"strict": BOOL, "contexts": NAMES,
        "checks": {"type": "array", "maxItems": 100, "items": obj({"context": STR, "app_id": {"type": "integer"}}, ["context"])}}, ["strict"])]},
    "enforce_admins": BOOL,
    "required_pull_request_reviews": {"anyOf": [NULL, obj({"dismiss_stale_reviews": BOOL, "require_code_owner_reviews": BOOL,
        "required_approving_review_count": {"type": "integer", "minimum": 0, "maximum": 6}, "require_last_push_approval": BOOL,
        "dismissal_restrictions": ACTORS, "bypass_pull_request_allowances": ACTORS})]},
    "restrictions": {"anyOf": [NULL, ACTORS]},
    **{key: BOOL for key in ("required_linear_history", "allow_force_pushes", "allow_deletions", "block_creations",
        "required_conversation_resolution", "lock_branch", "allow_fork_syncing")}})


class GitHubAdmin:
    def __init__(self, settings, env_file, authorization):
        self.settings = settings
        self.adapter = GitHubAdapter(settings, env_file)
        self.authorization = authorization
        self.prepared, self.uncertain = {}, set()

    async def request(self, method, path, body=None, missing=False):
        token = self.adapter.token()
        if not token:
            raise ValueError("GitHub credential missing")
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=True) as client:
            response = await client.request(method, "https://api.github.com" + path,
                headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, json=body)
        if missing and response.status_code == 404:
            return None
        if response.status_code not in {200, 201, 202, 204} or len(response.content) > 1048576:
            raise ValueError("GitHub administration request failed")
        return response.json() if response.content else {}

    async def scope(self, args):
        repository = next(iter(self.adapter.repositories("get_file_contents", args)))
        if repository not in self.settings.github_admin_repositories:
            raise ValueError("Repository administration not enabled")
        owner, repo = repository.split("/")
        error = await self.adapter.validate_read("get_file_contents", args, self.adapter._public_repository)
        if error:
            raise ValueError("Repository access check failed")
        metadata = await self.request("GET", "/repos/" + repository)
        if str(metadata.get("full_name", "")).lower() != repository or not metadata.get("permissions", {}).get("admin"):
            raise ValueError("Repository administration permission missing")
        return "/repos/" + owner + "/" + repo

    @staticmethod
    def protection(value):
        if value is None:
            return {"required_status_checks": None, "enforce_admins": False, "required_pull_request_reviews": None, "restrictions": None}
        def actors(data):
            return {key: [x["login" if key == "users" else "slug"] if isinstance(x, dict) else x for x in data.get(key, [])]
                    for key in ("users", "teams", "apps")}
        result = {}
        for key in PROTECTION["properties"]:
            item = value.get(key)
            if key == "required_status_checks":
                result[key] = {k: item[k] for k in ("strict", "contexts", "checks") if k in item} if item else None
            elif key == "required_pull_request_reviews":
                result[key] = None if item is None else {k: actors(v) if k in {"dismissal_restrictions", "bypass_pull_request_allowances"} else v
                    for k, v in item.items() if k in PROTECTION["properties"][key]["anyOf"][1]["properties"]}
            elif key == "restrictions":
                result[key] = actors(item) if item else None
            else:
                result[key] = bool(item.get("enabled")) if isinstance(item, dict) else bool(item)
        return result

    async def pages(self, path):
        values = []
        for page in range(1, 11):
            batch = await self.request("GET", path + f"?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("Invalid paginated result")
            values.extend(batch)
            if len(batch) < 100:
                return values
        raise ValueError("Administration preview too large")

    async def snapshot(self, name, args, root):
        if "repository_settings" in name:
            data = await self.request("GET", root)
            return {key: data.get(key) for key in SETTINGS["properties"]}
        if "branch_protection" in name:
            branch = quote(args["branch"], safe="")
            await self.request("GET", root + "/branches/" + branch)
            data = await self.request("GET", root + "/branches/" + branch + "/protection", missing=True)
            return {"protected": data is not None, "settings": self.protection(data)}
        if name.endswith("list"):
            return {"collaborators": await self.pages(root + "/collaborators"), "invitations": await self.pages(root + "/invitations")}
        username = quote(args["username"], safe="")
        return {"permission": await self.request("GET", root + "/collaborators/" + username + "/permission", missing=True),
            "invitations": [x for x in await self.pages(root + "/invitations") if x.get("invitee", {}).get("login", "").lower() == args["username"].lower()]}

    async def preview(self, name, args):
        key = request_key(name, args)
        self.prepared.pop(key, None)
        if key in self.uncertain:
            raise ToolPreparationError("github_admin_outcome_unknown", "上次管理操作结果不明；请先回读，本轮不自动重试。")
        try:
            root = await self.scope(args)
            before = await self.snapshot(name, args, root)
            changes = args.get("changes")
            if changes is not None and not changes:
                raise ValueError("No changes")
            body = changes
            if "branch_protection" in name:
                body = {**before["settings"], **changes}
            description = json.dumps({"操作": name, "完整参数": args, "当前状态": before, "将提交的设置": body}, ensure_ascii=False, indent=2)
            if len(description) > 24000:
                raise ValueError("Preview too large")
            self.prepared[key] = (time.monotonic(), before, body)
            return description
        except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError):
            raise ToolPreparationError("github_admin_preparation_failed", "无法核对仓库管理范围、权限或完整当前设置，未执行。") from None

    async def apply(self, name, action):
        args = action.arguments
        key = request_key(name, args)
        item = self.prepared.pop(key, None)
        if not item or time.monotonic() - item[0] > 600 or not self.authorization.active:
            return Observation(action, {"error": "github_admin_not_prepared"})
        try:
            root = await self.scope(args)
            if await self.snapshot(name, args, root) != item[1]:
                return Observation(action, {"error": "github_admin_state_changed"})
        except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError):
            return Observation(action, {"error": "github_admin_preparation_failed"})
        try:
            if "repository_settings" in name:
                result = await self.request("PATCH", root, item[2])
            elif "branch_protection" in name:
                result = await self.request("PUT", root + "/branches/" + quote(args["branch"], safe="") + "/protection", item[2])
            else:
                path = root + "/collaborators/" + quote(args["username"], safe="")
                if name.endswith("remove"):
                    for invitation in item[1]["invitations"]:
                        await self.request("DELETE", root + "/invitations/" + str(int(invitation["id"])))
                    result = await self.request("DELETE", path) if item[1]["permission"] is not None else {}
                else:
                    result = await self.request("PUT", path, {"permission": args["permission"]})
            after = await self.snapshot(name, args, root)
            if "repository_settings" in name and any(after.get(k) != v for k, v in item[2].items()):
                raise ValueError("Settings read-back mismatch")
            if "branch_protection" in name and any(after["settings"].get(k) != v for k, v in item[2].items()):
                raise ValueError("Protection read-back mismatch")
            if name == "github_collaborators_remove" and (after["permission"] is not None or after["invitations"]):
                raise ValueError("Collaborator removal read-back mismatch")
            if name == "github_collaborators_set":
                requested = {"pull": "read", "push": "write"}.get(args["permission"], args["permission"])
                permission = after.get("permission") or {}
                accepted = {permission.get("permission"), permission.get("role_name")}
                invited_roles = {x.get("permissions") for x in after["invitations"]}
                if requested not in accepted | invited_roles and args["permission"] not in accepted | invited_roles:
                    raise ValueError("Collaborator permission read-back mismatch")
            content = {"success": True, "actual_state": after, "invitation": bool(result.get("invitee")) if isinstance(result, dict) else False}
            if "repository_settings" in name and "visibility" in item[2] and item[2]["visibility"] != "public":
                content["message"] = "仓库已改为非公开；后续内容访问仍需单独启用私有访问范围。"
            return Observation(action, content)
        except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError):
            self.uncertain.add(key)
            return Observation(action, {"error": "github_admin_outcome_unknown", "message": "管理请求结果未核验，请先回读实际状态，不要自动重复。"})

    def registrations(self):
        if not self.settings.github_enabled or not self.settings.github_admin_repositories:
            return {}
        common = {"owner": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"}, "repo": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"}}
        definitions = {
            "github_repository_settings_get": ({}, False),
            "github_repository_settings_update": ({"changes": SETTINGS}, True),
            "github_branch_protection_get": ({"branch": {"type": "string", "minLength": 1}}, False),
            "github_branch_protection_update": ({"branch": {"type": "string", "minLength": 1}, "changes": PROTECTION}, True),
            "github_collaborators_list": ({}, False),
            "github_collaborators_set": ({"username": {"type": "string", "pattern": "^[A-Za-z0-9-]+$"},
                "permission": {"type": "string", "enum": ["pull", "triage", "push", "maintain", "admin"]}}, True),
            "github_collaborators_remove": ({"username": {"type": "string", "pattern": "^[A-Za-z0-9-]+$"}}, True),
        }
        registry = {}
        for name, (fields, write) in definitions.items():
            async def read(action, name=name):
                try:
                    root = await self.scope(action.arguments)
                    result = await self.snapshot(name, action.arguments, root)
                    return Observation(action, {"content": json.dumps(result, ensure_ascii=False)[:24000], "untrusted": True})
                except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError):
                    return Observation(action, {"error": "github_admin_read_failed"})
            async def prepare(args, name=name):
                return await self.preview(name, args)
            async def apply(action, name=name):
                return await self.apply(name, action)
            tool = {"type": "function", "function": {"name": name, "description":
                "管理 GitHub 仓库设置、分支保护或协作者权限；须预览确认，结果不明先回读。" if write else "读取已启用管理权限的 GitHub 仓库设置。",
                "parameters": obj({**common, **fields}, [*common, *fields])}}
            registry[name] = ToolRegistration(tool, "github.admin.write" if write else "github.admin.read", apply if write else read, prepare if write else None)
        self.authorization.grant("github.admin.read", uses=30)
        return registry
