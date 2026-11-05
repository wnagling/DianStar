"""Explicit command execution, separate from file tools."""
import json
import os
import re
import shlex
import time
from pathlib import Path
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.security.authorization import request_key
from dianstar.security.command_policy import command_plan, ExecutableUnavailable
from dianstar.security.content_guard import SecurityViolation
from dianstar.security.gateway import ToolPreparationError
from dianstar.hooks.workspace import WorkspaceAudit
from dianstar.platform.git_queries import SafeGitQueries, GitError
from dianstar.platform.shells import find_bash, find_powershell, PowerShellUnavailable
from dianstar.platform.powershell import powershell_argv, command_from_argv
from dianstar.platform.command_environment import command_environment, environment_schema, profile_environment, BUILD_VARIABLES
from dianstar.sessions.environments import EnvironmentProfileError
from dianstar.platform.toolchains import routine_version
from .files.io import blocking
from .process_runner import run_process


class CommandTools:
    def __init__(self, context):
        self.context, self.prepared = context, {}
        self.audits = {}

    def prepare_command(self, argv, root, *, environment=None):
        self.context.security.run("before_command", [("command", json.dumps(argv, ensure_ascii=False).encode())])
        if os.name == 'nt' and argv[0].casefold() in {'pwsh','pwsh.exe','powershell','powershell.exe'}:
            argv=[str(find_powershell()),*argv[1:]]
        plan = command_plan(argv, root, environment=environment)
        if os.name == "nt" and Path(plan["argv"][0]).name.casefold() in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            try:
                command = command_from_argv(plan["argv"])
            except ValueError:
                raise ToolPreparationError("powershell_arguments_unsupported",
                    "未执行：仅支持已安装 PowerShell 7 的 -Command 单一脚本文本，不支持 -File 文件启动、交互、编码命令或其他运行时选项；不会关闭系统策略或隔离。") from None
            self.context.security.run("before_command", [("powershell", command.encode())])
            plan = command_plan(powershell_argv(command, root), root, environment=environment)
            plan.update(shell="powershell", execution_mode="shell", command=command)
        if plan["validation_kind"] == "git_diff_check":
            # Reuse the Git tool's configuration and executable restrictions.
            try:
                workspace = SafeGitQueries([root])
                workspace.git = plan["argv"][0]
                context = workspace._context(str(root))
            except (GitError, ValueError, OSError):
                raise ValueError("Git validation configuration unavailable") from None
            plan["argv"] = [*context[2], "diff", "--no-ext-diff", "--no-textconv", "--no-color", *argv[2:]]
            plan["environment"] = {**(environment or {}), **context[1]}
            if environment is not None and "PATH" in environment:
                plan["environment"]["PATH"] = environment["PATH"]
        return plan

    def prepare_bash(self, args, root, *, environment=None):
        if os.name == "nt":
            raise ToolPreparationError("shell_incompatible_with_isolation", "Windows 的 Git Bash 与强隔离不兼容；请使用 shell 工具的 PowerShell，或用 workspace_run 直接执行程序。")
        command = args["command"]
        if "\0" in command or not command.strip():
            raise ValueError("Invalid Bash command")
        self.context.security.run("before_command", [("bash", command.encode())])
        # Only plain, non-expanding commands can reuse argv validation. Bind
        # those to the checked executable rather than re-resolving in Bash.
        if not re.search(r"[\r\n\0;&|<>$`(){}\[\]*?!~#\\]", command):
            try:
                argv = shlex.split(command, posix=True)
                if argv:
                    direct = self.prepare_command(argv, root, environment=environment)
                    if direct["routine_validation"] or routine_version(direct["argv"], root) or (
                            self.context.task_scope is not None and self.context.task_scope.go_command(direct["argv"], root)):
                        return {**direct, "shell": "bash", "execution_mode": "direct_validation" if direct["routine_validation"] else "direct_command", "command": command}
            except SecurityViolation:
                raise
            except ValueError:
                pass
        try:
            executable = find_bash()
        except ValueError:
            raise ToolPreparationError("bash_unavailable", "未找到可用 Bash。Windows 需要 Git Bash；本机程序仍可通过 workspace_run 执行。") from None
        return {**command_plan([str(executable), "--noprofile", "--norc", "-o", "pipefail", "-c", command], root, environment=environment),
                "shell": "bash", "execution_mode": "shell", "command": command}

    def prepare_shell(self, args, root, *, environment=None):
        command = args["command"]
        if "\0" in command or not command.strip():
            raise ValueError("Invalid PowerShell command")
        self.context.security.run("before_command", [("powershell", command.encode())])
        if not re.search(r"[\r\n;&|<>$`(){}\[\]*?!~#]", command):
            try:
                argv = [item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'" else item
                    for item in shlex.split(command, posix=False)]
                direct = self.prepare_command(argv, root, environment=environment)
                if direct["routine_validation"] or routine_version(direct["argv"], root) or (
                        self.context.task_scope is not None and self.context.task_scope.go_command(direct["argv"], root)):
                    return {**direct, "shell": "powershell", "execution_mode": "direct_validation" if direct["routine_validation"] else "direct_command", "command": command}
            except SecurityViolation:
                raise
            except ValueError:
                pass
        return {**command_plan(powershell_argv(command, root), root, environment=environment),
                "shell": "powershell", "execution_mode": "shell", "command": command}

    def prepare(self, name, args):
        self.context.projects.require(write=True)
        root = self.context.root(args)
        overrides = args.get("environment", {})
        environment_root = self.context.storage.manager.scratch_projects.get(str(root), root)
        profile = self.context.environments.current(environment_root)
        environment = profile_environment(self.context.security.scanner, profile, overrides)
        self.context.security.run("before_command", [("command-environment.cfg",
            "\n".join(f"{key}={value}" for key, value in overrides.items()).encode())])
        plan = (self.prepare_shell(args, root, environment=environment) if name == "shell" else
                self.prepare_bash(args, root, environment=environment) if name == "bash" else
                self.prepare_command(args["argv"], root, environment=environment))
        plan.setdefault("environment", environment)
        plan["network"] = args.get("network", False)
        plan["project_environment_revision"] = profile["revision"]
        plan["project_environment_configured"] = profile["configured"]
        plan["routine_inspection"] = routine_version(plan["argv"], root)
        plan["requested_task_command"] = bool(self.context.task_scope is not None
            and self.context.task_scope.go_command(plan["argv"], root))
        if plan["routine_inspection"] and Path(plan["argv"][0]).name.lower() in {"go", "go.exe"}:
            plan["environment"] = {**plan["environment"], "GOTOOLCHAIN": "local"}
        if overrides or profile["environment"] or profile["path_prepend"]:
            # Environment changes can alter module/compiler resolution even
            # for a familiar test command; do not silently waive its approval.
            plan["routine_validation"] = False
        if overrides:
            plan["routine_inspection"] = plan["requested_task_command"] = False
        return plan

    async def run(self, args, *, environment=None):
        root = self.context.root(args)
        audit = WorkspaceAudit(root, self.context.security, protected=(self.context.env_file,
            self.context.env_file.parent / ".local/config.json", self.context.env_file.parent / ".local/mcp.json",
            self.context.env_file.parent / ".local/github_account.json", self.context.env_file.parent / ".local/projects.json"))
        baseline = await blocking(lambda cancel: audit.capture())
        env = command_environment(self.context.security.scanner)
        if environment is not None:
            env = dict(environment)
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        result = await run_process(args["argv"], root, env, args.get("timeout_seconds", 120), protected=audit.protected,
            network=args.get("network", False), managed=str(root) in self.context.storage.manager.scratch_roots, scanner=self.context.security.scanner)
        output = result.pop("output")
        if "-OutputFormat" in args["argv"] and Path(args["argv"][0]).name.casefold() in {"powershell.exe","pwsh.exe"}:
            from dianstar.platform.powershell_output import decode_output
            content = self.context.mask(decode_output(output))
        else:
            content = self.context.mask(output.decode("utf-8", errors="replace"))
        return {**result, "content": content[:24000], "working_directory": str(root),
                "truncated": result["output_limit_exceeded"] or len(content) > 24000, "untrusted": True,
                "security_review": await blocking(lambda cancel: audit.review(baseline))}

    async def preview(self, args, name="workspace_run"):
        binding = request_key(name, args)
        self.prepared.pop(binding, None)
        self.audits.pop(binding, None)
        try:
            state = await blocking(lambda cancel: self.prepare(name, args))
            if getattr(self.context, "command_audit", None) is not None:
                audit = await self.context.command_audit(name, args, state)
                if audit is not None:
                    self.audits[binding] = audit
                    if audit.get("status") == "reviewed" and audit.get("decision") == "deny":
                        raise ToolPreparationError("command_audit_denied", "命令审计拒绝执行：" + audit.get("reason", "操作风险过高。"))
            self.prepared[request_key(name, args)] = (time.monotonic(), state)
            return json.dumps({"操作": "运行项目命令", "目录": args["repository"], "参数数组": state["argv"],
                "本次构建环境": args.get("environment", {}),
                "会话项目环境": {"已启用": state["project_environment_configured"], "版本": state["project_environment_revision"]},
                "环境作用范围": "本次覆盖优先，其次当前会话项目环境，最后宿主公开环境；不修改系统配置。",
                "超时秒数": args.get("timeout_seconds", 120), "允许互联网": args.get("network", False),
                "命令审计": self.audits.get(binding),
                "说明": "Windows 使用应用容器，项目及专用临时目录可写；既有凭据配置受保护，工具链只读，无提权或无限制后备。"}, ensure_ascii=False)
        except SecurityViolation as error:
            raise ToolPreparationError(error.code, error.message, findings=error.result()["findings"]) from None
        except EnvironmentProfileError as error:
            raise ToolPreparationError(error.code, error.message) from None
        except ExecutableUnavailable:
            raise ToolPreparationError("executable_not_found", "本次命令环境无法定位程序；不等于未安装，请检查绝对路径、PATH 或环境激活。") from None
        except PowerShellUnavailable:
            raise ToolPreparationError("powershell7_unavailable", "未找到独立安装的 PowerShell 7，请按 README 安装后重试；不会退回 5.1 或无限制执行。") from None
        except (ValueError, OSError):
            raise ToolPreparationError("workspace_preparation_failed", "命令或执行环境检查未通过。") from None

    async def apply(self, action):
        item = self.prepared.pop(request_key(action.name, action.arguments), None)
        if not item or time.monotonic() - item[0] > 600 or not self.context.projects.authorization.active:
            return Observation(action, {"error": "workspace_not_prepared"})
        try:
            async with self.context.files.exclusive_operation(self.context.root(action.arguments)):
                current = await blocking(lambda cancel: self.prepare(action.name, action.arguments))
                if current != item[1]:
                    return Observation(action, {"error": "workspace_command_changed"})
                result = await self.run({**action.arguments, "argv": item[1]["argv"]},
                    **({"environment": item[1]["environment"]} if "environment" in item[1] else {}))
                result["validation_kind"] = item[1].get("validation_kind")
                result["executable"] = item[1]["argv"][0]
                result["environment_scope"] = "conversation_project" if item[1]["project_environment_configured"] else "command"
                result["project_environment_revision"] = item[1]["project_environment_revision"]
                result["build_environment_names"] = sorted(key for key in item[1]["environment"] if key.upper() in BUILD_VARIABLES)
                result["environment_override_names"] = sorted(action.arguments.get("environment", {}))
                if item[1]["requested_task_command"]:
                    result["execution_kind"] = "go_" + item[1]["argv"][1]
                if "shell" in item[1]:
                    result.update(requested_shell=item[1]["shell"], shell=item[1]["shell"] if item[1]["execution_mode"] == "shell" else None,
                                  execution_mode=item[1]["execution_mode"])
                return Observation(action, result)
        except SecurityViolation as error:
            return Observation(action, error.result())
        except EnvironmentProfileError as error:
            return Observation(action, {"error": error.code, "message": error.message})
        except ToolPreparationError as error:
            return Observation(action, error.result)
        except ExecutableUnavailable:
            return Observation(action, {"error": "workspace_command_changed", "message": "确认后程序或环境已变化，未执行，请重新核对。"})
        except PowerShellUnavailable:
            return Observation(action, {"error":"powershell7_unavailable","message":"PowerShell 7 在确认后不可用，未执行；请恢复安装后重试。"})
        except (ValueError, OSError):
            return Observation(action, {"error": "workspace_operation_failed"})

    def registrations(self):
        if not self.context.projects.settings.enabled or not self.context.projects.settings.write_enabled:
            return {}
        schema = {"type": "function", "function": {"name": "workspace_run", "description":
            "在项目中执行任务范围内的命令；常规验证按后台策略免额外确认，单纯提交推送不附加测试。"
            "风险审计拒绝时不执行，需确认或未完成时必须用户审批；通过不能扩大原权限。"
            "Windows PowerShell 7的-Command单一脚本文本与shell统一处理目录和输出；仅支持NoLogo/NoProfile/NonInteractive和OutputFormat选项，不支持-File、交互或编码启动。",
            "parameters": {"type": "object", "properties": {"repository": {"type": "string"},
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600}, "environment": environment_schema(),
                "network": {"type": "boolean", "description": "默认不联网；仅确有下载/联网需求时请求，本次确认会说明互联网访问。不会扩大文件范围，不包含本机回环豁免。"}},
                "required": ["repository", "argv"], "additionalProperties": False}}}
        bash = {"type": "function", "function": {"name": "bash", "description":
            "在已接入项目中运行非交互 Bash 命令，Windows 使用 Git Bash；支持管道和条件执行，默认 pipefail，不加载用户启动配置。"
            "适合测试/构建/任务命令；文件读写优先专用工具。常规单条验证按后台策略免额外确认，复杂脚本需确认。"
            "风险审计拒绝时不执行，需确认或未完成时必须用户审批。"
            "每次启动新进程，cd/export/激活状态不跨调用保留；自动复用当前会话项目环境，environment 可仅覆盖本次。"
            "找不到程序先区分未安装与PATH/激活问题，不自行安装。Windows安装不使用Linux包管理命令。"
            "使用前台命令，不启动后台服务；最长600秒，输出有界，超时/取消清理进程树。隔离后端不可用时停止，不无限制执行；命令写入不提供文件工具自动回滚。",
            "parameters": {"type": "object", "properties": {"repository": {"type": "string", "minLength": 1},
                "command": {"type": "string", "minLength": 1, "maxLength": 16000},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600}, "environment": environment_schema(),
                "network": {"type": "boolean"}},
                "required": ["repository", "command"], "additionalProperties": False}}}
        result = {}
        shell = {"type": "function", "function": {"name": "shell", "description":
            "在 Windows 项目中运行受应用容器隔离的非交互 PowerShell 7 命令，请使用PowerShell 7语法和对应的.NET API。"
            "只允许写当前项目及专用临时目录，既有敏感配置和 Git 元数据禁止命令写入；默认不能联网。"
            "简单程序调用优先 workspace_run，文件读写优先专用工具。风险审计拒绝时不执行，需确认或未完成时必须用户审批；通过仍遵守原策略。没有无限制运行后备。",
            "parameters": bash["function"]["parameters"]}}
        for definition in ((schema, shell) if os.name == "nt" else (schema, bash)):
            name = definition["function"]["name"]
            async def preview(args, name=name):
                return await self.preview(args, name)
            def automatic(args, name=name):
                item = self.prepared.get(request_key(name, args))
                return bool(item and not args.get("network", False) and any(item[1].get(key) for key in ("routine_validation", "routine_inspection", "requested_task_command")))
            def force_approval(args, name=name):
                audit = self.audits.get(request_key(name, args))
                return bool(audit is not None and (audit.get("status") != "reviewed" or audit.get("decision") != "allow"))
            result[name] = ToolRegistration(definition, "workspace.write", self.apply, preview, auto_approve=automatic,
                force_approval=force_approval,
                required_tools=("workspace_run",) if name in {"bash", "shell"} else (), effects=("project.command",),
                policy_aliases=("bash",) if name == "shell" else ())
        return result
