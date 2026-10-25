"""搜索服务单独配置；只保存 Key ID，不保存密钥，不改变对话模型。"""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_FILE, ENV_FILE, api_key_configured, load_model_settings

SEARCH_CONFIG_FILE = CONFIG_FILE.with_name("search.json")


@dataclass(frozen=True)
class SearchSettings:
    provider: str = "auto"
    key_id: str = "current"
    paid_policy: str = "ask"


def _normalize(provider: str, key_id: str, paid_policy: str) -> SearchSettings:
    # 旧的显式付费模式已经获得使用付费服务的授权；免费模式迁移为默认询问。
    if provider == "deepseek":
        provider, paid_policy = "auto", "allow"
    elif provider == "free":
        provider = "auto"
    if (provider not in {"auto", "disabled"} or not isinstance(key_id, str)
            or paid_policy not in {"ask", "allow", "deny"}):
        raise ValueError
    return SearchSettings(provider, key_id, paid_policy)


def load_search_settings(path: Path = SEARCH_CONFIG_FILE) -> SearchSettings:
    if not path.exists():
        return SearchSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _normalize(data["provider"], data["key_id"], data.get("paid_policy", "ask"))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("网页权限配置无效，请检查 .local/search.json。") from error


def save_search_settings(provider: str, key_id: str, *, path: Path = SEARCH_CONFIG_FILE,
                         config_file: Path = CONFIG_FILE, env_file: Path = ENV_FILE,
                         paid_policy: str = "ask") -> SearchSettings:
    try:
        settings = _normalize(provider, key_id, paid_policy)
    except (TypeError, ValueError):
        raise ValueError("网页访问设置无效。") from None
    if settings.provider == "auto" and settings.paid_policy == "allow":
        models = load_model_settings(config_file)
        actual_id = models.default_key_id if key_id == "current" else key_id
        models.profile(actual_id)
        if not api_key_configured(env_file, key_id=actual_id):
            raise ValueError("API 备用 Key 尚未配置，请先设置 Key，或保留使用前询问。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".search-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump({"provider": settings.provider, "key_id": key_id,
                       "paid_policy": settings.paid_policy}, file, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return settings
