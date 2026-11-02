"""File and directory reads, path discovery and bounded content search."""
import asyncio
import hashlib
from importlib.resources import files as resources
import json
import os
from pathlib import Path
import sys
import subprocess

from dianstar.security.content_guard import MAX_TOTAL, SecurityViolation
from .catalog import directory_entries, inventory, match_glob
from .text import FileError, load

WORKER_SOURCE = resources(__package__).joinpath("search_worker.txt").read_text(encoding="utf-8")


def page(entries, offset, limit):
    selected, size = [], 0
    for entry in entries[offset:offset + limit]:
        if size + len(entry) + 1 > 24000:
            break
        selected.append(entry)
        size += len(entry) + 1
    return selected, offset + len(selected) < len(entries)


class FileReader:
    def __init__(self, context, reads):
        self.context, self.reads = context, reads

    def snapshot(self, args):
        path = self.context.path(args)
        with self.context.files.access(self.context.root(args), [path], recover=False):
            value = load(path)
            if value is None:
                raise FileError("file_not_found", "文件不存在，请先定位文件或用 write 创建。")
            self.context.path(args)
            self.context.security.run("before_read", [(args["path"], value.data)])
        return path, value

    def read(self, args):
        path = self.context.path(args, allow_root=True)
        offset, limit = args.get("offset", 1), args.get("limit", 200)
        if path.is_dir():
            with self.context.files.access(self.context.root(args), [path], recover=False):
                entries, incomplete = directory_entries(self.context, args)
            selected, more = page(entries, offset - 1, limit)
            text = self.context.mask("\n".join(selected))
            truncated = incomplete or more
            return {"type": "directory", "content": text[:24000], "truncated": truncated,
                    "next_offset": offset + len(selected) if truncated else None}
        path, value = self.snapshot(args)
        safe = self.context.mask_file(value.text, args["path"])
        lines = safe.splitlines()
        if offset > max(1, len(lines)):
            raise FileError("file_offset_out_of_range", "起始行超过文件范围，请缩小 offset。")
        selected, size = [], 0
        shortened = False
        for number in range(offset - 1, min(len(lines), offset - 1 + limit)):
            line = f"{number + 1}: {lines[number][:1000]}"
            if size + len(line) + 1 > 24000:
                break
            selected.append(line)
            size += len(line) + 1
            shortened |= len(lines[number]) > 1000
        more = offset - 1 + len(selected) < len(lines)
        self.reads[str(path)] = value
        return {"type": "file", "content": self.context.mask("\n".join(selected)), "total_lines": len(lines),
                "truncated": more or shortened, "next_offset": offset + len(selected) if more else None,
                "encoding": "utf-8-sig" if value.bom else "utf-8", "sha256": hashlib.sha256(value.data).hexdigest(),
                "redacted": safe != value.text, "recovery_pending": self.context.files.pending(self.context.root(args))}

    def glob(self, args):
        with self.context.files.access(self.context.root(args), [], recover=False):
            root, prefix, paths, incomplete = inventory(self.context, args)
        pattern = args["pattern"]
        match_glob("", pattern)  # Validate even an empty directory.
        matches = [relative for relative in paths if match_glob(relative[len(prefix):], pattern)]
        offset, limit = args.get("offset", 0), args.get("limit", 100)
        selected, more = page(matches, offset, limit)
        text = self.context.mask("\n".join(selected))
        more = incomplete or more
        return {"content": text[:24000], "truncated": more or len(text) > 24000,
                "next_offset": offset + len(selected) if more else None}

    async def grep(self, args):
        from .io import blocking
        def discover(cancel):
            with self.context.files.access(self.context.root(args), [], cancel=cancel, recover=False):
                return inventory(self.context, args)
        root, prefix, paths, incomplete = await blocking(discover)
        files, total, omitted, redacted = [], 0, 0, 0
        include = args.get("include")
        if include:
            match_glob("", include)
        for relative in paths:
            scoped = relative[len(prefix):]
            if include and not match_glob(scoped if "/" in include else scoped.rsplit("/", 1)[-1], include):
                continue
            try:
                _, snapshot = await blocking(lambda cancel: self.snapshot({**args, "path": relative}))
                total += len(snapshot.data)
                if total > MAX_TOTAL:
                    incomplete = True
                    break
                safe = self.context.mask_file(snapshot.text, relative)
                redacted += int(safe != snapshot.text)
                files.append({"path": relative, "text": safe})
            except (FileError, SecurityViolation, ValueError, OSError):
                omitted += 1
        payload = {"files": files, "pattern": args["pattern"], "literal": args.get("literal", False),
                   "case_sensitive": args.get("case_sensitive", True), "offset": args.get("offset", 0), "limit": args.get("limit", 100)}
        environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        environment["PYTHONIOENCODING"] = "utf-8"
        command = ([sys.executable, "--file-search-worker", hashlib.sha256(WORKER_SOURCE.encode()).hexdigest()]
                   if getattr(sys, "frozen", False) else [sys.executable, "-I", "-c", WORKER_SOURCE])
        process = await asyncio.create_subprocess_exec(*command,
            cwd=str(Path(__file__).resolve().parent), env=environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(payload, ensure_ascii=False).encode()), 5)
        except (TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise FileError("file_search_timeout", "搜索超过时间限制，请缩小范围或使用 literal=true。") from None
        if process.returncode or len(stdout) > 2 * 1024 * 1024:
            raise FileError("file_search_failed", "搜索进程未完成，不能把本次结果当作没有匹配。")
        response = json.loads(stdout)
        if response.get("error"):
            return response
        selected, size = [], 0
        for item in response["matches"]:
            text = f"{item['path']}:{item['line']}: {item['text']}"
            if size + len(text) + 1 > 24000:
                break
            selected.append(text)
            size += len(text) + 1
        more = response["truncated"] or incomplete or len(selected) < len(response["matches"])
        return {"content": self.context.mask("\n".join(selected)), "truncated": more,
                "next_offset": args.get("offset", 0) + len(selected) if more else None,
                "skipped_files": omitted, "redacted_files": redacted, "complete": not more and omitted == 0}
