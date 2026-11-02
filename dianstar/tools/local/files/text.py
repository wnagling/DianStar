"""Bounded UTF-8 snapshots and format preservation."""
import codecs
from dataclasses import dataclass
import os
import re
import stat

MAX_FILE = 2 * 1024 * 1024


class FileError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

    def result(self):
        return {"error": self.code, "message": self.message}


@dataclass(frozen=True)
class TextSnapshot:
    data: bytes
    text: str
    version: tuple
    bom: bool
    newline: str


def version(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode


def load(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or info.st_size > MAX_FILE:
        raise FileError("file_unsupported", "只支持大小受限的普通文本文件，不读取链接或设备文件。")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if version(os.fstat(stream.fileno())) != version(info):
            raise FileError("file_changed", "读取期间文件已变化，请重新读取。")
        data = stream.read(MAX_FILE + 1)
        if version(os.fstat(stream.fileno())) != version(info) or len(data) > MAX_FILE:
            raise FileError("file_changed", "读取期间文件已变化或超出大小限制。")
    if b"\0" in data:
        raise FileError("file_binary", "当前只支持 UTF-8 文本及 UTF-8 BOM，不读取二进制或其他编码。")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise FileError("file_encoding", "文件不是受支持的 UTF-8 编码，未转换或改写。") from None
    ending = re.search(r"\r\n|\n|\r", text)
    return TextSnapshot(data, text, version(info), data.startswith(codecs.BOM_UTF8), ending[0] if ending else "\n")


def normalize_lines(text, newline):
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def encode(text, bom):
    return (codecs.BOM_UTF8 if bom else b"") + text.removeprefix("\ufeff").encode("utf-8")
