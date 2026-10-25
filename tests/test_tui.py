"""终端界面的关键键盘交互和本地会话恢复。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from textual.containers import Vertical
from textual.events import Click, Paste
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Button, Label, RichLog, Static

from dianstar.conversations import Conversation
from dianstar.rabbit_art import rabbit_lines
from dianstar.tui import ChatInput, ChatLog, CopyMenu, DianStarApp
from dianstar.welcome_art import welcome_art_lines


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_double_click_and_ctrl_a_select_only_multiline_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            conversation.messages = [{"role": "user", "content": "聊天记录不应被选中"}]
            app = DianStarApp(conversation)
            async with app.run_test() as pilot:
                await pilot.pause()
                field = app.query_one("#input", ChatInput)
                draft = "第一行草稿\n第二行 hello"
                field.text = draft
                await pilot.pause()
                await pilot.double_click(field, offset=(3, 0))
                self.assertEqual(field.selected_text, draft)
                self.assertEqual(app.screen.selections, {})
                await pilot.press("ctrl+c")
                self.assertEqual(app.clipboard, draft)
                await pilot.press("right")
                chat = app.query_one("#chat", ChatLog)
                app.screen.selections = {chat: Selection(Offset(0, 1), Offset(2, 1))}
                await pilot.press("ctrl+a")
                self.assertEqual(field.selected_text, draft)
                self.assertEqual(app.screen.selections, {})
                await pilot.press("x")
                self.assertEqual(field.text, "x")
                self.assertEqual(conversation.messages[0]["content"], "聊天记录不应被选中")

    async def test_left_double_click_selects_only_clicked_display_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            conversation.messages = [{"role": "user", "content": "第一行\n中文第二行 0.3\n第三行"}]
            app = DianStarApp(conversation)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                chat = app.query_one("#chat", ChatLog)
                await pilot.double_click(chat, offset=(4, 3))
                self.assertEqual(app.screen.get_selected_text(), "中文第二行 0.3")
                await pilot.press("ctrl+c")
                self.assertEqual(app.clipboard, "中文第二行 0.3")
                await pilot.triple_click(chat, offset=(4, 3))
                self.assertEqual(app.screen.get_selected_text(), "中文第二行 0.3")

    async def test_double_click_selects_correct_row_after_scroll(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            conversation.messages = [{"role": "user", "content": "\n".join(
                f"第{index}行 中文" for index in range(60))}]
            app = DianStarApp(conversation)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                chat = app.query_one("#chat", ChatLog)
                chat.scroll_to(y=20, animate=False, immediate=True)
                await pilot.pause()
                row = chat.scroll_offset.y + 1
                await pilot.double_click(chat, offset=(4, 2))
                self.assertEqual(app.screen.get_selected_text(), chat.lines[row].text.rstrip())

    async def test_right_double_and_triple_click_never_expand_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            conversation.messages = [{"role": "user", "content": "只复制这几个字，其他不要"}]
            app = DianStarApp(conversation)
            async with app.run_test() as pilot:
                await pilot.pause()
                chat = app.query_one("#chat", ChatLog)
                original = {chat: Selection(Offset(0, 1), Offset(4, 1))}
                app.screen.selections = original.copy()
                for chain in (2, 3):
                    app.screen._forward_event(Click(
                        chat, chat.region.x + 3, chat.region.y + 2, 0, 0, 3,
                        False, False, False, screen_x=chat.region.x + 3,
                        screen_y=chat.region.y + 2, chain=chain))
                    await pilot.pause()
                    self.assertEqual(app.screen.selections, original)
                    self.assertEqual(app.screen.get_selected_text(), "只复制这")
                app.screen.clear_selection()
                app.screen._forward_event(Click(chat, 3, 2, 0, 0, 3, False, False, False, chain=2))
                await pilot.pause()
                self.assertEqual(app.screen.selections, {})

    async def test_left_click_clears_old_selection_and_new_drag_still_selects(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            conversation.messages = [{"role": "user", "content": "中文答案 0.3"}]
            app = DianStarApp(conversation)
            async with app.run_test() as pilot:
                await pilot.pause()
                chat = app.query_one("#chat", ChatLog)
                app.screen.selections = {chat: Selection(Offset(0, 1), Offset(4, 1))}
                await pilot.click("#input")
                self.assertEqual(app.screen.selections, {})
                await pilot.mouse_down(chat, offset=(2, 2))
                await pilot.hover(chat, offset=(8, 2))
                await pilot.mouse_up(chat, offset=(8, 2))
                self.assertEqual(app.screen.get_selected_text(), "中文答案")
                await pilot.click(chat, offset=(30, 5))
                self.assertEqual(app.screen.selections, {})

    async def test_mouse_selection_copies_chinese_and_markdown_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            conversation.messages = [{"role": "user", "content": "中文答案 0.3\n第二行"}]
            app = DianStarApp(conversation)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                chat = app.query_one("#chat", ChatLog)
                await pilot.mouse_down(chat, offset=(2, 2))
                await pilot.hover(chat, offset=(8, 2))
                await pilot.mouse_up(chat, offset=(8, 2))
                await pilot.pause()
                self.assertEqual(app.screen.get_selected_text(), "中文答案")
                highlighted = [segment for segment in chat.render_line(1)
                               if segment.style and segment.style.bgcolor
                               and segment.style.bgcolor.name == "#245b85"]
                self.assertTrue(highlighted)
                self.assertTrue(all(segment.style.color.name == "#ffffff" for segment in highlighted))
                self.assertEqual("".join(segment.text for segment in highlighted), "中文答案")
                await pilot.press("ctrl+c")
                self.assertEqual(app.clipboard, "中文答案")
                original_selection = dict(app.screen.selections)
                original_state = app.screen._select_state
                await pilot.mouse_down(chat, offset=(8, 2), button=3)
                await pilot.mouse_up(chat, offset=(8, 2))
                await pilot.pause()
                menu = app.query_one("#copy-menu", CopyMenu)
                self.assertTrue(menu.display)
                self.assertEqual(app.screen.selections, original_selection)
                self.assertIs(app.screen._select_state, original_state)
                self.assertEqual(len(app.screen_stack), 1)
                self.assertEqual(menu.region.width, 12)
                self.assertEqual(menu.region.height, 3)
                self.assertEqual(menu.region.x, chat.region.x + 8)
                self.assertEqual(menu.region.y, chat.region.y + 3)
                self.assertEqual(str(menu.query_one(Button).label), "复制")
                app.copy_to_clipboard("旧内容")
                await pilot.click("#copy-selection")
                await pilot.pause()
                self.assertFalse(menu.display)
                self.assertEqual(app.clipboard, "中文答案")
                # 拖选聊天记录不能修改仍在输入的草稿。
                self.assertEqual(app.query_one("#input", ChatInput).text, "")
                app.screen.selections = {chat: Selection(Offset(0, 1), Offset(3, 2))}
                await pilot.press("ctrl+c")
                self.assertEqual(app.clipboard, "中文答案 0.3\n第二行")
                chat.clear()
                self.assertIsNone(app.screen.get_selected_text())

    async def test_right_click_without_selection_and_cancel_do_not_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            async with app.run_test() as pilot:
                await pilot.pause()
                app.copy_to_clipboard("原有剪贴板")
                chat = app.query_one("#chat", ChatLog)
                await pilot.mouse_down(chat, offset=(3, 2), button=3)
                await pilot.mouse_up(chat, offset=(3, 2))
                await pilot.pause()
                menu = app.query_one("#copy-menu", CopyMenu)
                self.assertFalse(menu.display)
                with patch.object(app, "_status") as status:
                    app.on_chat_log_copy_requested(ChatLog.CopyRequested(""))
                    status.assert_not_called()
                app.on_chat_log_copy_requested(ChatLog.CopyRequested("待复制文本"))
                await pilot.pause()
                self.assertTrue(menu.display)
                await pilot.press("escape")
                self.assertFalse(menu.display)
                self.assertEqual(app.clipboard, "原有剪贴板")

    async def test_context_menu_stays_in_window_and_outside_click_closes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            conversation.messages = [{"role": "user", "content": "测试菜单位置"}]
            app = DianStarApp(conversation)
            async with app.run_test(size=(60, 20)) as pilot:
                await pilot.pause()
                original_screen = app.screen
                app.copy_to_clipboard("原有剪贴板")
                app.on_chat_log_copy_requested(ChatLog.CopyRequested("待复制", Offset(59, 19)))
                await pilot.pause()
                menu = app.query_one("#copy-menu", CopyMenu)
                self.assertIs(app.screen, original_screen)
                self.assertEqual(menu.region.right, 60)
                self.assertEqual(menu.region.bottom, 20)
                self.assertEqual(menu.styles.background, original_screen.styles.background)
                await pilot.click("#input")
                self.assertFalse(menu.display)
                self.assertIs(app.focused, app.query_one("#input", ChatInput))
                self.assertEqual(app.clipboard, "原有剪贴板")

    async def test_selection_tracks_scrolled_rows_and_preserves_markdown_style(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            conversation.messages = [{"role": "assistant", "content": "\n\n".join(
                f"**第{index}行** 结果" for index in range(50))}]
            app = DianStarApp(conversation)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                chat = app.query_one("#chat", ChatLog)
                target = next(index for index, line in enumerate(chat.lines)
                              if index >= 20 and any(segment.style and segment.style.bold for segment in line))
                chat.scroll_to(y=target, animate=False, immediate=True)
                await pilot.pause()
                self.assertGreater(chat.scroll_offset.y, 0)
                row = chat.scroll_offset.y
                app.screen.selections = {chat: Selection(Offset(0, row), Offset(2, row))}
                line = chat.render_line(0)
                self.assertTrue(any(segment.style and segment.style.bold for segment in line))
                self.assertTrue(any(segment.style and segment.style.meta.get("offset", (0, -1))[1] == row
                                    for segment in line))
                await pilot.press("ctrl+c")
                self.assertEqual(app.clipboard, chat.lines[row].text[:2])

    async def test_multiline_paste_is_not_sent_until_enter_and_is_saved_intact(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            app = DianStarApp(conversation)
            message = "第一行\n\n  保留缩进的第二行\n<document>外部材料</document>\n"
            with patch("dianstar.tui.ask_deepseek", new_callable=AsyncMock,
                       return_value="已收到完整内容。") as ask:
                async with app.run_test() as pilot:
                    field = app.query_one("#input", ChatInput)
                    app.post_message(Paste(message))
                    await pilot.pause()
                    self.assertEqual(field.text, message)
                    self.assertGreater(field.size.height, 1)
                    ask.assert_not_awaited()
                    await pilot.press("enter")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertEqual(ask.await_args.args, (message,))
                    self.assertEqual(field.text, "")
                    self.assertEqual(field.size.height, 1)
            restored = Conversation.load(conversation.id, Path(temporary))
            self.assertEqual(restored.messages[0]["content"], message)

    async def test_newline_keys_edit_without_submitting(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            with patch("dianstar.tui.ask_deepseek", new_callable=AsyncMock) as ask:
                async with app.run_test() as pilot:
                    await pilot.press("a", "shift+enter", "b", "ctrl+j", "c")
                    self.assertEqual(app.query_one("#input", ChatInput).text, "a\nb\nc")
                    ask.assert_not_awaited()

    async def test_clipboard_paste_keeps_multiple_lines_without_sending(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            with patch("dianstar.tui.ask_deepseek", new_callable=AsyncMock) as ask:
                async with app.run_test() as pilot:
                    app.copy_to_clipboard("第一段\n第二段")
                    await pilot.press("ctrl+v")
                    self.assertEqual(app.query_one("#input", ChatInput).text, "第一段\n第二段")
                    ask.assert_not_awaited()

    async def test_submit_while_busy_keeps_the_multiline_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            with patch("dianstar.tui.ask_deepseek", new_callable=AsyncMock) as ask:
                async with app.run_test() as pilot:
                    app.busy = True
                    app.post_message(Paste("下一条问题\n补充说明"))
                    await pilot.pause()
                    await pilot.press("enter")
                    self.assertEqual(app.query_one("#input", ChatInput).text, "下一条问题\n补充说明")
                    ask.assert_not_awaited()

    async def test_multiline_slash_text_is_a_message_not_a_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            app = DianStarApp(conversation)
            message = "/new\n这是要讨论的文本"
            with patch("dianstar.tui.ask_deepseek", new_callable=AsyncMock,
                       return_value="已分析文本。") as ask:
                async with app.run_test() as pilot:
                    field = app.query_one("#input", ChatInput)
                    app.post_message(Paste(message))
                    await pilot.pause()
                    await pilot.press("tab")
                    self.assertEqual(field.text, message)
                    await pilot.press("enter")
                    await app.workers.wait_for_complete()
                    self.assertEqual(app.conversation.id, conversation.id)
                    self.assertEqual(ask.await_args.args, (message,))

    async def test_long_paste_keeps_chat_visible_and_can_be_edited(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            async with app.run_test(size=(80, 24)) as pilot:
                field = app.query_one("#input", ChatInput)
                app.post_message(Paste("\n".join(f"第{i}行" for i in range(30))))
                await pilot.pause()
                self.assertLessEqual(field.size.height, 6)
                self.assertGreater(app.query_one("#chat", RichLog).size.height, 0)
                await pilot.press("backspace", "x")
                self.assertTrue(field.text.endswith("第29x"))

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

            async def answer_later(question, *, history, on_stream, on_thinking, **kwargs):
                on_thinking("第一步\n第二步\n第三步\n第四步\n第五步\n再组织回答。")
                on_stream("第一段")
                await release.wait()
                on_stream("第一段第二段")
                on_thinking("")
                return "第一段第二段"

            with patch("dianstar.tui.ask_deepseek", side_effect=answer_later):
                async with app.run_test() as pilot:
                    app.query_one("#input", ChatInput).text = "测试流式输出"
                    await pilot.press("enter")
                    await pilot.pause()
                    chat = app.query_one("#chat", RichLog)
                    self.assertIsNotNone(app._active_stream)
                    self.assertFalse(app.query("#stream"))
                    self.assertEqual(len(app.query("#thinking")), 0)
                    self.assertIn("模型思考", "\n".join(line.text for line in chat.lines))
                    self.assertIn("再组织回答", "\n".join(line.text for line in chat.lines))
                    rendered = "\n".join(line.text for line in chat.lines)
                    self.assertIn("第一段", rendered)
                    release.set()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertIsNone(app._active_stream)
                    displayed = "\n".join(line.text for line in chat.lines)
                    self.assertLess(displayed.index("测试流式输出"), displayed.index("模型思考"))
                    self.assertLess(displayed.index("模型思考"), displayed.index("第一段第二段"))
                    self.assertIn("再组织回答", displayed)
                    self.assertNotIn("第一步", displayed)
                    index = next(i for i, b in enumerate(app._chat_blocks) if b["kind"] == "thinking")
                    title_position = None
                    for y in range(chat.content_region.height):
                        x = 0
                        for segment in chat.render_line(y):
                            if segment.style and segment.style.meta.get('@click') == f'app.toggle_thinking({index})':
                                title_position = (chat.content_region.x-chat.region.x+x,
                                                  chat.content_region.y-chat.region.y+y)
                                break
                            x += segment.cell_length
                        if title_position is not None:
                            break
                    self.assertIsNotNone(title_position)
                    await pilot.click(chat, offset=title_position)
                    self.assertIn(index, app._expanded_thinking)
                    self.assertIn("第一步", "\n".join(line.text for line in chat.lines))
                    app.action_toggle_thinking(index)
                    await pilot.pause()
                    self.assertNotIn("第一步", "\n".join(line.text for line in chat.lines))
                    restored = Conversation.load(app.conversation.id, Path(temporary))
                    self.assertIn("第一步", restored.messages[-1]["reasoning_content"])
                    self.assertIn("第一段第二段", "\n".join(
                        line.text for line in app.query_one("#chat", RichLog).lines))

    async def test_each_turn_keeps_own_thinking_and_history_restores_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            turns = 0

            async def answer(question, *, history, on_stream, on_thinking, **kwargs):
                nonlocal turns
                turns += 1
                on_thinking(f"第{turns}轮思考")
                on_thinking("")
                on_thinking(f"第{turns}轮工具后思考")
                return f"第{turns}轮回答"

            with patch("dianstar.tui.ask_deepseek", side_effect=answer):
                async with app.run_test() as pilot:
                    for question in ("第一问", "第二问"):
                        app.query_one("#input", ChatInput).text = question
                        await pilot.press("enter")
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                    thoughts = [b["content"] for b in app._chat_blocks if b["kind"] == "thinking"]
                    self.assertEqual(thoughts, ["第1轮思考\n\n第1轮工具后思考", "第2轮思考\n\n第2轮工具后思考"])
                    conversation_id = app.conversation.id
                    await app._command("/new")
                    self.assertFalse(any(b["kind"] == "thinking" for b in app._chat_blocks))
                    await app._open_conversation(conversation_id)
                    self.assertEqual([b["content"] for b in app._chat_blocks if b["kind"] == "thinking"], thoughts)
                    self.assertEqual(len(app._chat_blocks), 6)

    async def test_legacy_model_reasoning_restores_by_turn_not_repeated_question(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            for index in (1, 2):
                conversation.add_exchange("相同的问题", f"回答{index}", model_turn_messages=[
                    {"role": "user", "content": "相同的问题"},
                    {"role": "assistant", "content": None, "reasoning_content": f"查询思考{index}"},
                    {"role": "assistant", "content": f"回答{index}", "reasoning_content": f"回答思考{index}"},
                ])
            app = DianStarApp(Conversation.load(conversation.id, Path(temporary)))
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual([b["content"] for b in app._chat_blocks if b["kind"] == "thinking"],
                                 ["查询思考1\n\n回答思考1", "查询思考2\n\n回答思考2"])

    async def test_failed_turn_keeps_inline_thinking_without_fabricating_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            app = DianStarApp(conversation)
            async def failed(question, *, history, on_stream, on_thinking, **kwargs):
                on_thinking("已收到的部分思考")
                raise ValueError("测试失败")
            with patch("dianstar.tui.ask_deepseek", side_effect=failed):
                async with app.run_test() as pilot:
                    app.query_one("#input", ChatInput).text = "失败问题"
                    await pilot.press("enter")
                    await app.workers.wait_for_complete()
                    self.assertEqual([b["kind"] for b in app._chat_blocks], ["message", "thinking"])
                    self.assertEqual(conversation.messages, [])

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
                app.query_one("#input", ChatInput).text = "/switch"
                await pilot.press("enter")
                self.assertTrue(sidebar.display)

    async def test_tab_completes_command_and_help_lists_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            async with app.run_test() as pilot:
                await pilot.press("/", "s", "w", "tab")
                self.assertEqual(app.query_one("#input", ChatInput).text, "/switch")
                app.query_one("#input", ChatInput).text = "/help"
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
                app.query_one("#input", ChatInput).text = "/switch"
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
