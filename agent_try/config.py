"""保存多 Key 配置；密钥只写入本地 .env，不进入模型配置文件。"""

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key


# In a frozen portable build, settings live beside the executable, outside _internal.
PROJECT_ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent.parent)
CONFIG_FILE = PROJECT_ROOT / ".local" / "config.json"
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_MODEL = "deepseek-flash"
PRO_MODEL = "deepseek-v4-pro"
DEFAULT_SWITCH_MODEL_SHORTCUT = "ctrl+b"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}\Z")
_KEY_ID = re.compile(r"(?:primary|key(?:[2-9]|[1-9][0-9]+))\Z")
_SHORTCUT = re.compile(r"(?:(?:ctrl|alt)(?:\+shift)?\+[a-z0-9]|ctrl\+alt\+[a-z0-9]|f(?:[1-9]|1[0-2]))\Z")
_RESERVED_SHORTCUTS = {"ctrl+l", "ctrl+q", "ctrl+s", "ctrl+c", "ctrl+v", "alt+f4"}


@dataclass(frozen=True)
class KeyProfile:
    id: str = "primary"
    name: str = "默认 Key"
    models: tuple[str, ...] = (DEFAULT_MODEL, PRO_MODEL)
    default_model: str = DEFAULT_MODEL


@dataclass(frozen=True)
class ModelSettings:
    profiles: tuple[KeyProfile, ...] = (KeyProfile(),)
    default_key_id: str = "primary"
    switch_model_shortcut: str = DEFAULT_SWITCH_MODEL_SHORTCUT

    def profile(self, key_id: str) -> KeyProfile:
        for profile in self.profiles:
            if profile.id == key_id:
                return profile
        raise ValueError("没有找到该 Key 配置。")

    @property
    def active_profile(self) -> KeyProfile:
        return self.profile(self.default_key_id)

    @property
    def models(self) -> tuple[str, ...]:
        return self.active_profile.models

    @property
    def default_model(self) -> str:
        return self.active_profile.default_model


def validate_shortcut(shortcut: str) -> str:
    """Accept terminal combinations without overriding chat and app controls."""
    normalized = re.sub(r"\s*\+\s*", "+", shortcut.strip().lower()) if isinstance(shortcut, str) else ""
    if not _SHORTCUT.fullmatch(normalized) or normalized in _RESERVED_SHORTCUTS:
        raise ValueError("请按 Ctrl/Alt+字母或数字，或 F1–F12；该按键也不能与现有操作冲突。")
    return normalized


def _validate_models(models: tuple[str, ...], default_model: str) -> None:
    if not isinstance(models, tuple) or any(not isinstance(model, str) for model in models):
        raise ValueError("模型列表格式无效。")
    if not models:
        if default_model:
            raise ValueError("空模型列表不能指定默认模型。")
        return
    if len(set(models)) != len(models):
        raise ValueError("模型列表不能包含重复项。")
    if any(not _MODEL_ID.fullmatch(model) for model in models):
        raise ValueError("模型 ID 只能包含英文字母、数字及 . _ : / + -，且不能有空格。")
    if default_model not in models:
        raise ValueError("默认模型必须在模型列表中。")


def _validate(settings: ModelSettings) -> ModelSettings:
    profiles = settings.profiles
    if not profiles:
        raise ValueError("Key 配置不能为空。")
    if any(not isinstance(profile.id, str) or not isinstance(profile.name, str)
           for profile in profiles):
        raise ValueError("Key 配置格式无效。")
    ids = [profile.id for profile in profiles]
    names = [profile.name.strip().casefold() for profile in profiles]
    if (len(set(ids)) != len(ids) or len(set(names)) != len(names)
            or settings.default_key_id not in ids):
        raise ValueError("Key 名称不能重复，且默认 Key 必须存在。")
    for profile in profiles:
        if not _KEY_ID.fullmatch(profile.id) or not profile.name.strip() or len(profile.name) > 40:
            raise ValueError("Key 名称不能为空或超过 40 个字符。")
        _validate_models(profile.models, profile.default_model)
    return ModelSettings(profiles, settings.default_key_id,
                         validate_shortcut(settings.switch_model_shortcut))


