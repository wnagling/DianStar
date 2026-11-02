"""Bounded native toolchain discovery; no directory walks, WSL or installers."""
import os
from pathlib import Path
import shutil
import re


def resolve_launcher(path):
    """Resolve a plain Scoop shim without executing it or evaluating config."""
    path = Path(path).resolve(strict=True)
    if os.name != "nt" or path.parent != Path.home() / "scoop/shims":
        return path
    description = path.with_suffix(".shim")
    if not description.is_file() or description.stat().st_size > 8192:
        return path
    text = description.read_text(encoding="utf-8-sig")
    match = re.fullmatch(r'\s*path\s*=\s*"([^"\r\n]+)"\s*', text)
    if match:
        target = Path(match[1])
        if target.is_absolute() and not str(target).startswith("\\\\") and target.name.casefold() == path.name.casefold():
            return target.resolve(strict=True)
    return path


VERSION_ARGUMENTS = {"go": ["version"], "python": ["--version"], "node": ["--version"],
    "rustc": ["--version"], "cargo": ["--version"], "java": ["-version"], "javac": ["-version"],
    "gcc": ["--version"], "g++": ["--version"], "cmake": ["--version"]}


def candidates(program, environment):
    found = shutil.which(program, path=environment.get("PATH", os.defpath))
    paths = [Path(found)] if found else []
    if program == "go":
        if os.name == "nt":
            home = Path.home()
            paths += [Path(os.environ.get("SystemDrive", "C:") + "/Go/bin/go.exe"),
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Go/bin/go.exe",
                home / "scoop/apps/go/current/bin/go.exe", home / "go/bin/go.exe"]
            if os.environ.get("LOCALAPPDATA"):
                paths.append(Path(os.environ["LOCALAPPDATA"]) / "Programs/Go/bin/go.exe")
        else:
            paths += [Path("/usr/local/go/bin/go"), Path("/opt/go/bin/go"), Path.home() / ".local/go/bin/go"]
    # Prefer an actual compiler over a Scoop launcher when both are known.
    return sorted(dict.fromkeys(paths), key=lambda path: path.parent.name.casefold() == "shims")


def located(program, environment, root):
    checked, found = [], []
    for path in candidates(program, environment):
        checked.append(str(path))
        try:
            actual = resolve_launcher(path)
            if actual.is_file() and not actual.is_relative_to(root) and actual not in found:
                found.append(actual)
        except (OSError, ValueError):
            continue
    return checked, found[:2]


def routine_version(argv, root):
    path = Path(argv[0])
    name = path.name.lower().removesuffix(".exe")
    return bool(path.is_absolute() and not path.is_relative_to(root)
        and name in VERSION_ARGUMENTS and list(argv[1:]) == VERSION_ARGUMENTS[name])
