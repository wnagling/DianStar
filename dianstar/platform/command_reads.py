"""One read-only capability per host lifetime, not one recursive ACL per call."""
import atexit
import hashlib
import json
from pathlib import Path
import sys
import uuid

from .file_state import private_directory, protect, state_directory, atomic_private_record
from dianstar.security.file_locks import FileLocks
from dianstar.security.content_guard import protected_path
from dianstar.security.system_paths import HostSafetyError, credential_roots

_catalog = None
CATALOG_DIRECTORY = state_directory() / "command-security"


class ReadCatalog:
    def __init__(self, api):
        self.api, self.roots = api, []
        identifier = uuid.uuid4().hex
        digest = hashlib.sha256(identifier.encode()).digest()
        self.sid = "S-1-15-3-1024-" + "-".join(str(int.from_bytes(digest[i:i+4], "little")) for i in range(0, 32, 4))
        directory = private_directory(CATALOG_DIRECTORY)
        self.locks = FileLocks(CATALOG_DIRECTORY.parent, timeout=60)
        with self.locks.hold("runtime-read-acl"):
            self.recover(directory)
        self.path = directory / ("read-" + identifier + ".json")
        self.lease = self.locks.hold("command-acl-live:" + self.path.stem, timeout=0)
        self.lease.__enter__()
        atexit.register(self.close)

    def recover(self, directory):
        for journal in directory.glob("read-*.json"):
            from dianstar.security.file_locks import FileLockBusy
            try:
                with self.locks.hold("command-acl-live:" + journal.stem, timeout=0):
                    record = json.loads(protect(journal.read_bytes(), decrypt=True))
                    for entry in record["paths"]:
                        path = Path(entry["path"])
                        if path.exists() and [path.stat().st_dev, path.stat().st_ino] == entry["identity"]:
                            self.api.edit_acl(path, record["sid"], mode=4, inherit=path.is_dir())
                        elif path.exists():
                            raise OSError("Toolchain identity changed; retaining permission recovery record")
                    journal.unlink()
            except FileLockBusy:
                continue

    def allow(self, roots, *, executable=None):
        with self.locks.hold("runtime-read-acl"):
            return self._allow(roots, executable=executable)

    def _allow(self, roots, *, executable=None):
        for path in roots:
            path = Path(path).resolve(strict=True)
            if not self.api.can_edit(path):
                if executable is not None and Path(executable).resolve().is_relative_to(path):
                    raise HostSafetyError("当前工具链目录无法建立受控只读访问，目标程序未执行。请使用用户可管理的工具链位置；不会提权或修改系统目录权限。", "command_runtime_inaccessible")
                continue  # Unrelated SDK variables never enlarge access.
            if str(path) in {entry["path"] for entry in self.roots if entry.get("applied")}:
                continue
            info = path.stat()
            self.roots.append({"path": str(path), "identity": [info.st_dev, info.st_ino]})
            atomic_private_record(self.path, protect(json.dumps({"kind": "read_catalog", "sid": self.sid, "paths": self.roots}).encode()))
            self.api.edit_acl(path, self.sid, permissions=0x1200A9, inherit=path.is_dir())
            self.roots[-1]["applied"] = True
        return self.sid

    def close(self):
        with self.locks.hold("runtime-read-acl"):
            self._close()

    def _close(self):
        if self.lease is None:
            return
        complete = True
        try:
            for entry in self.roots:
                path = Path(entry["path"])
                try:
                    if path.exists() and [path.stat().st_dev, path.stat().st_ino] == entry["identity"]:
                        self.api.edit_acl(path, self.sid, mode=4, inherit=path.is_dir())
                    elif path.exists():
                        complete = False
                except (ValueError, OSError):
                    complete = False
            if complete:
                self.path.unlink(missing_ok=True)
        finally:
            self.lease.__exit__(None, None, None)
            self.lease = None


def allow_runtime(api, roots, *, executable=None):
    global _catalog
    if _catalog is None:
        _catalog = ReadCatalog(api)
    return _catalog.allow(roots, executable=executable)


def runtime_roots(executable, root, environment=None, *, protected=()):
    import os
    exe, root = Path(executable).resolve(strict=True), Path(root)
    windows = Path(os.environ["SYSTEMROOT"]).resolve()
    result = [Path(sys.base_prefix).resolve()]
    private = [Path(item).absolute() for item in (*protected, state_directory(), *credential_roots())]
    if protected_path(exe.name) or (not exe.is_relative_to(root) and any(exe == item or exe.is_relative_to(item) for item in private)):
        raise HostSafetyError("受保护配置或应用状态不能作为可执行程序接入。", "dangerous_command_denied")
    if not exe.is_relative_to(windows) and not exe.is_relative_to(root):
        if exe.parent.name.casefold() in {"bin", "scripts", "cmd"}:
            result.append(exe.parent.parent)
        else:
            result.append(exe.parent)
    for key in ("JAVA_HOME", "JDK_HOME", "RUSTUP_HOME"):
        value = (environment or {}).get(key)
        if value and Path(value).is_dir():
            result.append(Path(value).resolve())
    system = [windows, *[Path(os.environ[key]).resolve() for key in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(key)]]
    selected = []
    broad = {Path.home(), Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads", Path.home() / ".cargo"}
    for path in dict.fromkeys(result):
        if path == root or path.is_relative_to(root):
            continue  # Never turn a project's grant into a host-wide SDK lease.
        if root.is_relative_to(path) and path != Path(sys.base_prefix).resolve():
            if exe.is_relative_to(path) and not exe.is_relative_to(root):
                selected.append(exe)
            continue
        if any(path.is_relative_to(item) for item in system):
            continue
        if path == path.parent or path in broad or any(item.is_relative_to(path) or path.is_relative_to(item) for item in private):
            # Never read-authorize a profile, desktop or application state just
            # because an executable/SDK variable points there.
            if exe.is_relative_to(path):
                selected.append(exe)
            continue
        selected.append(path)
    return selected
