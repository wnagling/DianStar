# 测试命令行回答的 Markdown 展示，不修改会话中保存的原文。

import io
import unittest

from rich.console import Console

from dianstar.ui.cli import _print_answer


class DisplayTests(unittest.TestCase):
    def test_markdown_bold_is_rendered_without_literal_asterisks(self):
        stream = io.StringIO()
        answer = "北京明天 **15℃**。"
        _print_answer(answer, interactive=True,
                      console=Console(file=stream, force_terminal=False, width=80))
        self.assertEqual(stream.getvalue(), "DianStar> 北京明天 15℃。\n")
        self.assertEqual(answer, "北京明天 **15℃**。")
