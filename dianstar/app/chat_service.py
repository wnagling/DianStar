"""Persist completed turns and explicit model-request interruptions."""

from dianstar.app.runtime import run_turn
from dianstar.sessions.tool_activity import ToolActivity
from openai import OpenAIError


INTERRUPTED = "模型请求失败，本轮已中断；已执行的操作记录已保存。输入 /retry 继续，先核对现状，不重复执行已完成操作。"


async def ask_and_save(question, conversation=None, *, runner=None, **options):
    """Keep tool evidence for completed turns and recoverable model failures."""
    history = conversation.model_context_messages() if conversation is not None else None
    history_length = len(history) if history is not None else 0
    callback = options.get("on_thinking")
    parts = []
    new_part = True
    tools = ToolActivity()
    tool_callback = options.get("on_tool")
    def tool_event(event):
        if event.get("conversation_id") is not None and conversation is not None and event["conversation_id"] != conversation.id:
            return
        tools.consume(event)
        if tool_callback is not None:
            tool_callback(event)

    def thinking(content):
        nonlocal new_part
        if not content:
            new_part = True
        elif new_part:
            parts.append(content)
            new_part = False
        else:
            parts[-1] = content
        callback(content)

    if callback is not None:
        options["on_thinking"] = thinking
    if runner is None or runner is run_turn or tool_callback is not None:
        options["on_tool"] = tool_event
    if conversation is not None and (runner is None or runner is run_turn):
        options.setdefault("conversation_id", conversation.id)
    try:
        answer = await (runner or run_turn)(question, history=history, **options)
    except (OpenAIError, TimeoutError, ConnectionError):
        if conversation is not None:
            tools.interrupt()
            conversation.add_exchange(question, INTERRUPTED,
                model_turn_messages=history[history_length:] or None,
                tool_activity=tools.snapshot() or None, interrupted=True)
        raise
    if conversation is not None:
        tools.interrupt()
        conversation.add_exchange(
            question, answer, reasoning_content="\n\n".join(parts) if parts else None,
            model_turn_messages=history[history_length:] or None,
            tool_activity=tools.snapshot() or None,
        )
    return answer
