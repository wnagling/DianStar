# 加载固定行为规则，并在每轮提问开始时注入本机当天日期。

from datetime import date, datetime
from pathlib import Path


ASSISTANT_INSTRUCTIONS = Path(__file__).with_name("system_prompt.md").read_text(encoding="utf-8").strip()


def build_system_prompt(current_date: date | None = None) -> str:
    """Build one turn's prompt; reuse its date throughout that turn's tool loop."""
    today = current_date if current_date is not None else datetime.now().astimezone().date()
    context = (
        "# 本轮运行上下文\n\n"
        f"当前本地日期：{today.isoformat()}。\n"
        "此日期由程序在本轮提问开始时按本机时区提供，精确到天；"
        "日期变化后，下一轮提问会重新获取。\n"
        "涉及其他地区的相对日期时，遵循用户指定时区及工具的日期规则，"
        "不要把本机日期当作目的地日期，也不要根据本机日期推断用户位置。"
    )
    return f"{ASSISTANT_INSTRUCTIONS}\n\n{context}"
