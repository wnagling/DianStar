"""Discover the packaged, pinned GitHub component without a user-facing setup page."""
import hashlib
import os
from pathlib import Path
import shutil

VERSION = "v2.0.3"
WINDOWS_SHA256 = "647fe7e7f3c1296d1acb936b5c7704bbcdb6bda95b4533c524157446b33f0f16"
MANAGED_PATH = Path(".local/tools/github-mcp-server") / VERSION / "github-mcp-server.exe"


def discover_server(configured, application_root):
    if configured:
        return configured
    candidate = Path(application_root) / MANAGED_PATH
    try:
        if candidate.is_file() and candidate.resolve() == candidate.absolute():
            with candidate.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == WINDOWS_SHA256:
                    return str(candidate)
    except OSError:
        pass
    # Search absolute installation directories explicitly: Windows which() can
    # otherwise prepend the current (potentially untrusted project) directory.
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(directory)
        if not directory.is_absolute() or directory.resolve() == Path.cwd().resolve():
            continue
        installed = shutil.which(str(directory / "github-mcp-server"))
        if installed:
            return installed
    return ""
