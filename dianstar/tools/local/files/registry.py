"""Schemas and shared handlers for the model-facing file tool set."""
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation
from dianstar.security.content_guard import SecurityViolation
from dianstar.security.tool_policy import ToolPolicy
from dianstar.security.authorization import request_key
from .read import FileReader
from .text import FileError
from .write import FileWriter
from .io import blocking

FILE_TOOL_NAMES = frozenset({"read", "glob", "grep", "edit", "write", "patch", "move", "workspace_delete", "file_history", "file_restore", "file_batch"})


def migrate_file_policies(policies):
    result = dict(policies or {})
    for old, names in {"workspace_read": ("read",), "workspace_list": ("read", "glob"),
                       "workspace_search": ("grep",), "workspace_edit": ("edit", "write")}.items():
        if old not in result:
            continue
        if not isinstance(result[old], ToolPolicy):
            raise ValueError("Invalid backend file policy")
        for name in names:
            previous = result.get(name)
            legacy = result[old]
            result[name] = legacy if previous is None else ToolPolicy(
                enabled=legacy.enabled and previous.enabled,
                requires_approval=legacy.requires_approval or previous.requires_approval)
    return result


class FileTools:
    def __init__(self, context):
        self.context, self.reads = context, {}
        self.reader = FileReader(context, self.reads)
        self.writer = FileWriter(context, self.reads)

    async def observe(self, action):
        try:
            if action.name == "read":
                result = await blocking(lambda cancel: self.reader.read(action.arguments))
            elif action.name == "glob":
                result = await blocking(lambda cancel: self.reader.glob(action.arguments))
            elif action.name == "file_history":
                result = await self.writer.history(action.arguments)
            else:
                result = await self.reader.grep(action.arguments)
        except (FileError, SecurityViolation) as error:
            result = error.result()
        except Exception:
            result = {"error": "file_read_failed", "message": "文件范围、路径、忽略规则或读取检查未完成；不代表没有匹配。"}
        return Observation(action, {**result, "untrusted": True})

    async def mutate(self, action):
        return Observation(action, await self.writer.apply(action))

    def registrations(self):
        if not self.context.projects.settings.enabled:
            return {}
        string = {"type": "string"}
        path = {"type": "string", "minLength": 1, "description": "相对 repository 的项目内路径，使用 / 分隔。"}
        page = {"limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "minimum": 0, "maximum": 10000}, "hidden": {"type": "boolean"}}
        local = {"type": "boolean", "description": "仅限用户明确要求只在本机保存的运行配置；不获得 Git 发布权限，也不能复制应用凭据。"}
        definitions = {
            "file_history": ({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, [],
                "查看当前项目的本地文件修改和恢复记录，只返回文件范围、编号和状态，不返回备份内容。"),
            "file_restore": ({"transaction_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}, "keep_current": {"type": "boolean"}}, ["transaction_id"],
                "按编号撤销已完成修改或继续中断恢复，差异确认后执行；文件有其他变化则拒绝。keep_current=true 用于用户确认保留未完成或冲突的当前文件并结束该恢复记录，副本仍保留。"),
            "read": ({"path": path, "offset": {"type": "integer", "minimum": 1}, "limit": page["limit"], "hidden": page["hidden"]}, ["path"],
                     "读取项目 UTF-8 文件，返回行号和分页；path 为目录或 . 时列出目录。现有文件修改前先 read，保护敏感路径及内容。"),
            "glob": ({"path": path, "pattern": {"type": "string", "minLength": 1, "maxLength": 500}, **page}, ["pattern"],
                     "按项目内路径模式找文件，如 **/*.py；尊重忽略规则，支持目录、分页和隐藏文件选项。"),
            "grep": ({"path": path, "pattern": {"type": "string", "minLength": 1, "maxLength": 1000}, "include": string,
                      "literal": {"type": "boolean"}, "case_sensitive": {"type": "boolean"}, **page}, ["pattern"],
                     "搜索项目文件内容，返回路径、行号和片段；默认正则，可用 literal=true 搜索字面文本，可限定 include 文件模式。"),
            "edit": ({"path": path, "old_string": {"type": "string", "minLength": 1}, "new_string": string,
                      "replace_all": {"type": "boolean"}, "local_only": local}, ["path", "old_string", "new_string"],
                     "精确修改已 read 的文件，默认要求旧文本唯一匹配；完整差异经确认并复查，保留 UTF-8 BOM 和换行。"),
            "write": ({"path": path, "content": string, "local_only": local}, ["path", "content"],
                      "新建或完整写入 UTF-8 文件，自动创建父目录；已有文件须先 read。完整差异经确认，局部修改优先用 edit。"),
            "workspace_delete": ({"path": path}, ["path"], "删除明确的单个项目文件，预览完整差异后确认，不递归删除目录。"),
            "move": ({"source": path, "destination": path}, ["source", "destination"],
                "在同一项目内移动或重命名单个已 read 的 UTF-8 文件；目标必须不存在，自动创建父目录。保留原始字节和文件模式，预览后确认，失败补偿，可 file_restore 撤销；不处理目录、跨项目或仅改大小写。"),
            "patch": ({"patch": {"type": "string", "minLength": 1, "maxLength": 262144}}, ["patch"],
                "以一项可恢复事务应用多文件文本补丁，最多20个源/目标路径。格式：*** Begin Patch，*** Add File: path（正文每行+开头），*** Update File: path（可跟*** Move to: new_path），修改段以独立@@行开始，上下文行以空格、删除行以-、新增行以+开头；*** Delete File: path；最后*** End Patch。End of File 标记可限定末尾。不是标准 unified diff，不接受数字行号或模糊匹配。已有文件须先 read，所有段完整预检后一次确认，任一失败补偿；无 local_only 绕过。"),
        }
        operation_schemas = []
        for operation in ("edit", "write", "delete"):
            fields, required, _ = definitions["workspace_delete" if operation == "delete" else operation]
            operation_schemas.append({"type": "object", "properties": {"operation": {"type": "string", "enum": [operation]}, **fields},
                "required": ["operation", *required], "additionalProperties": False})
        definitions["file_batch"] = ({"operations": {"type": "array", "items": {"oneOf": operation_schemas}, "minItems": 1, "maxItems": 20}}, ["operations"],
            "将有关联的 edit/write/delete 合为一次差异确认，失败时按持久记录恢复本批修改；已有文件仍须先 read。每路径一次，最多20个文件，外部变化时保留现场报告恢复冲突。")
        result = {}
        for name, (properties, required, description) in definitions.items():
            mutation = name in {"edit", "write", "patch", "move", "workspace_delete", "file_batch", "file_restore"}
            if mutation and not self.context.projects.settings.write_enabled:
                continue
            async def preview(args, name=name):
                return await self.writer.preview(name, args)
            def automatic(args, name=name):
                scope = self.context.task_scope
                if scope is None or name not in {"write", "file_batch"}:
                    return False
                with self.writer.prepared_lock:
                    item = self.writer.prepared.get(request_key(name, args))
                    return bool(item and scope.new_files(self.context.root(args), item[1]["entries"]))
            schema = {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": {"repository": {"type": "string", "minLength": 1}, **properties},
                               "required": ["repository", *required], "additionalProperties": False}}}
            result[name] = ToolRegistration(schema, "workspace.write" if mutation else "workspace.read",
                self.mutate if mutation else self.observe, preview if mutation else None,
                required_tools=(("edit", "write", "workspace_delete") if name in {"file_batch", "file_restore", "patch"} else
                                ("write", "workspace_delete") if name == "move" else ()),
                parallel_safe=not mutation, effects=("project.files",) if mutation else ())
            if name in {"write", "file_batch"}:
                from dataclasses import replace
                result[name] = replace(result[name], auto_approve=automatic)
        self.context.projects.authorization.grant("workspace.read", uses=60)
        return result
