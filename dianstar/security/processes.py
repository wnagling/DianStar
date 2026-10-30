"""Terminate an owned process tree without invoking a shell."""
import os
from pathlib import Path
import signal
import subprocess


def terminate_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        executable = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe"
        try:
            subprocess.run([str(executable), "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
