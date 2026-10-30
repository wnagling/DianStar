"""Project file tools and explicit process execution within configured repositories."""
import asyncio
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from dotenv import dotenv_values
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.security.authorization import request_key
from dianstar.security.gateway import ToolPreparationError
from dianstar.tools.mcp.servers.git.operations import sensitive
from dianstar.security.content_guard import ContentScanner, SecurityViolation, known_credentials, local_configuration
from dianstar.hooks import HookRunner
from dianstar.hooks.workspace import WorkspaceAudit


class WorkspaceTools:
    def __init__(self, mcp, env_file):
        self.mcp, self.env_file = mcp, env_file
        self.prepared = {}
        github = getattr(mcp, "github", None)
        self.security = HookRunner(ContentScanner(known=known_credentials(env_file,
            extra=(github.token(),) if github else ())))

    def root(self, args):
        root = Path(args["repository"])
        if not self.mcp.settings.git_enabled or not root.is_absolute() or str(root) not in self.mcp.git.repositories or root.resolve(strict=True) != root.absolute():
            raise ValueError("Repository not enabled")
        return root

    def path(self, args, *, local_only=False):
        root = self.root(args)
        relative = args["path"]
        if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
                or relative.startswith("/") or any(p in {"", ".", ".."} for p in relative.split("/"))
                or (sensitive(relative) and not (local_only and local_configuration(relative)))):
            raise ValueError("Invalid project path")
        path = root / relative
        if path.resolve() != path.absolute() or not path.is_relative_to(root):
            raise ValueError("Project path escaped repository")
        if local_only and path.absolute() == self.env_file.absolute():
            raise ValueError("Application credentials require the configuration editor")
        return path

    def mask(self, value):
        values = list(dotenv_values(self.env_file).values()) if self.env_file.is_file() else []
        github = getattr(self.mcp, "github", None)
        if github:
            values.append(github.token())
        values += [value for key, value in os.environ.items() if any(x in key.upper() for x in ("TOKEN", "API_KEY", "PASSWORD"))]
        for secret in values:
            if secret and len(secret) >= 8:
                value = value.replace(secret, "[已遮挡密钥]")
        return self.security.scanner.redact(value)

    async def read(self, action):
        try:
            args, name = action.arguments, action.name
            if name == "workspace_read":
                path = self.path(args)
                if path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("File too large")
                data = path.read_bytes()
                if b"\0" in data:
                    raise ValueError("Binary file")
                lines = data.decode("utf-8").splitlines()
                start, count = args.get("start_line", 1), args.get("line_count", 200)
                text = "\n".join(f"{i+1}: {line}" for i, line in enumerate(lines) if start - 1 <= i < start - 1 + count)
                result = {"content": self.mask(text[:24000]), "total_lines": len(lines), "truncated": len(text) > 24000 or start - 1 + count < len(lines)}
            else:
                root = self.root(args)
                found = []
                query = args.get("query", "")
                for parent, directories, files in os.walk(root):
                    directories[:] = sorted(d for d in directories if d not in {"node_modules", ".venv", "__pycache__", "build", "dist"}
                        and not sensitive(str((Path(parent) / d).relative_to(root)).replace("\\", "/"))
                        and (Path(parent) / d).resolve() == (Path(parent) / d).absolute())
                    for filename in sorted(files):
                        path = Path(parent) / filename
                        relative = path.relative_to(root).as_posix()
                        if sensitive(relative) or path.resolve() != path.absolute():
                            continue
                        if name == "workspace_list":
                            found.append(relative)
                        elif path.stat().st_size <= 1024 * 1024:
                            try:
                                content = path.read_text(encoding="utf-8")
                                found.extend(f"{relative}:{i}: {line}" for i, line in enumerate(content.splitlines(), 1) if query in line)
                            except (UnicodeError, OSError):
                                pass
                        if len(found) >= 500:
                            break
                    if len(found) >= 500:
                        break
                text = "\n".join(found)
                result = {"content": self.mask(text[:24000]), "truncated": len(text) > 24000 or len(found) >= 500}
            return Observation(action, {**result, "untrusted": True})
        except (ValueError, KeyError, OSError, UnicodeError):
            return Observation(action, {"error": "workspace_read_failed", "message": "仓库、路径、编码或读取范围检查未通过。"})

    def edit(self, args):
        path = self.path(args, local_only=args.get("local_only", False))
        before = path.read_bytes() if path.exists() else None
        if before is not None and (len(before) > 1024 * 1024 or b"\0" in before):
            raise ValueError("Unsupported file")
        text = before.decode("utf-8") if before is not None else ""
        mode = args.get("mode", "replace")
        if mode == "create":
            if before is not None:
                raise ValueError("File already exists")
            after = args["new_text"]
        elif mode == "replace":
            old = args.get("old_text", "")
            if before is None or not old or text.count(old) != 1:
                raise ValueError("Replacement must match exactly once")
            after = text.replace(old, args["new_text"], 1)
        elif mode == "overwrite":
            if before is None:
                raise ValueError("File does not exist")
            after = args["new_text"]
        else:
            raise ValueError("Invalid edit mode")
        if len(after.encode()) > 1024 * 1024:
            raise ValueError("File too large")
        return path, before, after

    async def preview(self, action_name, args):
        try:
            self.root(args)
            if action_name in {"workspace_edit", "workspace_delete"}:
                path, before, after = self.edit(args if action_name == "workspace_edit" else {**args, "mode": "overwrite", "new_text": ""})
                if action_name == "workspace_edit":
                    self.security.run("before_write", [(args["path"], after.encode())], local_only=args.get("local_only", False))
                patch = "".join(difflib.unified_diff((before or b"").decode().splitlines(True), after.splitlines(True), fromfile=args["path"], tofile=args["path"]))
                if len(patch) > 24000:
                    raise ValueError("Diff too large")
                state = (str(path), before, after)
                description = json.dumps({"操作": "删除项目文件" if action_name == "workspace_delete" else "修改项目文件", "仓库": args["repository"], "文件": args["path"], "差异": self.mask(patch),
                    "仅本地保存": args.get("local_only", False)}, ensure_ascii=False, indent=2)
            else:
                argv = args["argv"]
                if not argv or len(argv) > 100 or any(not isinstance(x, str) or not x or "\0" in x or len(x) > 4000 for x in argv):
                    raise ValueError("Invalid command")
                state = None
                description = json.dumps({"操作": "运行项目命令", "目录": args["repository"], "参数数组": argv,
                    "超时秒数": args.get("timeout_seconds", 120), "说明": "进程以当前用户运行，无操作系统沙箱；不会继承模型或 GitHub 密钥环境变量。"}, ensure_ascii=False, indent=2)
            self.prepared[request_key(action_name, args)] = (time.monotonic(), state)
            return description
        except SecurityViolation as error:
            raise ToolPreparationError(error.code, error.message, findings=error.result()["findings"]) from None
        except (ValueError, KeyError, OSError, UnicodeError):
            raise ToolPreparationError("workspace_preparation_failed", "无法生成准确的文件差异或命令预览。") from None

    async def apply(self, action):
        item = self.prepared.pop(request_key(action.name, action.arguments), None)
        if not item or time.monotonic() - item[0] > 600 or not self.mcp.authorization.active:
            return Observation(action, {"error": "workspace_not_prepared"})
        try:
            if action.name == "workspace_run":
                return Observation(action, await self.run(action.arguments))
            checked_path = self.path(action.arguments, local_only=action.arguments.get("local_only", False))
            checked_before = checked_path.read_bytes() if checked_path.exists() else None
            if item[1][:2] != (str(checked_path), checked_before):
                return Observation(action, {"error": "workspace_file_changed"})
            if action.name == "workspace_delete":
                checked_path.unlink()
                return Observation(action, {"success": True, "deleted": str(checked_path)})
            path, before, after = self.edit(action.arguments)
            if item[1] != (str(path), before, after):
                return Observation(action, {"error": "workspace_file_changed"})
            self.security.run("before_write", [(action.arguments["path"], after.encode())], local_only=action.arguments.get("local_only", False))
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
                    temporary = Path(file.name)
                    file.write(after.encode("utf-8"))
                if path.exists():
                    temporary.chmod(path.stat().st_mode)
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return Observation(action, {"success": True, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        except SecurityViolation as error:
            return Observation(action, error.result())
        except (ValueError, KeyError, OSError, UnicodeError):
            return Observation(action, {"error": "workspace_operation_failed"})

    async def run(self, args):
        root = self.root(args)
        audit = WorkspaceAudit(root, self.security, protected=(self.env_file,
            self.env_file.parent / ".local/config.json", self.env_file.parent / ".local/mcp.json",
            self.env_file.parent / ".local/github_account.json"))
        baseline = audit.capture()
        env = {key: value for key, value in os.environ.items() if key.upper() in
            {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"}}
        env.update(PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        process = await asyncio.create_subprocess_exec(*args["argv"], cwd=root, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}))
        async def stop():
            if process.returncode is None:
                if os.name == "nt":
                    executable = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe"
                    child = await asyncio.create_subprocess_exec(str(executable), "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                    await child.wait()
                    if process.returncode is None:
                        process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        output, truncated, timed_out = bytearray(), False, False
        async def collect():
            nonlocal truncated
            while data := await process.stdout.read(8192):
                output.extend(data)
                if len(output) > 2 * 1024 * 1024:
                    truncated = True
                    await stop()
                    break
            await process.wait()
        try:
            await asyncio.wait_for(collect(), args.get("timeout_seconds", 120))
        except TimeoutError:
            timed_out = True
            await stop()
        except asyncio.CancelledError:
            await stop()
            raise
        content = self.mask(output.decode("utf-8", errors="replace"))
        return {"exit_code": process.returncode, "timed_out": timed_out, "content": content[:24000],
                "truncated": truncated or len(content) > 24000, "success": process.returncode == 0 and not timed_out, "untrusted": True,
                "security_review": audit.review(baseline)}

    def registrations(self):
        common = {"repository": {"type": "string"}}
        string = {"type": "string"}
        definitions = {
            "workspace_list": ({}, [], False),
            "workspace_read": ({"path": string, "start_line": {"type": "integer", "minimum": 1}, "line_count": {"type": "integer", "minimum": 1, "maximum": 500}}, ["path"], False),
            "workspace_search": ({"query": {"type": "string", "minLength": 1}}, ["query"], False),
            "workspace_edit": ({"path": string, "mode": {"type": "string", "enum": ["create", "replace", "overwrite"]}, "old_text": string, "new_text": string,
                "local_only": {"type": "boolean", "description": "仅用于用户明确要求只在本机保存的运行配置；不会获得 Git 发布权限，应用自身凭据仍受保护。"}}, ["path", "new_text"], True),
            "workspace_delete": ({"path": string}, ["path"], True),
            "workspace_run": ({"argv": {"type": "array", "items": string, "minItems": 1, "maxItems": 100}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600}}, ["argv"], True),
        }
        registry = {}
        if not self.mcp.settings.git_enabled:
            return registry
        for name, (fields, required, write) in definitions.items():
            if write and not self.mcp.settings.workspace_write_enabled:
                continue
            async def preview(args, name=name):
                return await self.preview(name, args)
            tool = {"type": "function", "function": {"name": name, "description": {
                "workspace_list": "列出项目文件，排除敏感路径和常见依赖目录。", "workspace_read": "按行读取项目文本文件。",
                "workspace_search": "在项目文件内搜索文本。", "workspace_edit": "创建或精确替换项目文件；完整差异须经用户确认。",
                "workspace_delete": "删除明确的单个项目文件，展示完整删除差异后确认，不递归删除目录。",
                "workspace_run": "运行项目测试或构建命令；使用 argv 数组，无隐式 shell，执行前须确认。"}[name],
                "parameters": {"type": "object", "properties": {**common, **fields}, "required": ["repository", *required], "additionalProperties": False}}}
            registry[name] = ToolRegistration(tool, "workspace.write" if write else "workspace.read", self.apply if write else self.read, preview if write else None)
        self.mcp.authorization.grant("workspace.read", uses=60)
        return registry
