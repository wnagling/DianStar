"""Windows Forms 右键菜单。与终端选择逻辑分离，不向子进程发送聊天内容。"""

import base64
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
from typing import Literal


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        raise OSError("Unable to read cursor position")
    return point.x, point.y


class WindowsCopyMenuClient:
    def __init__(self) -> None:
        self._process = None
        self._lock = asyncio.Lock()

    async def _read(self, timeout: float) -> str:
        line = await asyncio.wait_for(self._process.stdout.readline(), timeout)
        if not line:
            raise OSError("Menu process exited")
        return line.decode("utf-8").strip()

    async def _ensure_started(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        script = Path(__file__).with_name("assets").joinpath("copy_menu.ps1").read_text(encoding="utf-8")
        powershell = (Path(os.environ.get("SystemRoot", r"C:\Windows"))
                      / "System32/WindowsPowerShell/v1.0/powershell.exe")
        self._process = await asyncio.create_subprocess_exec(
            str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
            "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode("ascii"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if await self._read(20) != "ready":
            raise ValueError("Invalid menu startup response")

    async def start(self) -> None:
        async with self._lock:
            try:
                await self._ensure_started()
            except BaseException:
                await self.close()
                raise

    async def show(self, position: tuple[int, int]) -> Literal["copy", "cancel", "outside"]:
        async with self._lock:
            try:
                await self._ensure_started()
                payload = json.dumps({"x": position[0], "y": position[1]}) + "\n"
                self._process.stdin.write(payload.encode("utf-8"))
                await self._process.stdin.drain()
                if await self._read(10) != "shown":
                    raise ValueError("Invalid menu display response")
                choice = await self._read(120)
                if choice not in {"copy", "cancel", "outside"}:
                    raise ValueError("Invalid menu response")
                return choice
            except BaseException:
                # 取消后重建连接，不能把旧返回误用于下一次复制。
                await self.close()
                raise

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
