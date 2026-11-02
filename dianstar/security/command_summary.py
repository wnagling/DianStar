"""Small trusted purpose labels for display only, never approval decisions."""
from pathlib import PureWindowsPath
import re
import shlex


def command_purpose(name, args):
    if name == "workspace_run":
        argv = args["argv"]
    else:
        command = args["command"]
        # Only peel common output presentation. Added commands remain visible
        # as an unknown script, not hidden behind an installation label.
        plain = re.sub(r"\s*\|\s*(?:head|tail)\s+-\d{1,3}\s*$", "", command)
        plain = re.sub(r"\s+2>&1\s*$", "", plain)
        try:
            argv = shlex.split(plain)
        except ValueError:
            argv = []
        if re.search(r"[;&|<>$`()\n\r]", plain):
            argv = []
    program = PureWindowsPath(argv[0]).name.lower().removesuffix(".exe").removesuffix(".cmd") if argv else ""
    if program == "scoop" and len(argv) == 3 and argv[1] == "install" and re.fullmatch(r"go(?:@[\d.]+)?", argv[2]):
        return ("通过 Scoop 为当前账号安装 Go", "会下载 Go 并更新当前账号的开发环境，可能需要几分钟。")
    if program == "go" and len(argv) >= 2:
        if argv[1] == "version" and len(argv) == 2:
            return ("检查 Go 版本", "查询本机已有 Go 的版本，不安装软件。")
        if argv[1] in {"run", "build"} and not any(value.startswith("-") for value in argv[2:]):
            return ("编译并运行 Go 项目" if argv[1] == "run" else "编译 Go 项目", "会执行项目构建，并可能生成程序和缓存；首次构建通常更慢。")
    if program == "wsl":
        return ("在 WSL 的 Linux 环境中执行命令", "这会使用另一套开发环境；原始命令见详情。")
    if program and len(argv) == 2 and argv[1] in {"--version", "-version"}:
        return ("检查 " + program + " 的版本", "查询本机程序版本。")
    return ("运行一段 PowerShell 项目脚本" if name == "shell" else "运行一段项目脚本" if name == "bash" else "运行项目程序" + ("（" + program + "）" if program else ""),
            "会执行项目程序，并可能修改项目文件；完整执行内容请查看详情。")


def activity(action):
    if action.name in {"workspace_run", "bash", "shell"}:
        return command_purpose(action.name, action.arguments)[0]
    return {"workspace_environment_inspect": "检查本机开发环境", "write": "创建或写入项目文件",
        "mcp_git__git_init": "创建项目", "workspace_environment_set": "保存项目开发环境"}.get(action.name)
