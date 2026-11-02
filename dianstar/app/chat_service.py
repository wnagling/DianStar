"""Execute and persist a successful turn independently of its user interface."""

from dianstar.app.runtime import run_turn
from dianstar.sessions.tool_activity import ToolActivity


async def ask_and_save(question, conversation=None, *, runner=None, **options):
    """Persist only completed turns; keep tool messages and per-turn reasoning."""
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
    answer = await (runner or run_turn)(question, history=history, **options)
    if conversation is not None:
        tools.interrupt()
        conversation.add_exchange(
            question, answer, reasoning_content="\n\n".join(parts) if parts else None,
            model_turn_messages=history[history_length:] or None,
            tool_activity=tools.snapshot() or None,
        )
    return answer
