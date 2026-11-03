"""Local project policy and remembered directories, independent of transports."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile

from .paths import CONFIG_FILE

PROJECT_CONFIG_FILE = CONFIG_FILE.with_name("projects.json")


@dataclass(frozen=True)
class ProjectSettings:
    enabled: bool = True
    write_enabled: bool = True
    roots: tuple[str, ...] = ()
    workspace_roots: tuple[str, ...] = ()
    allow_external_paths: bool = True


def normalize(data):
    if not isinstance(data, dict) or set(data) - set(ProjectSettings.__dataclass_fields__):
        raise ValueError("Invalid local project settings")
    defaults = ProjectSettings()
    for key in ("enabled", "write_enabled", "allow_external_paths"):
        if type(data.get(key, getattr(defaults, key))) is not bool:
            raise ValueError("Invalid local project policy")
    paths = {}
    for key in ("roots", "workspace_roots"):
        values = data.get(key, ())
        if not isinstance(values, (list, tuple)) or len(values) > 100:
            raise ValueError("Invalid local project scope")
        paths[key] = []
        for value in values:
            if (not isinstance(value, str) or not value or not Path(value).is_absolute()
                    or value.startswith(("\\\\", "//")) or any(ord(c) < 32 for c in value)):
                raise ValueError("Invalid local project directory")
            path = str(Path(value).absolute())
            if path not in paths[key]:
                paths[key].append(path)
    return ProjectSettings(**{key: data.get(key, getattr(defaults, key)) for key in
        ("enabled", "write_enabled", "allow_external_paths")}, **{key: tuple(values) for key, values in paths.items()})


def load_project_settings(path=PROJECT_CONFIG_FILE, *, fallback=None):
    path = Path(path)
    if not path.exists():
        return fallback if fallback is not None else ProjectSettings()
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        return normalize(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError, OSError):
        raise ValueError("本地项目配置损坏，已停止本地工具；请恢复配置备份。") from None


def save_project_settings(settings, path=PROJECT_CONFIG_FILE):
    path = Path(path)
    settings = normalize(asdict(settings))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".projects-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(settings), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return settings
