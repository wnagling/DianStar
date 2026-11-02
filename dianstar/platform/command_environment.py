"""Public build variables for one command; never a persistent shell session."""
import os
from pathlib import Path


BASE_VARIABLES = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"})
BUILD_VARIABLES = frozenset({"JAVA_HOME", "JDK_HOME", "CARGO_HOME", "RUSTUP_HOME", "VIRTUAL_ENV", "CONDA_PREFIX",
    "CC", "CXX", "CMAKE_GENERATOR", "CMAKE_PREFIX_PATH", "CMAKE_TOOLCHAIN_FILE", "PKG_CONFIG_PATH",
    "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "RUSTFLAGS", "RUSTUP_TOOLCHAIN", "CARGO_TARGET_DIR",
    "INCLUDE", "LIB", "LIBPATH", "WINDOWSSDKDIR", "WINDOWSSDKVERSION", "VCINSTALLDIR", "VCTOOLSINSTALLDIR",
    "VSINSTALLDIR", "SDKROOT", "DEVELOPER_DIR"})
OVERRIDE_VARIABLES = BUILD_VARIABLES | {"PATH"}


def environment_schema():
    return {"type": "object", "propertyNames": {"enum": sorted(OVERRIDE_VARIABLES)}, "maxProperties": 24,
            "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 4096},
            "description": "仅本次命令的非敏感构建变量，例如 JAVA_HOME、PATH、CC；值为字面文本，不展开 $PATH。"
                           "优先于已保存的会话项目环境，不写系统或会话配置，也不传令牌；有覆盖项时按后台策略确认。"}


def profile_values(environment, path_prepend, *, check_directories=True):
    if not isinstance(environment, dict) or any(key not in BUILD_VARIABLES for key in environment):
        raise ValueError("Only public build variables can be saved; PATH uses path_prepend")
    validate_overrides(environment)
    if (not isinstance(path_prepend, list) or len(path_prepend) > 16
            or any(not isinstance(value, str) or not 1 <= len(value) <= 4096
                   or any(c in value for c in "\0\r\n" + os.pathsep) for value in path_prepend)
            or sum(map(len, path_prepend)) > 16384):
        raise ValueError("Invalid toolchain search directories")
    paths = []
    for value in path_prepend:
        path = Path(value)
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise ValueError("Toolchain directory must be an absolute local path")
        if check_directories and (not path.is_dir() or path.resolve(strict=True) != path.absolute()):
            raise ValueError("Toolchain directory missing or aliased")
        text = str(path)
        if os.path.normcase(text) not in {os.path.normcase(item) for item in paths}:
            paths.append(text)
    return {"environment": dict(environment), "path_prepend": paths}


def profile_environment(scanner, values, overrides):
    values = profile_values(values["environment"], values["path_prepend"])
    environment = command_environment(scanner, values["environment"])
    if values["path_prepend"]:
        paths = []
        for value in [*values["path_prepend"], *environment.get("PATH", os.defpath).split(os.pathsep)]:
            if value and os.path.normcase(value) not in {os.path.normcase(item) for item in paths}:
                paths.append(value)
        environment["PATH"] = os.pathsep.join(paths)
    # Validate caller overrides separately, then apply only those exact keys.
    validate_overrides(overrides)
    environment.update(overrides)
    return environment


def validate_overrides(overrides):
    if (not isinstance(overrides, dict) or len(overrides) > 24 or any(key not in OVERRIDE_VARIABLES for key in overrides)
            or any(not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(c in value for c in "\0\r\n") for value in overrides.values())
            or sum(len(value) for value in overrides.values()) > 16384):
        raise ValueError("Invalid public command environment")


def command_environment(scanner, overrides=None):
    overrides = {} if overrides is None else overrides
    validate_overrides(overrides)
    environment = {}
    for key, value in os.environ.items():
        if key.upper() in BASE_VARIABLES | BUILD_VARIABLES and not scanner.matches(value, env_style=True):
            environment[key.upper() if os.name == "nt" else key] = value
    environment.update(overrides)
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    return environment
