"""User-driven GitHub login; never a model tool or conversation message."""
import asyncio
from dataclasses import dataclass, replace
import uuid

import httpx

from dianstar.config.github_account import cached_token, load_account, save_account, MAX_PENDING_CLEANUP
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
        self._operation_lock = asyncio.Lock()

    @staticmethod
    def _pending(profile):
        pending = [dict(entry) for entry in profile.get("pending_cleanup", [])]
        # Preserve cleanup records produced by the older logout implementation.
        if profile["mode"] == "signed_out" and "namespace" in profile:
            if not any(entry["namespace"] == profile["namespace"] for entry in pending):
                pending.append({key: profile[key] for key in ("login", "namespace")})
        return pending

    @staticmethod
    def _with_pending(profile, pending):
        result = {key: value for key, value in profile.items() if key != "pending_cleanup"}
        if result["mode"] == "signed_out" and not any(entry["namespace"] == result.get("namespace") for entry in pending):
            result = {"mode": "signed_out"}
        if pending:
            result["pending_cleanup"] = pending
        return result

    def _cleanup_notice(self, status):
        try:
            pending = self._pending(load_account(self.root))
        except (ValueError, OSError):
            return replace(status, message=status.message + " 清理记录暂时无法读取，请检查账号状态。")
        if pending:
            return replace(status, message=status.message + " 另有登录记录尚未清理；可再次检查账号状态重试。")
        return status

    async def _retry_cleanup(self):
        profile = load_account(self.root)
        pending = self._pending(profile)
        if not pending:
            return
        # Journal before contacting the vault, including legacy pending logout.
        try:
            save_account(self.root, self._with_pending(profile, pending))
        except (ValueError, OSError):
            return
        for entry in pending:
            current = load_account(self.root)
            if current["mode"] == "managed" and current["namespace"] == entry["namespace"]:
                continue
            try:
                await self.manager.erase(entry["namespace"], entry["login"])
            except (GitHubLoginError, OSError):
                # Retain this and later entries; a failed vault must not cause
                # repeated helper timeouts for every recorded account.
                return
            current = load_account(self.root)
            remaining = [item for item in self._pending(current) if item["namespace"] != entry["namespace"]]
            try:
                save_account(self.root, self._with_pending(current, remaining))
            except (ValueError, OSError):
                return

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
        async with self._operation_lock:
            try:
                await self._retry_cleanup()
                return self._cleanup_notice(await self._check())
            except (ValueError, OSError):
                return AccountStatus("unavailable", message="账号或待清理记录无法读取，请稍后重试。")

    async def _check(self):
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
        except (GitHubLoginError, OSError):
            return
        profile = load_account(self.root)
        if profile["mode"] == "managed" and profile["namespace"] == namespace:
            return
        pending = self._pending(profile)
        if not any(entry["namespace"] == namespace for entry in pending):
            pending.append({"login": login, "namespace": namespace})
        save_account(self.root, self._with_pending(profile, pending))
        await self._retry_cleanup()

    async def login(self, token=None):
        async with self._operation_lock:
            return await self._login(token)

    async def _login(self, token=None):
        try:
            previous = load_account(self.root)
        except ValueError:
            previous = {"mode": "signed_out"}
        if len(self._pending(previous)) >= MAX_PENDING_CLEANUP - 1:
            raise GitHubLoginError("待清理的登录记录过多，请先检查账号状态重试清理。")
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
            pending = self._pending(previous)
            if previous["mode"] == "managed":
                pending.append({key: previous[key] for key in ("login", "namespace")})
            # Activate the new account and retain the old cleanup reference in
            # the same atomic metadata replacement. No token enters this file.
            save_account(self.root, self._with_pending({"mode": "managed", "login": login, "namespace": namespace}, pending))
            activated = True
            await self._retry_cleanup()
            return self._cleanup_notice(AccountStatus("connected", login, "登录成功，后续启动无需重复登录。"))
        finally:
            if not activated and touched:
                await self._cleanup(namespace, login)

    async def logout(self):
        async with self._operation_lock:
            return await self._logout()

    async def _logout(self):
        try:
            previous = load_account(self.root)
        except ValueError:
            previous = {"mode": "signed_out"}
        pending = self._pending(previous)
        if previous["mode"] == "managed":
            pending.append({key: previous[key] for key in ("login", "namespace")})
        state = {"mode": "signed_out", **({key: previous[key] for key in ("login", "namespace")} if "namespace" in previous else {})}
        # Disabling the account and recording cleanup are one atomic update.
        save_account(self.root, self._with_pending(state, pending))
        await self._retry_cleanup()
        return self._cleanup_notice(AccountStatus("signed_out", message="已退出 DianStar 的 GitHub 登录，其他 Git 工具的登录不受影响。"))
