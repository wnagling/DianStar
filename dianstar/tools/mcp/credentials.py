"""Resolve GitHub credentials without returning them to model tools or logs."""
from functools import lru_cache
import os
import subprocess
from dotenv import dotenv_values
from dianstar.config.github_account import load_account, cached_token


@lru_cache(maxsize=1)
def git_credential():
    try:
        result = subprocess.run(["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True, timeout=15,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        if result.returncode == 0:
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            return values.get("password")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def github_token(settings, env_file):
    try:
        account = load_account(env_file.parent)
    except (ValueError, OSError):
        return None
    if account["mode"] == "signed_out":
        return None
    if account["mode"] == "managed":
        return cached_token(account["namespace"], account["login"])
    values = dotenv_values(env_file) if env_file.is_file() else {}
    token = values.get("GITHUB_PERSONAL_ACCESS_TOKEN") or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    return token or (git_credential() if settings.github_use_git_credentials else None)
