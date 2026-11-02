"""Private local state shared by all DianStar instances of the current user."""
import ctypes
import os
from pathlib import Path
import stat
import uuid


def state_directory():
    override = os.environ.get("DIANSTAR_FILE_STATE_ROOT")
    if override:
        result = Path(override)
        if not result.is_absolute():
            raise ValueError("File state directory must be absolute")
        return result
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "DianStar" / "file-state"
    return Path.home() / ".local" / "share" / "dianstar" / "file-state"


def private_directory(path):
    path = Path(path).absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.resolve() != path or not path.is_dir():
        raise ValueError("Unsafe file state directory")
    if os.name != "nt" and (path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid()):
        raise ValueError("File state directory must be private")
    return path


def private_file(path):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.resolve() != path.absolute()
            or (os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()))):
        os.close(descriptor)
        raise ValueError("Unsafe file state file")
    return os.fdopen(descriptor, "r+b", buffering=0)


def atomic_private_record(path, data):
    """Publish a complete private state record before changing host permissions."""
    path = Path(path)
    temporary = path.with_name(path.name + ".pending-" + uuid.uuid4().hex)
    try:
        with private_file(temporary) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def protect(data, *, decrypt=False):
    """Windows backups are bound to the current user's DPAPI identity.

    POSIX uses the enclosing 0700 directory and 0600 files, not encryption.
    """
    if os.name != "nt":
        return data
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
        raise OSError("Local backup protection failed")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel.LocalFree(destination.data)
