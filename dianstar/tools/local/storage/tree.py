"""Bounded ordinary trees, streamed checksums and exact, non-recursive removal."""
import hashlib
import os
from pathlib import Path
import shutil
import stat


MAX_NODES = 50000
MAX_BYTES = 4 * 1024**3


class StorageError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise StorageError("storage_cancelled", "操作已取消，请依据恢复编号核对状态。")


def identity(path):
    info = Path(path).lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or Path(path).resolve() != Path(path).absolute()):
        raise StorageError("storage_link_denied", "暂不移动链接、junction 或经过链接的路径，避免影响其他位置。")
    return [info.st_dev, info.st_ino]


def ordinary_stream(path):
    """Do not silently drop NTFS alternate streams during cross-volume copy."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    class Stream(ctypes.Structure):
        _fields_ = [("size", ctypes.c_longlong), ("name", wintypes.WCHAR * 296)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(Stream), wintypes.DWORD]
    kernel.FindFirstStreamW.restype = wintypes.HANDLE
    kernel.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Stream)]
    kernel.FindNextStreamW.restype = wintypes.BOOL
    kernel.FindClose.argtypes = [wintypes.HANDLE]
    item = Stream()
    handle = kernel.FindFirstStreamW(str(path), 0, ctypes.byref(item), 0)
    if handle == ctypes.c_void_p(-1).value:
        if ctypes.get_last_error() in {38, 1, 50}:  # no streams / filesystem has no stream support
            return
        raise StorageError("storage_stream_check_failed", "无法完整检查文件数据流，未移动。")
    try:
        while True:
            if item.name != "::$DATA":
                raise StorageError("storage_stream_unsupported", "对象包含 NTFS 附加数据流，暂不回收，避免跨盘复制丢失数据。")
            if not kernel.FindNextStreamW(handle, ctypes.byref(item)):
                if ctypes.get_last_error() != 38:
                    raise StorageError("storage_stream_check_failed", "数据流检查未完成，未移动。")
                break
    finally:
        kernel.FindClose(handle)


def snapshot(root, cancel=None, *, max_bytes=MAX_BYTES, max_nodes=MAX_NODES):
    root = Path(root)
    if not root.exists() and not root.is_symlink():
        return None
    nodes, total = [], 0
    pending = [(root, ".")]
    while pending:
        check_cancel(cancel)
        path, relative = pending.pop()
        ident = identity(path)
        before = path.lstat()
        if getattr(before, "st_file_attributes", 0) & (0x4000 | 0x1000):
            raise StorageError("storage_file_unsupported", "加密或离线文件暂不回收，避免改变保护状态。")
        ordinary_stream(path)
        directory = stat.S_ISDIR(before.st_mode)
        if not directory and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
            raise StorageError("storage_file_unsupported", "只支持普通文件和目录，不移动设备文件或多重硬链接。")
        node = {"path": relative, "directory": directory, "identity": ident,
                "mode": stat.S_IMODE(before.st_mode), "mtime": before.st_mtime_ns, "size": 0}
        if directory:
            children = []
            for child in path.iterdir():
                check_cancel(cancel)
                children.append(child)
                if len(nodes) + len(pending) + len(children) + 1 > max_nodes:
                    raise StorageError("storage_too_large", "目录项超过本次检查上限，未继续。")
            children.sort(key=lambda item: item.name)
            pending.extend((child, child.name if relative == "." else relative + "/" + child.name) for child in reversed(children))
        else:
            total += before.st_size
            if total > max_bytes:
                raise StorageError("storage_too_large", "本次对象超过4 GiB安全检查上限，未继续移动。")
            digest = hashlib.sha256()
            consumed = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    check_cancel(cancel)
                    consumed += len(chunk)
                    if consumed > before.st_size:
                        raise StorageError("storage_changed", "读取期间文件增长，未继续操作。")
                    digest.update(chunk)
            after = path.lstat()
            if (identity(path) != ident or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns)):
                raise StorageError("storage_changed", "检查期间文件发生变化，未继续操作。")
            node.update(size=before.st_size, sha256=digest.hexdigest())
        nodes.append(node)
        if len(nodes) > max_nodes:
            raise StorageError("storage_too_large", "对象超过50000个文件和目录的检查上限。")
    nodes.sort(key=lambda item: item["path"])
    return {"nodes": nodes, "size": total, "files": sum(not item["directory"] for item in nodes),
            "directories": sum(item["directory"] for item in nodes)}


def same(left, right, *, identities=True):
    if left is None or right is None:
        return left is right
    def values(tree):
        return [{key: value for key, value in node.items() if key != "identity" or identities} for node in tree["nodes"]]
    return values(left) == values(right)


def rename_new(source, destination):
    source, destination = Path(source), Path(destination)
    identity(source)
    identity(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise StorageError("storage_destination_exists", "目标位置已经存在，未覆盖。")
    os.rename(source, destination)
    from ..files.recovery import sync_directory
    sync_directory(source.parent)
    if destination.parent != source.parent:
        sync_directory(destination.parent)


def copy_tree(source, destination, expected, cancel=None):
    """Retain the complete source until the destination has been verified."""
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise StorageError("storage_destination_exists", "复制目标已经存在，未覆盖。")
    for node in sorted(expected["nodes"], key=lambda item: (item["path"].count("/"), item["path"])):
        check_cancel(cancel)
        src = source if node["path"] == "." else source / node["path"]
        dst = destination if node["path"] == "." else destination / node["path"]
        identity(src)
        if node["directory"]:
            dst.mkdir(mode=0o700)
        else:
            with src.open("rb") as reader, dst.open("xb") as writer:
                consumed = 0
                while chunk := reader.read(1024 * 1024):
                    check_cancel(cancel)
                    consumed += len(chunk)
                    if consumed > node["size"]:
                        raise StorageError("storage_changed", "复制期间文件增长，原件继续保留。")
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            shutil.copystat(src, dst, follow_symlinks=False)
    # Restore directory timestamps after creating all children.
    for node in sorted(expected["nodes"], key=lambda item: item["path"].count("/"), reverse=True):
        if node["directory"]:
            src = source if node["path"] == "." else source / node["path"]
            dst = destination if node["path"] == "." else destination / node["path"]
            shutil.copystat(src, dst, follow_symlinks=False)
    copied = snapshot(destination, cancel)
    if not same(snapshot(source, cancel), expected) or not same(copied, expected, identities=False):
        raise StorageError("storage_copy_changed", "复制校验未通过，原内容继续保留，未删除原件。")
    return copied


def remove_verified(root, expected, cancel=None):
    """Only remove recorded entries; unexpected files survive, never rmtree."""
    root = Path(root)
    current = snapshot(root, cancel)
    if current is None:
        return
    recorded = {node["path"]: node for node in expected["nodes"]}
    for node in current["nodes"]:
        old = recorded.get(node["path"])
        if old is None or node["identity"] != old["identity"] or node["directory"] != old["directory"]:
            raise StorageError("storage_cleanup_conflict", "清理对象出现未记录内容或路径被替换，已保留现场。")
        if not node["directory"] and node != old:
            raise StorageError("storage_cleanup_conflict", "清理对象内容已变化，已保留现场。")
    for node in sorted(current["nodes"], key=lambda item: (item["path"].count("/"), item["path"]), reverse=True):
        check_cancel(cancel)
        path = root if node["path"] == "." else root / node["path"]
        if identity(path) != node["identity"]:
            raise StorageError("storage_cleanup_conflict", "清理期间路径发生变化，未继续删除。")
        if node["directory"]:
            path.rmdir()  # A new/unrecorded child makes this fail safely.
        else:
            if not same(snapshot(path, cancel), {**current, "nodes": [{**node, "path": "."}]}):
                raise StorageError("storage_cleanup_conflict", "清理期间文件变化，未继续删除。")
            if not path.stat().st_mode & stat.S_IWRITE:
                path.chmod(path.stat().st_mode | stat.S_IWRITE)
            path.unlink()
