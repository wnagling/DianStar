"""Early rejection of explicit machine administration; the OS boundary is separate."""
import json
import ntpath
from pathlib import PureWindowsPath
import re
import shlex

from .system_paths import HostSafetyError

DENIED = frozenset({"diskpart", "format", "fdisk", "sfdisk", "parted", "wipefs", "mkfs", "mkswap", "dd", "bcdedit", "bcdboot", "bootrec", "bootsect", "mountvol",
    "pnputil", "devcon", "drvload", "dism", "regedit", "runas", "sudo", "su", "doas", "psexec", "psexec64", "schtasks", "wmic", "mshta", "wscript", "cscript",
    "takeown", "icacls", "cacls", "subinacl", "setfacl", "shutdown", "reboot", "poweroff", "taskkill", "netsh", "regsvr32", "rundll32",
    "wsl", "wslconfig", "docker", "podman", "vboxmanage", "vmrun"})
PS_DENIED = re.compile(r"(?i)\b(?:format-volume|clear-disk|initialize-disk|remove-partition|set-partition|remove-windowsdriver|add-windowsdriver|disable-pnpdevice|uninstall-pnpdevice|set-acl|set-executionpolicy|(?:new|set|remove|start|stop|restart|suspend|resume)-service|(?:enable|disable|register|unregister|set)-scheduledtask|(?:new|set|remove|enable|disable)-local(?:user|group)|start-process\b[^\r\n]*-verb\s+runas|invoke-cimmethod|invoke-wmimethod)\b")


def _name(value):
    return PureWindowsPath(value.strip("\"'").replace("/", "\\")).name.casefold().removesuffix(".exe").removesuffix(".com")


def _check(argv, depth=0):
    if depth > 8:
        raise HostSafetyError("嵌套命令超过检查上限，未执行。", "dangerous_command_denied")
    if not argv:
        return
    name, args = _name(argv[0]), argv[1:]
    if name in DENIED or name.startswith("mkfs."):
        raise HostSafetyError("该操作涉及磁盘、驱动、启动项、权限或提权，开发命令工具禁止执行。", "dangerous_command_denied")
    if name in {"sc", "net", "reg"} and (not args or args[0].casefold() not in {
            "sc": {"query", "queryex", "qc", "qdescription"}, "net": {"help", "helpmsg"}, "reg": {"query"}}[name]):
        raise HostSafetyError("禁止通过开发命令修改系统服务、账号或注册表。", "dangerous_command_denied")
    if name in {"powershell", "pwsh"}:
        script = " ".join(args).replace("`", "")
        if any(re.match(r"(?i)^[-/](?:e|en|enc|enco|encod|encode|encodedcommand|encodedarguments)$", item) for item in args) or PS_DENIED.search(script):
            raise HostSafetyError("禁止编码后隐藏命令、提权或修改系统磁盘及驱动。", "dangerous_command_denied")
        check_script(script, depth+1)
    if name in {"cmd", "bash", "sh", "dash", "zsh"}:
        for index, item in enumerate(args):
            if item.casefold() in {"-c", "-lc", "/c", "/k"} and index+1 < len(args):
                check_script(" ".join(args[index+1:]), depth+1, preserve_paths=name == "cmd")
                break
    if name in {"rm", "rmdir", "rd", "del", "erase", "remove-item"}:
        for item in args:
            clean = item.strip("\"'").replace("\\", "/").casefold()
            # Normalize Windows separators and dot segments before matching;
            # c:// and c:/safe/.. must receive the same denial as c:/.
            if re.match(r"^[a-z]:/", clean):
                clean = ntpath.normpath(clean).replace("\\", "/")
            else:
                clean = re.sub(r"/+", "/", clean)
            if (re.fullmatch(r"(?:[a-z]:/?|/[a-z]/?|/|/[a-z]/\*|[a-z]:/\*)", clean)
                    or re.match(r"^(?:[a-z]:|/[a-z])/(?:windows|program files(?: \(x86\))?|programdata|boot|efi|recovery)(?:/|$)", clean)):
                raise HostSafetyError("禁止删除磁盘根目录、系统、驱动或程序安装目录。", "dangerous_command_denied")


def check_script(script, depth=0, *, preserve_paths=False):
    if depth > 8:
        raise HostSafetyError("嵌套命令超过检查上限，未执行。", "dangerous_command_denied")
    try:
        lexer = shlex.shlex(script, posix=not preserve_paths, punctuation_chars=";&|()\n")
        lexer.whitespace_split = True
        current = []
        for token in lexer:
            if token and all(char in ";&|()\n" for char in token):
                _check(current, depth)
                current = []
            else:
                current.append(token)
        _check(current, depth)
    except ValueError as error:
        if isinstance(error, HostSafetyError):
            raise
        # PowerShell/CMD do not use POSIX quote rules. OS restrictions remain
        # mandatory even when text cannot be parsed as Bash.
        if PS_DENIED.search(script.replace("`", "")):
            raise HostSafetyError("禁止修改系统磁盘、驱动或权限。", "dangerous_command_denied") from None


def inspect_command(kind, data):
    text = data.decode("utf-8")
    if kind == "command":
        _check(json.loads(text))
    elif kind in {"bash", "powershell"}:
        if kind == "powershell" and PS_DENIED.search(text.replace("`", "")):
            raise HostSafetyError("禁止修改系统磁盘、驱动、权限或提权。", "dangerous_command_denied")
        check_script(text, preserve_paths=kind == "powershell")
