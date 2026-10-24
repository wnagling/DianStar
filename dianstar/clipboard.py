"""Windows 系统剪贴板写入，文本通过标准输入传递，不拼接为命令。"""

import os
from pathlib import Path
import subprocess


def copy_windows_text(text: str) -> None:
    powershell = (Path(os.environ.get("SystemRoot", r"C:\Windows"))
                  / "System32/WindowsPowerShell/v1.0/powershell.exe")
    subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference='Stop'; "
         "[Console]::InputEncoding=[System.Text.UTF8Encoding]::new(); "
         "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
        input=text, encoding="utf-8", capture_output=True, check=True, timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
