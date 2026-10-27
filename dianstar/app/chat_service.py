"""Execute and persist a successful turn independently of its user interface."""

from dianstar.app.runtime import run_turn


async def ask_and_save(question, conversation=None, *, runner=None, **options):
    """Persist only completed turns; keep tool messages and per-turn reasoning."""
    history = conversation.model_context_messages() if conversation is not None else None
    history_length = len(history) if history is not None else 0
    callback = options.get("on_thinking")
    parts = []
    new_part = True

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
    answer = await (runner or run_turn)(question, history=history, **options)
    if conversation is not None:
        conversation.add_exchange(
            question, answer, reasoning_content="\n\n".join(parts) if parts else None,
            model_turn_messages=history[history_length:] or None,
        )
    return answer
