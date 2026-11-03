"""Bounded filesystem discovery with project-local ignore semantics."""
from functools import lru_cache
from itertools import islice
import fnmatch
import os
from pathlib import Path, PurePosixPath

from dianstar.security.content_guard import protected_path
from .text import FileError
from .ignore import IgnoreRules

MAX_PATHS = 10000
SKIP = {".git", ".local", ".ssh", ".aws", "node_modules", ".venv", "__pycache__", "build", "dist", "target"}


def directory_entries(context, args):
    root = context.root(args)
    directory = context.path(args, allow_root=True)
    children = list(islice(directory.iterdir(), MAX_PATHS + 1))
    selected = []
    for child in sorted(children[:MAX_PATHS]):
        relative = child.relative_to(root).as_posix()
        if protected_path(relative) or (not args.get("hidden", False) and child.name.startswith(".")):
            continue
        try:
            context.path({**args, "path": relative})
            if child.is_file() and child.stat().st_nlink > 1:
                continue
            if not child.is_file() and not child.is_dir():
                continue
            selected.append((relative, child.name + ("/" if child.is_dir() else "")))
        except (ValueError, OSError):
            continue
    ignore = IgnoreRules(root, match_glob)
    selected = [(path, label) for path, label in selected if not ignore.ignored(path, label.endswith("/"))]
    return [label for _, label in selected], len(children) > MAX_PATHS


def match_glob(path, pattern):
    if (not pattern or len(pattern) > 500 or "\\" in pattern or ":" in pattern or pattern.startswith("/")
            or any(part in {"", ".", ".."} for part in pattern.split("/"))):
        raise FileError("file_invalid_pattern", "请使用项目内相对路径模式，例如 **/*.py。")
    parts, patterns = path.split("/"), pattern.split("/")
    if os.name == "nt":
        parts, patterns = [part.casefold() for part in parts], [part.casefold() for part in patterns]

    @lru_cache(None)
    def matches(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == "**":
            return matches(i, j + 1) or (i < len(parts) and matches(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and matches(i + 1, j + 1)

    return matches(0, 0)


def inventory(context, args):
    root = context.root(args)
    directory = context.path({**args, "path": args.get("path", ".")}, allow_root=True)
    if not directory.is_dir():
        raise FileError("file_not_directory", "搜索范围必须是项目内目录。")
    prefix = directory.relative_to(root).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    candidates = []
    ignore = IgnoreRules(root, match_glob)
    def scan_error(error):
        raise FileError("file_scan_incomplete", "部分项目目录无法读取，搜索未完成。") from error
    for parent, directories, files in os.walk(directory, followlinks=False, onerror=scan_error):
        directories[:] = sorted(name for name in directories if name not in SKIP
            and not protected_path((Path(parent) / name).relative_to(root).as_posix())
            and (Path(parent) / name).resolve() == (Path(parent) / name).absolute()
            and not ignore.ignored((Path(parent) / name).relative_to(root).as_posix(), True))
        candidates.extend((Path(parent) / name).relative_to(root).as_posix() for name in sorted(files)
            if not ignore.ignored((Path(parent) / name).relative_to(root).as_posix()))
        if len(candidates) > MAX_PATHS:
            break
    result = []
    for relative in sorted(candidates):
        parts = PurePosixPath(relative).parts
        if not relative.startswith(prefix) or protected_path(relative):
            continue
        if not args.get("hidden", False) and any(part.startswith(".") for part in parts):
            continue
        try:
            path = context.path({**args, "path": relative})
            if not path.is_file() or path.stat().st_nlink > 1:
                continue
        except (ValueError, OSError):
            continue
        result.append(relative)
        if len(result) >= MAX_PATHS:
            break
    return root, prefix, result, len(candidates) > MAX_PATHS or len(result) >= MAX_PATHS
