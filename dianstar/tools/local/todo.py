"""Planning tools are metadata only; execution remains behind ToolGateway."""
from dianstar.core.registry import ToolRegistration
from dianstar.core.types import Observation


def registrations(plan):
    if not plan.conversation_id:
        return {}
    step = {"type": "object", "properties": {
        "id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,24}$"},
        "title": {"type": "string", "minLength": 1, "maxLength": 100},
        "status": {"type": "string", "enum": ["pending", "in_progress", "pending_review", "completed", "blocked", "waiting_user"]},
        "evidence": {"type": "array", "items": {"type": "string", "maxLength": 128}, "maxItems": 10}},
        "required": ["id", "title", "status", "evidence"], "additionalProperties": False}
    write = {"type": "function", "function": {"name": "todo_write",
        "description": "为确需持续跟踪的复杂多阶段任务创建或更新待办，右侧实时显示并按会话保存。普通问答、简单操作和小程序加测试无需创建；不因建项目或测试字样强行规划。先todo_read取revision，新建用0。每次提交完整列表；步骤完成需引用本轮已成功工具的call_id，无证据用pending_review。待办不授权执行。",
        "parameters": {"type": "object", "properties": {
            "revision": {"type": "integer", "minimum": 0},
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "steps": {"type": "array", "items": step, "minItems": 2, "maxItems": 12}},
            "required": ["revision", "title", "steps"], "additionalProperties": False}}}
    read = {"type": "function", "function": {"name": "todo_read", "description": "读取当前会话的持久待办及版本；历史进度需重新核对，不恢复授权或自动重做。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    async def execute(action):
        result = await plan.write(action.arguments) if action.name == "todo_write" else {"plan": await plan.load()}
        return Observation(action, result)
    return {schema["function"]["name"]: ToolRegistration(schema, "task.progress", execute) for schema in (write, read)}
