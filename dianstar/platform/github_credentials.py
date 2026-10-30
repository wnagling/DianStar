"""Git Credential Manager in an application-owned OS-vault namespace."""
import asyncio
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
from urllib.request import getproxies

from dianstar.security.processes import terminate_tree
from dianstar.config.paths import PROJECT_ROOT

LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
NAMESPACE = re.compile(r"dianstar-[a-f0-9]{32}\Z")


class GitHubLoginError(ValueError):
    pass


def credential_environment(*, interactive=False):
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "USERPROFILE", "HOME",
               "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY",
               "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "SSL_CERT_FILE"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    for scheme, value in getproxies().items():
        if scheme in {"http", "https", "all", "no"}:
            env[scheme.upper() + "_PROXY"] = value
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GCM_PROVIDER="github", GCM_INTERACTIVE="1" if interactive else "0",
               GCM_TRACE="0", GCM_TRACE_SECRETS="0", GCM_DEBUG="0",
               GCM_CREDENTIAL_STORE="wincredman" if os.name == "nt" else "keychain" if sys.platform == "darwin" else "secretservice")
    return env


def discover_manager():
    """Use installed absolute paths; never search the current project implicitly."""
    directories = [Path(value) for value in os.environ.get("PATH", "").split(os.pathsep)
                   if Path(value).is_absolute() and Path(value).resolve() != Path.cwd().resolve()]
    suffix = ".exe" if os.name == "nt" else ""
    for directory in directories:
        candidate = directory / ("git-credential-manager" + suffix)
        if candidate.is_file():
            return str(candidate)
    for directory in directories:
        git = shutil.which(str(directory / "git"))
        if not git:
            continue
        try:
            result = subprocess.run([git, "--exec-path"], capture_output=True, text=True,
                env=credential_environment(), timeout=5, cwd=PROJECT_ROOT,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            location = Path(result.stdout.strip())
            if result.returncode or not location.is_absolute():
                continue
            for base in (location, location.parent.parent / "bin", Path(git).parent):
                candidate = base / ("git-credential-manager" + suffix)
                if candidate.is_file():
                    return str(candidate)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ""


def credential_input(login="", token=None):
    if login and not LOGIN.fullmatch(login):
        raise GitHubLoginError("GitHub 账号格式无效。")
    if token is not None and (not 8 <= len(token) <= 8192 or not token.isascii() or any(c.isspace() for c in token)):
        raise GitHubLoginError("访问令牌格式无效，请重新输入。")
    fields = [("protocol", "https"), ("host", "github.com")]
    if login:
        fields.append(("username", login))
    if token is not None:
        fields.append(("password", token))
    return "".join(f"{key}={value}\n" for key, value in fields) + "\n"


class CredentialManager:
    def __init__(self, executable=None):
        self.executable = executable

    def _executable(self):
        self.executable = self.executable or discover_manager()
        if not self.executable:
            raise GitHubLoginError("未找到 GitHub 登录组件，请修复包含登录组件的 Git 安装。")
        return self.executable

    async def call(self, namespace, args, *, data="", interactive=False, timeout=15):
        if not NAMESPACE.fullmatch(namespace):
            raise GitHubLoginError("登录信息无效，请重新登录。")
        env = credential_environment(interactive=interactive)
        env["GCM_NAMESPACE"] = namespace
        try:
            process = await asyncio.create_subprocess_exec(self._executable(), *args,
                env=env, cwd=PROJECT_ROOT, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}))
            try:
                output, _ = await asyncio.wait_for(process.communicate(data.encode()), timeout)
            except (asyncio.CancelledError, TimeoutError):
                if interactive:
                    # The browser belongs to the user; cancel the login helper only.
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                else:
                    handle = SimpleNamespace(pid=process.pid, poll=lambda: process.returncode, kill=process.kill)
                    await asyncio.to_thread(terminate_tree, handle)
                await process.wait()
                raise
            if process.returncode or len(output) > 65536:
                raise GitHubLoginError("登录未完成或系统凭据库不可用，请重试。")
            return output.decode("utf-8", errors="strict")
        except (OSError, UnicodeError, TimeoutError):
            raise GitHubLoginError("登录组件未能完成操作，请检查网络或重试。") from None

    async def browser_login(self, namespace):
        await self.call(namespace, ["github", "login", "--browser", "--url", "https://github.com", "--force"], interactive=True, timeout=180)

    async def read(self, namespace, login=""):
        output = await self.call(namespace, ["get", "--no-ui"], data=credential_input(login))
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        account, token = fields.get("username", ""), fields.get("password", "")
        credential_input(account, token)
        if not account or (login and account.lower() != login.lower()):
            raise GitHubLoginError("登录账号无法确认，请重新登录。")
        return account, token

    async def store(self, namespace, login, token):
        await self.call(namespace, ["store"], data=credential_input(login, token))

    async def erase(self, namespace, login):
        if not LOGIN.fullmatch(login):
            raise GitHubLoginError("登录账号无效。")
        await self.call(namespace, ["github", "logout", login, "--url", "https://github.com", "--no-ui"])


def read_stored_token(namespace, login):
    """Synchronous SDK-side lookup; stdout is never exposed to tools or logs."""
    if not NAMESPACE.fullmatch(namespace) or not LOGIN.fullmatch(login):
        return None
    executable = discover_manager()
    if not executable:
        return None
    env = credential_environment()
    env["GCM_NAMESPACE"] = namespace
    try:
        result = subprocess.run([executable, "get", "--no-ui"], input=credential_input(login),
            capture_output=True, text=True, env=env, cwd=PROJECT_ROOT, timeout=15,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode == 0 and values.get("username", "").lower() == login.lower():
            token = values.get("password", "")
            credential_input(login, token)
            return token
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None
