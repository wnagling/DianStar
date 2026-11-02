"""OS-held shared/exclusive locks; lock files must never be unlinked."""
from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path
import time

from dianstar.platform.file_state import private_directory, private_file, state_directory


class FileLockError(ValueError):
    pass


class FileLockBusy(FileLockError):
    pass


def canonical(path):
    return os.path.normcase(str(Path(path).absolute()))


def _lock(stream, shared):
    if os.name != "nt":
        import fcntl
        fcntl.flock(stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        return None
    import msvcrt
    from ctypes import wintypes
    class Overlapped(ctypes.Structure):
        _fields_ = [("internal", ctypes.c_size_t), ("high", ctypes.c_size_t), ("offset", wintypes.DWORD),
                    ("offset_high", wintypes.DWORD), ("event", wintypes.HANDLE)]
    overlap = Overlapped()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel.LockFileEx.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(stream.fileno())
    if not kernel.LockFileEx(handle, 1 | (0 if shared else 2), 0, 1, 0, ctypes.byref(overlap)):
        raise ctypes.WinError(ctypes.get_last_error())
    return overlap


def _unlock(stream, token):
    if os.name != "nt":
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    if not kernel.UnlockFileEx(msvcrt.get_osfhandle(stream.fileno()), 0, 1, 0, ctypes.byref(token)):
        raise OSError("File unlock failed")


class FileLocks:
    def __init__(self, directory=None, *, timeout=10):
        self.directory = Path(directory) if directory is not None else state_directory()
        self.timeout = timeout

    @contextmanager
    def hold(self, key, *, shared=False, cancel=None, timeout=None):
        directory = private_directory(self.directory / "locks")
        name = hashlib.sha256(key.encode()).hexdigest() + ".lock"
        with private_file(directory / name) as stream:
            deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
            while True:
                if cancel is not None and cancel.is_set():
                    raise FileLockError("File operation cancelled")
                try:
                    token = _lock(stream, shared)
                    break
                except OSError as error:
                    if (getattr(error, "winerror", None) not in {32, 33, 36}
                            and error.errno not in {11, 13}):
                        raise FileLockError("Unable to acquire file lock") from None
                    if time.monotonic() >= deadline:
                        raise FileLockBusy("File lock is busy; no operation performed") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                _unlock(stream, token)

    @contextmanager
    def repository(self, root, *, shared=False, cancel=None):
        from contextlib import ExitStack
        root = Path(root).absolute()
        # A parent-repository command must also exclude file operations inside
        # nested repositories. Ancestor gates are always shared and ordered.
        with ExitStack() as stack:
            for parent in [*reversed(root.parents), root]:
                stack.enter_context(self.hold("repository:" + canonical(parent), shared=shared if parent == root else True, cancel=cancel))
            yield

    def ensure_ready(self, root):
        """Git/command writes must not bypass an unfinished file transaction."""
        import sqlite3
        from contextlib import closing
        path = self.directory / "history.sqlite3"
        if not path.exists():
            return
        try:
            if path.resolve() != path.absolute():
                raise ValueError("Unsafe recovery database")
            with self.hold("journal-schema"), closing(sqlite3.connect(path, timeout=10)) as connection:
                connection.execute("PRAGMA query_only=ON")
                roots = connection.execute("SELECT root FROM transactions WHERE status IN ('prepared','rolling_back','conflict') LIMIT 1001").fetchall()
            if len(roots) > 1000:
                raise ValueError("Recovery backlog")
            opened = Path(canonical(root))
            if any(opened.is_relative_to(Path(item[0])) or Path(item[0]).is_relative_to(opened) for item in roots):
                raise FileLockError("项目存在未完成的文件恢复，请先查看 file_history 并通过 file_restore 恢复或处理冲突。")
        except FileLockError:
            raise
        except Exception:
            raise FileLockError("无法核对文件恢复状态，未执行项目修改。") from None

    @contextmanager
    def files(self, paths, *, shared=False, cancel=None):
        with self.resources([(path, shared) for path in paths], cancel=cancel):
            yield

    @contextmanager
    def resources(self, paths, *, cancel=None):
        from contextlib import ExitStack
        modes = {}
        for path, shared in paths:
            key = canonical(path)
            modes[key] = modes.get(key, True) and shared
        with ExitStack() as stack:
            for path, shared in sorted(modes.items()):
                stack.enter_context(self.hold("file:" + path, shared=shared, cancel=cancel))
            yield
