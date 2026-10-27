"""Public repository read scope and API-provided source attribution."""
import json
import httpx
from dianstar.tools.mcp.schemas import METHODS


class GitHubAdapter:
    def __init__(self, settings):
        self.settings = settings

    async def validate_read(self, name, arguments, public_check):
        owner, repo = arguments.get("owner", ""), arguments.get("repo", "")
        if not isinstance(owner, str) or not isinstance(repo, str) or f"{owner}/{repo}".lower() not in self.settings.github_repositories:
            return {"error": "mcp_repository_denied"}
        if name in METHODS and arguments.get("method") not in METHODS[name]:
            return {"error": "mcp_tool_denied"}
        # Every call rechecks visibility; never silently use a token for private data.
        if not await public_check(owner, repo):
            return {"error": "mcp_public_repository_unverified", "message": "未确认仓库公开可读；未使用凭据读取内容。"}
        return None

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


def github_sources(texts, arguments, resource_urls=()):
    """Record API-provided object URLs, never links quoted inside bodies."""
    prefix = f"https://github.com/{arguments['owner']}/{arguments['repo']}".lower()
    urls = [url for url in resource_urls if url.lower() == prefix or url.lower().startswith(prefix + "/")][:20]
    def visit(value, depth=0):
        if depth > 6 or len(urls) >= 20:
            return
        if isinstance(value, dict):
            url = value.get("html_url")
            if isinstance(url, str) and (url.lower() == prefix or url.lower().startswith(prefix + "/")):
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
