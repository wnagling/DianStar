"""Non-overridable host path boundaries shared by local tools and Git scopes."""
import os
from pathlib import Path
from .content_guard import SecurityViolation


class HostSafetyError(SecurityViolation):
    def __init__(self, message, code="host_path_denied"):
        self.code, self.message = code, message
        self.findings, self.source = (), None
        ValueError.__init__(self, message)

    def result(self):
        return {"error": self.code, "message": self.message, "findings": []}


def credential_roots():
    return [Path.home() / name for name in (".ssh", ".aws", ".azure", ".gnupg", ".kube", ".codex")]


def check_host_path(value, *, project=False, managed=False):
    path = Path(value).absolute()
    text = str(path)
    from dianstar.platform.file_state import state_directory
    if path.is_relative_to(state_directory()) and not managed:
        raise HostSafetyError("应用状态、凭据备份及授权记录不能作为项目修改目标。")
    private_roots = credential_roots()
    if any(path == root or path.is_relative_to(root) for root in private_roots):
        raise HostSafetyError("用户凭据库及应用授权目录禁止作为开发工具的修改目标。")
    if (not Path(value).is_absolute() or text.startswith(("\\\\", "//")) or path == path.parent
            or path.resolve() != path or any(ord(char) < 32 for char in text)):
        raise HostSafetyError("磁盘根目录、设备路径、网络路径或经过链接的路径不能作为修改目标。")
    if os.name == "nt":
        if any(":" in part or part.endswith((".", " ")) for part in path.parts[1:]):
            raise HostSafetyError("路径包含 Windows 别名或附加数据流，未操作。")
        windows = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        forbidden = [windows, Path(os.environ.get("WINDIR", str(windows)))]
        forbidden += [Path(os.environ[key]) for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData") if os.environ.get(key)]
        forbidden += [Path(path.anchor) / name for name in ("Boot", "EFI", "Recovery", "System Volume Information", "$Recycle.Bin")]
        if any(path == root or path.is_relative_to(root) for root in forbidden):
            raise HostSafetyError("系统、驱动、启动或程序安装目录禁止通过开发工具修改。")
        if path.parent == Path(path.anchor) and path.name.casefold() in {"bootmgr", "bootnxt", "boot.ini", "ntldr", "ntdetect.com", "pagefile.sys", "hiberfil.sys", "swapfile.sys"}:
            raise HostSafetyError("系统启动及交换文件禁止修改。")
    else:
        if any(path.is_relative_to(Path(root)) for root in ("/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys", "/lib", "/lib64", "/System", "/Library")):
            raise HostSafetyError("系统及设备目录禁止通过开发工具修改。")
    if path in {Path.home(), Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads"}:
        raise HostSafetyError("不能把整个用户目录、桌面或文档目录作为开发工具的修改范围。")
    return path
