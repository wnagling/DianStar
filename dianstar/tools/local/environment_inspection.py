"""One bounded native environment query instead of repeated shell probes."""
import json
import os
import shutil

from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.platform.command_environment import profile_environment
from dianstar.platform.toolchains import VERSION_ARGUMENTS, located
from dianstar.security.content_guard import SecurityViolation
from dianstar.security.system_paths import HostSafetyError
from dianstar.sessions.environments import EnvironmentProfileError
from dianstar.security.gateway import ToolPreparationError
from .files.io import blocking
from .process_runner import run_process


class EnvironmentInspection:
    def __init__(self, context):
        self.context = context

    async def inspect(self, action):
        try:
            root = self.context.root(action.arguments)
            async with self.context.files.exclusive_operation(root):
                return await self._inspect(action)
        except SecurityViolation as error:
            return Observation(action, error.result())
        except (ValueError, OSError):
            return Observation(action, {"error": "environment_inspection_failed", "message": "项目环境检查未完成，未绕过文件锁或恢复检查。"})

    async def _inspect(self, action):
        try:
            root = self.context.root(action.arguments)
            profile = await blocking(lambda cancel: self.context.environments.current(root))
            environment = profile_environment(self.context.security.scanner, profile, {})
            # Version discovery must not trigger Go's automatic toolchain download.
            environment["GOTOOLCHAIN"] = "local"
            async def program(name):
                checked, paths = await blocking(lambda cancel: located(name, environment, root))
                versions = []
                for path in paths:
                    self.context.security.run("before_command", [("command", json.dumps([str(path), *VERSION_ARGUMENTS[name]]).encode())])
                    try:
                        result = await run_process([str(path), *VERSION_ARGUMENTS[name]], root, environment, 4,
                            protected=(self.context.env_file,), managed=str(root) in self.context.storage.manager.scratch_roots, scanner=self.context.security.scanner)
                    except HostSafetyError as error:
                        versions.append({"executable": str(path), "bin_directory": str(path.parent),
                            "success": False, "version": "", "timed_out": False, **error.result()})
                        continue
                    versions.append({"executable": str(path), "bin_directory": str(path.parent),
                        "success": result["success"], "version": self.context.mask(result["output"].decode("utf-8", errors="replace"))[:2000],
                        "timed_out": result["timed_out"]})
                    if result["success"]:
                        break
                return {"program": name, "state": "available" if any(item["success"] for item in versions) else
                    "found_but_unusable" if paths else "not_found_in_checked_locations",
                    "checked_locations": checked, "versions": versions}
            # All probes share a project ACL lease. Keep that lease exclusive
            # across sessions/processes and settle each probe before the next.
            programs = [await program(name) for name in action.arguments["programs"]]
            managers = {name: path for name in ("scoop", "winget", "choco", "brew", "apt-get")
                if (path := shutil.which(name, path=environment.get("PATH", os.defpath)))}
            result = {"platform": "native_windows" if os.name == "nt" else "native_posix", "programs": programs,
                "package_managers": managers, "scope": "PATH 与有限常见安装位置，未扫描全盘、未检查 WSL、未安装软件。",
                "next_step": "可用时用返回的绝对程序路径继续；未发现时选择一个安装方案并确认，不继续枚举其他平台或反复扫描。",
                "untrusted": True}
        except SecurityViolation as error:
            result = error.result()
        except EnvironmentProfileError as error:
            result = {"error": error.code, "message": error.message}
        except (ValueError, OSError):
            result = {"error": "environment_inspection_failed", "message": "本机环境检查未完成，不代表工具未安装。"}
        return Observation(action, result)

    def registrations(self):
        if not self.context.mcp.settings.git_enabled or not self.context.mcp.settings.workspace_write_enabled:
            return {}
        schema = {"type": "function", "function": {"name": "workspace_environment_inspect", "description":
            "一次检查本机原生工具链的 PATH、有限常见位置与版本，并返回可见包管理器。环境诊断优先使用，避免多轮 shell 枚举；不检查 WSL、不安装软件。",
            "parameters": {"type": "object", "properties": {"repository": {"type": "string"},
                "programs": {"type": "array", "items": {"type": "string", "enum": list(VERSION_ARGUMENTS)},
                    "minItems": 1, "maxItems": 4, "uniqueItems": True}}, "required": ["repository", "programs"], "additionalProperties": False}}}
        def preview(args):
            try:
                root = self.context.root(args)
            except (ValueError, OSError):
                raise ToolPreparationError("environment_inspection_failed", "项目尚未接入，未检查本机程序。") from None
            return "检查项目所需的本机开发环境\n项目位置：" + str(root) + "\n工具：" + "、".join(args["programs"]) + "\n仅查询位置和版本，不安装软件。"
        return {"workspace_environment_inspect": ToolRegistration(schema, "workspace.write", self.inspect, preview,
            auto_approve=lambda args: True, required_tools=("workspace_run",))}
