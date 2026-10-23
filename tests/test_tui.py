"""终端界面的关键键盘交互和本地会话恢复。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from textual.containers import Vertical
from textual.widgets import Button, Input, Label, RichLog, Static

from dianstar.conversations import Conversation
from dianstar.rabbit_art import rabbit_lines
from dianstar.tui import DianStarApp
from dianstar.welcome_art import welcome_art_lines


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_welcome_flower_stays_separate_from_rabbit(self):
        rabbit = rabbit_lines(80, 12)
        art = welcome_art_lines(80, 20)
        self.assertEqual(len(art), 20)
        self.assertEqual(len(art[0].plain), 80)
        self.assertTrue(any(span.style == "#edb45e" for line in art for span in line.spans))
        self.assertTrue(any(span.style == "#d95646" for line in art for span in line.spans))
        rabbit_top = (len(art) - len(rabbit)) // 2
        for y, original in enumerate(rabbit):
            for x, glyph in enumerate(original.plain):
                if glyph != " ":
                    self.assertEqual(art[y + rabbit_top].plain[x], glyph)
        rabbit_ink = [(ry + rabbit_top, x) for ry, row in enumerate(rabbit)
                      for x, glyph in enumerate(row.plain) if glyph != " "]
        flower_ink = []
        for y, line in enumerate(art):
            for span in line.spans:
                if span.style not in {"#a93631", "#d95646", "#f08360", "#edb45e"}:
                    continue
                flower_ink.append((y, span.start))
                self.assertTrue(all(y != ry or abs(span.start - x) > 1
                                    for ry, x in rabbit_ink))
        left = min(x for _, x in rabbit_ink)
        right = max(x for _, x in rabbit_ink)
        top = min(y for y, _ in rabbit_ink)
        bottom = max(y for y, _ in rabbit_ink)
        self.assertTrue(any(x < left for _, x in flower_ink))
        self.assertTrue(any(x > right for _, x in flower_ink))
        self.assertTrue(any(y < top for y, _ in flower_ink))
        self.assertTrue(any(y > bottom for y, _ in flower_ink))
        self.assertTrue(any(12 <= y <= 15 and x >= 58 for y, x in flower_ink))
        self.assertGreater(max(x for y, x in flower_ink if y == 13),
                           max(x for y, x in flower_ink if y == 16))
        right_edge = {y: max(x for row, x in flower_ink if row == y)
                      for y in (15, 16, 17, 18, 19)}
        self.assertLess(right_edge[16], right_edge[17])
        self.assertLess(right_edge[17], right_edge[15])
        self.assertGreater(right_edge[17], right_edge[18])
        self.assertGreater(right_edge[18], right_edge[19])
        self.assertTrue(all((18, x) in flower_ink for x in range(42, 49)))
        # The narrow bridge on the flower's left must survive foreground masking.
        left_flower = {(y, x) for y, x in flower_ink if x < left}
        upper = next((point for point in left_flower if point[0] <= 3), None)
        self.assertIsNotNone(upper)
        reached = {upper}
        pending = [upper]
        while pending:
            y, x = pending.pop()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    neighbor = (y + dy, x + dx)
                    if neighbor in left_flower and neighbor not in reached:
                        reached.add(neighbor)
                        pending.append(neighbor)
        self.assertTrue(any(y >= 10 for y, _ in reached))
        compact = welcome_art_lines(32, 30)
        self.assertEqual(len(compact), 10)
        self.assertTrue(any(span.style == "#edb45e" for line in compact for span in line.spans))
        self.assertEqual(welcome_art_lines(20, 7), rabbit_lines(20, 7))

    async def test_answer_is_visible_before_request_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            release = asyncio.Event()

            async def answer_later(question, *, history, on_stream, on_thinking):
                on_thinking("第一步\n第二步\n第三步\n第四步\n第五步\n再组织回答。")
                on_stream("第一段")
                await release.wait()
                on_stream("第一段第二段")
                return "第一段第二段"

            with patch("dianstar.tui.ask_deepseek", side_effect=answer_later):
                async with app.run_test() as pilot:
                    app.query_one("#input", Input).value = "测试流式输出"
                    await pilot.press("enter")
                    await pilot.pause()
                    live = app.query_one("#stream", Static)
                    thinking = app.query_one("#thinking", RichLog)
                    self.assertTrue(live.display)
                    self.assertGreater(live.size.height, 0)
                    self.assertTrue(thinking.display)
                    self.assertEqual(thinking.size.height, 4)
                    self.assertIn("再组织回答", "\n".join(line.text for line in thinking.lines))
                    self.assertGreater(thinking.scroll_y, 0)
                    rendered = "".join(segment.text for segment in app.console.render(live.visual._renderable))
                    self.assertIn("第一段", rendered)
                    release.set()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertFalse(live.display)
                    self.assertFalse(thinking.display)
                    self.assertIn("第一段第二段", "\n".join(
                        line.text for line in app.query_one("#chat", RichLog).lines))

    async def test_sidebar_can_be_collapsed_and_reopened(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            async with app.run_test() as pilot:
                sidebar = app.query_one("#sessions", Vertical)
                self.assertFalse(sidebar.display)
                await pilot.click("#toggle-sessions")
                self.assertTrue(sidebar.display)
                self.assertEqual(str(app.query_one("#toggle-sessions", Button).label), "收起会话")
                await pilot.press("ctrl+l")
                self.assertFalse(sidebar.display)
                await pilot.press("ctrl+l")
                self.assertTrue(sidebar.display)
                await pilot.press("ctrl+l")
                self.assertFalse(sidebar.display)
                app.query_one("#input", Input).value = "/switch"
                await pilot.press("enter")
                self.assertTrue(sidebar.display)

    async def test_tab_completes_command_and_help_lists_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            async with app.run_test() as pilot:
                await pilot.press("/", "s", "w", "tab")
                self.assertEqual(app.query_one("#input", Input).value, "/switch")
                app.query_one("#input", Input).value = "/help"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("/help", str(app.query_one("#chat", RichLog).lines))

    async def test_switch_restores_visible_history_and_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            previous = Conversation.create(directory)
            previous.add_exchange("北京天气如何", "天气不错")
            current = Conversation.create(directory)
            app = DianStarApp(current)
            async with app.run_test() as pilot:
                app.query_one("#input", Input).value = "/switch"
                await pilot.press("enter")
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertEqual(app.conversation.id, previous.id)
                self.assertEqual(str(app.query_one("#session-name", Label).render()), "北京天气如何"[:6])
                self.assertIn("天气不错", str(app.query_one("#chat", RichLog).lines))

    async def test_question_is_saved_and_named(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            app = DianStarApp(conversation)
            with patch("dianstar.tui.ask_deepseek", new_callable=AsyncMock, return_value="你好！"):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    self.assertGreater(len(app.query_one("#chat", RichLog).lines), 5)
                    self.assertGreater(len(rabbit_lines(80, 20)), 14)
                    await pilot.press("你", "好", "enter")
                    await pilot.pause()
                    self.assertIn("⣿", "\n".join(line.text for line in app.query_one("#chat", RichLog).lines))
            restored = Conversation.load(conversation.id, Path(temporary))
            self.assertEqual(restored.messages[-1], {"role": "assistant", "content": "你好！"})
            self.assertEqual(restored.name, "你好")


if __name__ == "__main__":
    unittest.main()
