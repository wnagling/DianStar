"""Recognize routine validation intent, not an operating-system security boundary."""
import os
from pathlib import Path
import re
import shutil
import sys
from .content_guard import protected_path


def command_plan(argv, root):
    """Bind execution to the executable selected during preparation."""
    executable = argv[0]
    if "/" in executable or "\\" in executable:
        path = Path(executable)
        path = path if path.is_absolute() else root / path
    else:
        found = shutil.which(executable)
        if found is None:
            raise ValueError("Executable unavailable")
        path = Path(found)
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Invalid executable")
    stat = path.stat()
    bound = [str(path), *argv[1:]]
    routine = routine_validation(bound, root)
    kind = ("git_diff_check" if path.name.lower() in {"git", "git.exe"} else argv[2]) if routine else None
    if kind == "compileall" and not any(item == "-e" or item.startswith("-e=") for item in bound[3:]):
        # compileall supports restricting source-file symlinks to this project.
        bound += ["-e", str(root)]
    return {"argv": bound,
            "executable_state": (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns),
            "routine_validation": routine, "validation_kind": kind}


def _project_path(value, root):
    if not value or "\0" in value or value.startswith("-"):
        return False
    if os.name != "nt" and ("\\" in value or ":" in value):
        return False
    path = Path(value)
    path = path if path.is_absolute() else root / path
    resolved = path.resolve()
    return resolved.is_relative_to(root) and not protected_path(resolved.relative_to(root).as_posix())


def _compile_validation(args, root, *, single=False):
    flags = {"-q", "--quiet"} if single else {"-q", "-qq", "-f", "-l", "-b"}
    values = {} if single else {"-r": "number", "-j": "workers", "--workers": "workers", "-o": "optimization",
                               "-x": "text", "-e": "path", "--invalidation-mode": "invalidation"}
    index, targets = 0, 0
    while index < len(args):
        item = args[index]
        index += 1
        if item in flags:
            continue
        option, separator, value = item.partition("=")
        if option in values:
            if not separator:
                if index == len(args):
                    return False
                value = args[index]
                index += 1
            kind = values[option]
            if not value or (kind == "path" and not _project_path(value, root)):
                return False
            if kind == "number" and not re.fullmatch(r"[0-9]{1,3}", value):
                return False
            if kind == "workers" and (not value.isdecimal() or not 1 <= int(value) <= 32):
                return False
            if kind == "optimization" and value not in {"0", "1", "2"}:
                return False
            if kind == "invalidation" and value not in {"timestamp", "checked-hash", "unchecked-hash"}:
                return False
            continue
        if not _project_path(item, root) or (single and Path(item).suffix not in {".py", ".pyw"}):
            return False
        target = Path(item) if Path(item).is_absolute() else root / item
        if not target.exists() or (single and not target.is_file()):
            return False
        targets += 1
    # No compileall target means sys.path, including installed libraries.
    return targets > 0


def _git_diff_check(args):
    return (len(args) >= 2 and args[0] == "diff" and "--check" in args
            and all(item in {"--check", "--cached", "--staged", "--no-ext-diff", "--no-textconv", "--no-color"}
                    for item in args[1:]))


def routine_validation(argv, root):
    """Familiar test, compilation, dependency and whitespace checks.

    Project tests and their configuration execute project code. This recognizer
    controls UX; it does not isolate their filesystem or network effects.
    """
    executable = Path(argv[0])
    if (not executable.is_absolute()
            or (executable.is_relative_to(root) and executable != Path(sys.executable).resolve())):
        return False
    if executable.name.lower() in {"git", "git.exe"}:
        return _git_diff_check(argv[1:])
    if (not re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", executable.name.lower())
            or len(argv) < 3 or argv[1] != "-m"):
        return False
    runner = argv[2]
    if runner in {"compileall", "py_compile"}:
        return _compile_validation(argv[3:], root, single=runner == "py_compile")
    if runner == "pip":
        return argv[3:] == ["check"]
    if runner not in {"pytest", "unittest"}:
        return False
    flags = ({"-q", "-qq", "-v", "-vv", "-x", "-s", "--quiet", "--verbose", "--exitfirst",
              "--disable-warnings", "--strict-markers", "--strict-config", "--lf", "--last-failed",
              "--ff", "--failed-first", "-ra", "-rf", "-rA"} if runner == "pytest" else
             {"-q", "--quiet", "-v", "--verbose", "-b", "--buffer", "-c", "--catch", "-f", "--failfast"})
    args = argv[3:]
    discover = runner == "unittest" and bool(args) and args[0] == "discover"
    values = ({"-k": "text", "-m": "text", "--maxfail": "number", "--durations": "number",
               "--tb": "traceback", "--color": "color"} if runner == "pytest" else
              {"-k": "text"})
    if discover:
        values.update({"-s": "path", "--start-directory": "path", "-t": "path", "--top-level-directory": "path",
                       "-p": "text", "--pattern": "text"})
    index = 1 if discover else 0
    while index < len(args):
        item = args[index]
        index += 1
        if item in flags:
            continue
        option, separator, value = item.partition("=")
        if option in values:
            if not separator:
                if index == len(args):
                    return False
                value = args[index]
                index += 1
            kind = values[option]
            if not value or (kind == "path" and not _project_path(value, root)):
                return False
            if kind == "number" and not re.fullmatch(r"[0-9]{1,6}", value):
                return False
            if kind == "traceback" and value not in {"auto", "long", "short", "line", "native", "no"}:
                return False
            if kind == "color" and value not in {"yes", "no", "auto"}:
                return False
            continue
        if item.startswith("-"):
            return False
        if runner == "pytest":
            if not _project_path(item.split("::", 1)[0], root):
                return False
        elif discover:
            # Discovery directories must use the explicit -s/-t options above.
            return False
        elif re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", item):
            first = item.split(".", 1)[0]
            if not (root / first).exists() and not (root / (first + ".py")).is_file():
                return False
        elif not _project_path(item, root):
            return False
    return True