def load_model_settings(path: Path = CONFIG_FILE) -> ModelSettings:
    """Load profiles, including the former single-Key format."""
    if not path.is_file():
        return ModelSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        shortcuts = data.get("shortcuts", {})
        if not isinstance(shortcuts, dict):
            raise ValueError
        shortcut = shortcuts.get("switch_model",
                                 data.get("switch_model_shortcut", DEFAULT_SWITCH_MODEL_SHORTCUT))
        if "profiles" in data:
            if not isinstance(data["profiles"], list):
                raise ValueError
            profiles = tuple(KeyProfile(
                item["id"], item["name"], tuple(item["models"]), item["default_model"])
                for item in data["profiles"] if isinstance(item, dict))
            if len(profiles) != len(data["profiles"]):
                raise ValueError
            settings = ModelSettings(profiles, data["default_key_id"], shortcut)
        else:
            settings = ModelSettings((KeyProfile(models=tuple(data["models"]),
                                                 default_model=data["default_model"]),),
                                     "primary", shortcut)
        return _validate(settings)
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("模型配置文件格式无效。") from error


def _save(settings: ModelSettings, path: Path) -> ModelSettings:
    settings = _validate(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".config-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump({
                "profiles": [{"id": item.id, "name": item.name,
                              "models": item.models, "default_model": item.default_model}
                             for item in settings.profiles],
                "default_key_id": settings.default_key_id,
                "shortcuts": {"switch_model": settings.switch_model_shortcut},
            }, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return settings


def save_model_settings(models: list[str], default_model: str, path: Path = CONFIG_FILE,
                        *, key_id: str | None = None,
                        switch_model_shortcut: str | None = None) -> ModelSettings:
    """Update one Key's models; preserve all other profiles."""
    current = load_model_settings(path)
    target = key_id or current.default_key_id
    current.profile(target)
    profiles = tuple(KeyProfile(item.id, item.name, tuple(models), default_model)
                     if item.id == target else item for item in current.profiles)
    return _save(ModelSettings(profiles, current.default_key_id,
                               (current.switch_model_shortcut if switch_model_shortcut is None
                                else switch_model_shortcut)), path)


def save_switch_model_shortcut(shortcut: str, path: Path = CONFIG_FILE) -> ModelSettings:
    current = load_model_settings(path)
    return _save(ModelSettings(current.profiles, current.default_key_id, shortcut), path)


def set_default_key(key_id: str, path: Path = CONFIG_FILE) -> ModelSettings:
    current = load_model_settings(path)
    current.profile(key_id)
    return _save(ModelSettings(current.profiles, key_id, current.switch_model_shortcut), path)


def create_key_profile(name: str, key_value: str, *, path: Path = CONFIG_FILE,
                       env_file: Path = ENV_FILE) -> ModelSettings:
    current = load_model_settings(path)
    sequence = 2
    while f"key{sequence}" in {item.id for item in current.profiles}:
        sequence += 1
    profile = KeyProfile(f"key{sequence}", name.strip(), (), "")
    default_id = (profile.id if current.default_key_id == "primary"
                  and not api_key_configured(env_file) else current.default_key_id)
    candidate = _validate(ModelSettings((*current.profiles, profile), default_id,
                                        current.switch_model_shortcut))
    save_api_key(key_value, env_file, key_id=profile.id)
    return _save(candidate, path)


def update_key_profile(key_id: str, name: str, key_value: str = "", *,
                       path: Path = CONFIG_FILE, env_file: Path = ENV_FILE,
                       make_default: bool = False) -> ModelSettings:
    current = load_model_settings(path)
    current.profile(key_id)
    profiles = tuple(KeyProfile(item.id, name.strip(), item.models, item.default_model)
                     if item.id == key_id else item for item in current.profiles)
    candidate = _validate(ModelSettings(profiles,
                                        key_id if make_default else current.default_key_id,
                                        current.switch_model_shortcut))
    if key_value:
        save_api_key(key_value, env_file, key_id=key_id)
    return _save(candidate, path)


def delete_key_profile(key_id: str, *, path: Path = CONFIG_FILE,
                       env_file: Path = ENV_FILE) -> ModelSettings:
    """Remove a profile and its local credential; retain an empty setup slot if needed."""
    current = load_model_settings(path)
    current.profile(key_id)
    profiles = tuple(item for item in current.profiles if item.id != key_id)
    if not profiles:
        profiles = (KeyProfile("primary", "默认 Key", (), ""),)
    default_id = (profiles[0].id if current.default_key_id == key_id
                  else current.default_key_id)
    candidate = ModelSettings(profiles, default_id, current.switch_model_shortcut)
    secret = read_api_key(env_file, key_id=key_id)
    if secret:
        removed, _ = unset_key(str(env_file), _env_name(key_id))
        if not removed:
            raise OSError("无法删除 API Key。")
    try:
        return _save(candidate, path)
    except (OSError, ValueError):
        if secret:
            save_api_key(secret, env_file, key_id=key_id)
        raise


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
