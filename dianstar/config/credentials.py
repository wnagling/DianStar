"""Local credential storage; values never enter model settings or history."""
import re
from pathlib import Path
from dotenv import dotenv_values, set_key
from .paths import ENV_FILE


_KEY_ID = re.compile(r"(?:primary|key(?:[2-9]|[1-9][0-9]+))\Z")


def _env_name(key_id: str) -> str:
    if not _KEY_ID.fullmatch(key_id):
        raise ValueError("Key ID 无效。")
    return "DEEPSEEK_API_KEY" if key_id == "primary" else f"DEEPSEEK_API_KEY_{key_id.upper()}"


def read_api_key(env_file: Path = ENV_FILE, *, key_id: str = "primary") -> str:
    """Read a credential for the SDK or the masked editor; keep it out of chat/config JSON."""
    return (dotenv_values(env_file).get(_env_name(key_id)) or "").strip()


def api_key_configured(env_file: Path = ENV_FILE, *, key_id: str = "primary") -> bool:
    value = read_api_key(env_file, key_id=key_id)
    return bool(value and value != "your-key-here")


def save_api_key(value: str, env_file: Path = ENV_FILE, *, key_id: str = "primary") -> None:
    key = value.strip()
    if not key or any(char.isspace() for char in key):
        raise ValueError("API Key 不能为空，也不能包含空白字符。")
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.touch(exist_ok=True)
    if not set_key(str(env_file), _env_name(key_id), key, quote_mode="always"):
        raise OSError("无法保存 API Key。")
