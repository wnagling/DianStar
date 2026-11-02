"""Bounded patch parsing and exact, unambiguous context matching; no shell."""
from dataclasses import dataclass
import re

from .text import FileError

MAX_PATCH = 256 * 1024


def invalid(message="补丁格式无效；请使用 Begin/End Patch 和 Add/Update/Delete File 格式。"):
    raise FileError("file_patch_invalid", message)


@dataclass(frozen=True)
class Hunk:
    lines: tuple[str, ...]
    eof: bool = False


@dataclass(frozen=True)
class PatchFile:
    operation: str
    path: str
    content: str = ""
    hunks: tuple[Hunk, ...] = ()
    destination: str | None = None


def parse_patch(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_PATCH or "\0" in text:
        invalid("补丁必须是至多 256 KiB 的 UTF-8 文本。")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    if "\r" in "".join(lines) or len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        invalid()
    files, index = [], 1
    while index < len(lines) - 1:
        header = lines[index]
        operation = next((name for name in ("Add", "Update", "Delete") if header.startswith(f"*** {name} File: ")), None)
        if operation is None:
            invalid()
        path = header[len(f"*** {operation} File: "):]
        if not path or path != path.strip():
            invalid("补丁文件路径不能为空或含首尾空格。")
        index += 1
        destination = None
        if operation == "Update" and index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
            destination = lines[index][len("*** Move to: "):]
            if not destination or destination != destination.strip():
                invalid("补丁移动目标路径无效。")
            index += 1
        body = []
        while index < len(lines) - 1 and not lines[index].startswith(("*** Add File: ", "*** Update File: ", "*** Delete File: ")):
            body.append(lines[index])
            index += 1
        if operation == "Add":
            if any(not line.startswith("+") for line in body):
                invalid("新增文件的每行内容必须以 + 开头。")
            files.append(PatchFile("add", path, "".join(line[1:] + "\n" for line in body)))
        elif operation == "Delete":
            if body:
                invalid("Delete File 后不能附带正文。")
            files.append(PatchFile("delete", path))
        else:
            hunks, cursor = [], 0
            while cursor < len(body):
                if body[cursor] != "@@":
                    invalid("每段修改以独立一行 @@ 开始；上下文行用空格，删除行用 -，新增行用 +。")
                cursor += 1
                chunk = []
                while cursor < len(body) and body[cursor] != "@@":
                    if body[cursor] == "*** End of File":
                        break
                    if not body[cursor] or body[cursor][0] not in " +-":
                        invalid("补丁内容行缺少空格、- 或 + 前缀。")
                    chunk.append(body[cursor])
                    cursor += 1
                eof = cursor < len(body) and body[cursor] == "*** End of File"
                if eof:
                    cursor += 1
                    if cursor != len(body):
                        invalid("End of File 只能出现在文件最后一段。")
                if not chunk or not any(line[0] in "+-" for line in chunk):
                    invalid("每段补丁必须包含实际新增或删除。")
                hunks.append(Hunk(tuple(chunk), eof))
            if not hunks and destination is None:
                invalid("Update File 必须包含修改段或 Move to。")
            files.append(PatchFile("update", path, hunks=tuple(hunks), destination=destination))
        if len(files) > 20:
            invalid("一份补丁最多包含 20 个文件段，移动的源和目标各计一个事务路径。")
    if not files or len(patch_paths(files)) > 20:
        invalid("补丁必须修改 1 至 20 个事务路径。")
    if sum(len(item.hunks) for item in files) > 200:
        invalid("一份补丁最多包含 200 个修改段，请缩小范围。")
    return tuple(files)


def patch_paths(files):
    return [path for item in files for path in ((item.path, item.destination) if item.destination else (item.path,))]


def apply_hunks(snapshot, hunks):
    # Match whole lines, including whitespace. Never use fuzzy matching or let
    # an earlier replacement become a later hunk's source context.
    original = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", snapshot.text)
    if original and original[-1] == "":
        original.pop()
    lines = [line.rstrip("\r\n") for line in original]
    cursor, output = 0, []
    for hunk in hunks:
        old = [line[1:] for line in hunk.lines if line[0] != "+"]
        if not old:
            if lines or cursor:
                raise FileError("file_patch_ambiguous", "插入已有文件时必须提供上下文；未修改任何文件。")
            start = 0
        elif hunk.eof:
            start = len(lines) - len(old)
            if start < cursor or lines[start:] != old:
                raise FileError("file_patch_mismatch", "补丁末尾上下文与文件不一致；未修改任何文件。")
        else:
            source = "\n" + "\n".join(lines[cursor:]) + "\n"
            needle = "\n" + "\n".join(old) + "\n"
            found = source.find(needle)
            if found < 0:
                raise FileError("file_patch_mismatch", "补丁上下文与文件不一致；请重新读取文件。")
            if source.find(needle, found + 1) >= 0:
                raise FileError("file_patch_ambiguous", "补丁上下文匹配多处；请补充上下文。")
            start = cursor + source[:found].count("\n")
        output.extend(original[cursor:start])
        position = start
        for line in hunk.lines:
            if line[0] == " ":
                output.append(original[position])
            elif line[0] == "+":
                output.append(line[1:] + snapshot.newline)
            if line[0] != "+":
                position += 1
        cursor = start + len(old)
    output.extend(original[cursor:])
    for index in range(len(output) - 1):
        if not output[index].endswith(("\n", "\r")):
            output[index] += snapshot.newline
    if output and snapshot.text and not snapshot.text.endswith(("\n", "\r")):
        output[-1] = output[-1].removesuffix("\r\n").removesuffix("\n").removesuffix("\r")
    return "".join(output)
