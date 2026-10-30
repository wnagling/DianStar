"""User-driven GitHub login; never a model tool or conversation message."""
import asyncio
from dataclasses import dataclass
import uuid

import httpx

from dianstar.config.github_account import cached_token, load_account, save_account
from dianstar.platform.github_credentials import CredentialManager, GitHubLoginError, LOGIN, credential_input


@dataclass(frozen=True)
class AccountStatus:
    state: str
    login: str = ""
    message: str = ""


class GitHubAccount:
    def __init__(self, env_file, *, manager=None, transport=None):
        self.env_file, self.root = env_file, env_file.parent
        self.manager = manager or CredentialManager()
        self.transport = transport

    async def _verify(self, token):
        credential_input(token=token)
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=False, transport=self.transport) as client:
                response = await client.get("https://api.github.com/user", headers={
                    "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
            if response.status_code == 401:
                raise GitHubLoginError("登录凭据已失效，请重新登录。")
            if response.status_code != 200 or len(response.content) > 65536:
                raise GitHubLoginError("暂时无法验证 GitHub 账号，请检查网络或账号权限。")
            payload = response.json()
            if not isinstance(payload, dict):
                raise GitHubLoginError("GitHub 未返回可确认的账号，请重试。")
            login = payload.get("login", "")
            if not isinstance(login, str) or not LOGIN.fullmatch(login) or type(payload.get("id")) is not int or payload["id"] <= 0:
                raise GitHubLoginError("GitHub 未返回可确认的账号，请重试。")
            return login
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, GitHubLoginError):
                raise
            raise GitHubLoginError("无法连接 GitHub，请检查网络后重试。") from None

    async def check(self):
        try:
            profile = load_account(self.root)
            if profile["mode"] == "signed_out":
                return AccountStatus("signed_out", message="尚未登录 GitHub。")
            cached_token.cache_clear()
            if profile["mode"] == "managed":
                token = await asyncio.to_thread(cached_token, profile["namespace"], profile["login"])
            else:
                from dianstar.config.mcp import load_mcp_settings
                from dianstar.tools.mcp.credentials import github_token
                token = await asyncio.to_thread(github_token, load_mcp_settings(self.root / ".local/mcp.json"), self.env_file)
            if not token:
                if profile["mode"] == "managed":
                    return AccountStatus("unavailable", profile["login"], "无法读取已保存的登录，请重新登录或检查系统凭据库。")
                return AccountStatus("signed_out", message="尚未登录 GitHub。")
            login = await self._verify(token)
            if profile["mode"] == "managed" and login.lower() != profile["login"].lower():
                raise GitHubLoginError("保存的账号与 GitHub 返回的身份不一致，请重新登录。")
            return AccountStatus("connected", login, "账号验证成功，下次提问会自动使用此登录。")
        except (ValueError, OSError) as error:
            return AccountStatus("unavailable", message=str(error) if isinstance(error, GitHubLoginError) else "账号状态无法读取，请重新登录。")

    async def _cleanup(self, namespace, login=""):
        try:
            if not login:
                login, _ = await self.manager.read(namespace)
            await self.manager.erase(namespace, login)
        except (GitHubLoginError, OSError):
            pass

    async def login(self, token=None):
        namespace = "dianstar-" + uuid.uuid4().hex
        activated, touched, login = False, False, ""
        try:
            if token is None:
                touched = True
                await self.manager.browser_login(namespace)
                login, value = await self.manager.read(namespace)
                verified = await self._verify(value)
                if login.lower() != verified.lower():
                    raise GitHubLoginError("登录账号无法确认，请重试。")
                login = verified
            else:
                credential_input(token=token)
                login = await self._verify(token)
                touched = True
                await self.manager.store(namespace, login, token)
                stored_login, stored = await self.manager.read(namespace, login)
                if stored != token or stored_login.lower() != login.lower():
                    raise GitHubLoginError("系统未能安全保存登录，请重试。")
            try:
                previous = load_account(self.root)
            except ValueError:
                previous = {"mode": "signed_out"}
            save_account(self.root, {"mode": "managed", "login": login, "namespace": namespace})
            activated = True
            if previous["mode"] == "managed":
                await self._cleanup(previous["namespace"], previous["login"])
            return AccountStatus("connected", login, "登录成功，后续启动无需重复登录。")
        finally:
            if not activated and touched:
                await self._cleanup(namespace, login)

    async def logout(self):
        try:
            previous = load_account(self.root)
        except ValueError:
            previous = {"mode": "signed_out"}
        pending = {"mode": "signed_out", **({key: previous[key] for key in ("login", "namespace")} if "namespace" in previous else {})}
        save_account(self.root, pending)
        if "namespace" in previous:
            try:
                await self.manager.erase(previous["namespace"], previous["login"])
            except (GitHubLoginError, OSError):
                return AccountStatus("signed_out", message="已退出此应用；系统保存的登录未能清理，可稍后重试。")
        save_account(self.root, {"mode": "signed_out"})
        return AccountStatus("signed_out", message="已退出 DianStar 的 GitHub 登录，其他 Git 工具的登录不受影响。")
