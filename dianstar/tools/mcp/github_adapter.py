"""Repository access policy and API-provided source attribution."""
import json
from pathlib import Path
import httpx
from dotenv import dotenv_values
import os
from dianstar.tools.mcp.github_contract import METHODS, SEARCH_TOOLS, WRITE_TOOLS
from dianstar.tools.mcp.github_search import search_repositories


class GitHubAdapter:
    def __init__(self, settings, env_file=Path("missing.env")):
        self.settings, self.env_file = settings, env_file

    def token(self):
        values = dotenv_values(self.env_file) if self.env_file.is_file() else {}
        return values.get("GITHUB_PERSONAL_ACCESS_TOKEN") or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")

    def repositories(self, name, arguments):
        allowed = set(self.settings.github_repositories)
        if name in SEARCH_TOOLS:
            return search_repositories(arguments, allowed)
        owner, repo = arguments.get("owner"), arguments.get("repo")
        if not isinstance(owner, str) or not isinstance(repo, str) or f"{owner}/{repo}".lower() not in allowed:
            raise ValueError("repository not allowed")
        return {f"{owner}/{repo}".lower()}

    async def validate_read(self, name, arguments, public_check):
        if name in METHODS and arguments.get("method") not in METHODS[name]:
            return {"error": "mcp_tool_denied"}
        try:
            repositories = self.repositories(name, arguments)
        except ValueError:
            return {"error": "mcp_repository_denied", "message": "仓库未获允许，或搜索的每个 OR 分支未限定允许的 repo:owner/repo。"}
        for repository in sorted(repositories):
            owner, repo = repository.split("/")
            if repository in self.settings.github_private_repositories:
                if not self.token():
                    return {"error": "github_token_missing"}
                data = await self.request_json(f"/repos/{owner}/{repo}")
                if str(data.get("full_name", "")).lower() != repository:
                    return {"error": "mcp_repository_denied"}
            elif not await public_check(owner, repo):
                return {"error": "mcp_public_repository_unverified", "message": "未确认仓库公开可读；未使用凭据读取内容。"}
        return None

    async def validate_write(self, name, arguments, public_check):
        if name not in WRITE_TOOLS:
            return {"error": "mcp_tool_denied"}
        try:
            targets = self.repositories(name, arguments)
            if ("parent_owner" in arguments) != ("parent_repo" in arguments):
                return {"error": "invalid_tool_arguments"}
            if arguments.get("parent_issue_number") is not None:
                parent = {"owner": arguments.get("parent_owner", arguments.get("owner")),
                          "repo": arguments.get("parent_repo", arguments.get("repo"))}
                targets |= self.repositories(name, parent)
        except ValueError:
            return {"error": "mcp_repository_denied"}
        if not targets <= set(self.settings.github_write_repositories):
            return {"error": "github_write_disabled"}
        if not self.token():
            return {"error": "github_token_missing"}
        for target in sorted(targets):
            owner, repo = target.split("/")
            error = await self.validate_read(name, {**arguments, "owner": owner, "repo": repo}, public_check)
            if error:
                return error
        return None

    async def request_json(self, path, params=None):
        """Fixed-host metadata/preflight reads; never follow server-provided URLs."""
        headers = {"Accept": "application/vnd.github+json"}
        if self.token():
            headers["Authorization"] = "Bearer " + self.token()
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=True) as client:
            response = await client.get("https://api.github.com" + path, headers=headers, params=params)
            if response.status_code != 200 or len(response.content) > 1048576:
                raise ValueError("GitHub preflight failed")
            return response.json()

    async def review_thread(self, thread_id):
        query = """query($id: ID!) { node(id: $id) { ... on PullRequestReviewThread {
          id isResolved pullRequest { number repository { nameWithOwner } }
        } } }"""
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=True) as client:
            response = await client.post("https://api.github.com/graphql",
                headers={"Authorization": "Bearer " + self.token()},
                json={"query": query, "variables": {"id": thread_id}})
            if response.status_code != 200 or len(response.content) > 65536:
                raise ValueError("thread preflight failed")
            data = response.json()
            if data.get("errors"):
                raise ValueError("thread preflight failed")
            return data["data"]["node"]

    async def _public_repository(self, owner, repo):
        # Unauthenticated check: a token must not silently expose private content.
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=True) as client:
            response = await client.get(f"https://api.github.com/repos/{owner}/{repo}",
                headers={"Accept": "application/vnd.github+json"})
            if response.status_code != 200 or len(response.content) > 65536:
                return False
            data = response.json()
            return (data.get("private") is False and data.get("visibility") == "public"
                    and str(data.get("full_name", "")).lower() == f"{owner}/{repo}".lower())


def github_sources(texts, arguments, resource_urls=(), repositories=()):
    """Record API-provided object URLs, never links quoted inside bodies."""
    if arguments.get("owner") and arguments.get("repo"):
        repositories = [*repositories, f"{arguments['owner']}/{arguments['repo']}"]
    prefixes = [f"https://github.com/{repository}".lower() for repository in repositories]
    def in_scope(url):
        return isinstance(url, str) and any(url.lower() == prefix or url.lower().startswith(prefix + "/") for prefix in prefixes)
    urls = [url for url in resource_urls if in_scope(url)][:20]
    def visit(value, depth=0):
        if depth > 6 or len(urls) >= 20:
            return
        if isinstance(value, dict):
            url = value.get("html_url")
            if in_scope(url):
                urls.append(url)
            for key, item in list(value.items())[:100]:
                if key not in {"body", "content", "text", "description"}:
                    visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:100]:
                visit(item, depth + 1)
    for text in texts:
        try:
            visit(json.loads(text))
        except (ValueError, RecursionError):
            pass
    return list(dict.fromkeys(urls))
