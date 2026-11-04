"""Locate a native Bash; Windows never silently switches to WSL."""
import os
from pathlib import Path
import shutil


class PowerShellUnavailable(OSError):
    pass


def find_powershell():
    if os.name != "nt":
        raise ValueError("PowerShell backend is Windows-only")
    # Use independently installed runtimes, never a project/PATH shadow or
    # another application's private bundled pwsh. No silent 5.1 fallback.
    candidates=[]
    if os.environ.get('LOCALAPPDATA'):
        candidates.append(Path(os.environ['LOCALAPPDATA'])/'Programs/PowerShell/7/pwsh.exe')
    if os.environ.get('ProgramFiles'):
        candidates.append(Path(os.environ['ProgramFiles'])/'PowerShell/7/pwsh.exe')
    candidates.append(Path.home()/'scoop/apps/pwsh/current/pwsh.exe')
    for path in candidates:
        if path.is_file():
            return path.resolve(strict=True)
    raise PowerShellUnavailable("未找到独立安装的 PowerShell 7，请按 README 安装后重试。")


def is_powershell_runtime(executable):
    path=Path(executable).resolve(strict=True)
    legacy=Path(os.environ.get('SYSTEMROOT',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    if path==legacy.resolve():
        return True
    try:
        return path==find_powershell()
    except (ValueError,OSError):
        return False


def find_bash():
    found = shutil.which("bash")
    if os.name != "nt":
        candidates = [Path(found)] if found else []
    else:
        candidates = []
        # Prefer Git's actual Bash over a launcher shim or WSL's bash.exe.
        git = shutil.which("git")
        if git:
            parent = Path(git).resolve().parent
            candidates += [parent.parent / "bin/bash.exe", parent.parent / "usr/bin/bash.exe"]
        candidates += [Path.home() / "scoop/apps/git/current/bin/bash.exe"]
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if os.environ.get(variable):
                base = Path(os.environ[variable])
                candidates += [base / "Git/bin/bash.exe", base / "Programs/Git/bin/bash.exe"]
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        try:
            path = candidate.resolve(strict=True)
            if not path.is_file():
                continue
            if os.name == "nt":
                windows = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")).resolve()
                if path.is_relative_to(windows) or path.parent.name.casefold() == "shims":
                    continue
            return path
        except (OSError, ValueError):
            continue
    raise ValueError("Native Bash unavailable; install Git Bash or use workspace_run for native commands")
