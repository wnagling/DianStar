# 提示词加载器：读取随 Python 包分发的通用助手系统提示词。

from pathlib import Path


ASSISTANT_INSTRUCTIONS = Path(__file__).with_name("system_prompt.md").read_text(encoding="utf-8").strip()
