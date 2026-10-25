"""长回答完成前可见、滚动与网页链接点击；浏览器调用全部模拟。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from textual.events import Click
from textual.geometry import Offset
from textual.selection import Selection

from dianstar.chat_rendering import ChatMarkdown, StreamLines
from dianstar.conversations import Conversation
from dianstar.tui import ChatInput, ChatLog, DianStarApp


class StreamLinkTests(unittest.IsolatedAsyncioTestCase):
    def test_incremental_rows_handle_wide_combining_tabs_and_newlines(self):
        stream = StreamLines(5)
        self.assertEqual(stream.append('中文e\u0301'), [])
        self.assertEqual(stream.tail, '中文e\u0301')
        self.assertEqual(stream.append('x\r\n\n'), ['中文e\u0301', 'x', ''])
        self.assertEqual(stream.tail, '')
        tabs = StreamLines(4)
        self.assertEqual(tabs.append('a\tb'), ['a   ', '    '])
        self.assertEqual(tabs.tail, 'b')

    async def test_long_single_paragraph_reuses_rows_and_renders_only_one_pending_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.app(Path(temporary))
            async with app.run_test(size=(90,25)) as pilot:
                app._chat_blocks=[]
                app._welcome_only=False
                chat=app.query_one('#chat',ChatLog)
                chat.clear()
                original='连续长段落abc '*4000
                app._stream_answer(original)
                app._paint_stream()
                stable=chat.lines[app._stream_start+1]
                with patch.object(chat,'write',wraps=chat.write) as write, \
                        patch.object(app,'_status',wraps=app._status) as status:
                    app._stream_answer(original+'新增')
                    app._paint_stream()
                    rendered=sum(len(call.args[0].plain) for call in write.call_args_list)
                    self.assertLessEqual(rendered, chat.scrollable_content_region.width*2)
                    self.assertIs(chat.lines[app._stream_start+1],stable)
                    self.assertLessEqual(len(app._stream_lines.tail),chat.scrollable_content_region.width)
                    status.assert_not_called()
                self.assertIn('新增', ''.join(line.text for line in chat.lines[-2:]))

    async def test_stream_rebuilds_on_rewrite_or_resize_without_losing_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            app=self.app(Path(temporary))
            async with app.run_test(size=(90,25)) as pilot:
                app._chat_blocks=[]
                app._welcome_only=False
                chat=app.query_one('#chat',ChatLog)
                chat.clear()
                app._stream_answer('旧段落'*100)
                app._paint_stream()
                app._stream_answer('改写后的正文')
                app._paint_stream()
                self.assertNotIn('旧段落',''.join(line.text for line in chat.lines))
                await pilot.resize_terminal(65,25)
                await pilot.pause()
                app._stream_answer('改写后的正文继续输出')
                app._paint_stream()
                self.assertEqual(app._stream_lines.width,chat.scrollable_content_region.width)
                self.assertIn('改写后的正文继续输出',''.join(line.text for line in chat.lines))

    def app(self, root):
        return DianStarApp(Conversation.create(root/'conversations'),
                          config_file=root/'.local/config.json', env_file=root/'.env')

    def link_position(self, chat, url):
        for y in range(chat.content_region.height):
            x = 0
            for segment in chat.render_line(y):
                if segment.style and segment.style.link == url:
                    return (chat.content_region.x-chat.region.x+x,
                            chat.content_region.y-chat.region.y+y)
                x += segment.cell_length
        self.fail(f'找不到可见链接 {url}')

    def test_markdown_bare_and_inline_code_urls_keep_links_but_code_blocks_do_not(self):
        text = ('[官方教程](https://docs.python.org/3/tutorial/index.html#intro)\n\n'
                'https://example.com/news\n\n`https://arxiv.org/abs/2402.06363`\n\n'
                '```python\nurl="https://example.com/example"\n```')
        segments = list(Console(width=55).render(ChatMarkdown(text)))
        links = {s.style.link for s in segments if s.style and s.style.link}
        self.assertEqual(links, {'https://docs.python.org/3/tutorial/index.html#intro',
                                'https://example.com/news', 'https://arxiv.org/abs/2402.06363'})

    async def test_click_markdown_bare_and_evidence_inline_urls_opens_default_browser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.app(root)
            urls = ['https://docs.python.org/3/tutorial/index.html#intro',
                    'https://example.com/news', 'https://arxiv.org/abs/2402.06363']
            app.conversation.add_exchange('链接', f'[官方教程]({urls[0]})\n\n{urls[1]}\n\n来源核验（`{urls[2]}`）：已读摘要页。')
            with patch('dianstar.tui.webbrowser.open', return_value=True) as browser:
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    chat = app.query_one('#chat', ChatLog)
                    for url in urls:
                        await pilot.click(chat, offset=self.link_position(chat, url))
                        await app.workers.wait_for_complete()
                        await pilot.pause(0.3)
                    self.assertEqual([call.args[0] for call in browser.call_args_list], urls)
                    self.assertTrue(all(call.kwargs == {'new':2} for call in browser.call_args_list))

    async def test_selection_and_right_click_do_not_open_browser(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.app(Path(temporary))
            url = 'https://example.com/article'
            app.conversation.add_exchange('链接', f'[这段文字可以选择]({url})')
            with patch('dianstar.tui.webbrowser.open', return_value=True) as browser:
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    chat = app.query_one('#chat', ChatLog)
                    x, y = self.link_position(chat, url)
                    row = int(chat.scroll_y) + y - (chat.content_region.y-chat.region.y)
                    app.screen.selections = {chat:Selection(Offset(0,row),Offset(4,row))}
                    # 拖选结束后即使出现 Click，也不能把选中的链接当作激活。
                    app.screen._forward_event(Click(chat,x,y,0,0,1,False,False,False,
                        screen_x=chat.region.x+x,screen_y=chat.region.y+y))
                    await pilot.pause()
                    await pilot.click(chat,offset=(x,y),button=3)
                    await pilot.pause()
                    browser.assert_not_called()
                    chat.post_message(ChatLog.LinkRequested('file:///C:/Windows/system.ini'))
                    chat.post_message(ChatLog.LinkRequested('javascript:alert(1)'))
                    await pilot.pause()
                    browser.assert_not_called()

    async def test_long_stream_follows_before_finish_and_does_not_jump_when_reading_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.app(root)
            continue_stream, finish = asyncio.Event(), asyncio.Event()
            first = '\n'.join(f'段落{i:03d}：内容持续生成' for i in range(100))
            second = first + '\n继续生成的段落\n最后一段还在生成'
            async def answer(question, *, on_stream, on_thinking, **kwargs):
                on_thinking('整理资料')
                on_stream(first)
                await continue_stream.wait()
                on_stream(second)
                await finish.wait()
                return second
            with patch('dianstar.tui.ask_deepseek', side_effect=answer):
                async with app.run_test(size=(90, 25)) as pilot:
                    app.query_one('#input', ChatInput).text = '写一篇长回答'
                    await pilot.press('enter')
                    await pilot.pause(0.2)
                    chat = app.query_one('#chat', ChatLog)
                    self.assertTrue(app.busy)
                    self.assertIn('段落099', '\n'.join(line.text for line in chat.lines))
                    self.assertTrue(chat.is_vertical_scroll_end)
                    self.assertIn('段落099', '\n'.join(chat.render_line(y).text for y in range(chat.content_region.height)))
                    chat.scroll_to(y=8, animate=False, immediate=True)
                    await pilot.pause()
                    before = chat.scroll_y
                    continue_stream.set()
                    await pilot.pause(0.2)
                    self.assertIn('最后一段还在生成', '\n'.join(line.text for line in chat.lines))
                    self.assertEqual(chat.scroll_y, before)
                    finish.set()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    self.assertEqual(chat.scroll_y, before)
                    self.assertIsNone(app._active_stream)
                    self.assertEqual(sum(b.get('role')=='assistant' for b in app._chat_blocks),1)
                    restored = Conversation.load(app.conversation.id,root/'conversations')
                    self.assertEqual(restored.messages[-1]['content'],second)

    async def test_burst_stream_throttles_render_and_keeps_completed_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.app(Path(temporary))
            async with app.run_test(size=(90,25)) as pilot:
                app._chat_blocks = []
                chat = app.query_one('#chat',ChatLog)
                chat.clear()
                app._welcome_only = False
                with patch('dianstar.tui.ChatMarkdown', wraps=ChatMarkdown) as markdown, \
                        patch.object(app,'_paint_stream',wraps=app._paint_stream) as paint:
                    content = ''
                    for i in range(200):
                        content += f'流式第{i}行\n'
                        app._stream_answer(content)
                    await pilot.pause(0.2)
                    self.assertLess(paint.call_count,10)
                    markdown.assert_not_called()
                    first = chat.lines[app._stream_start+1]
                    app._stream_answer(content+'追加行')
                    await pilot.pause(0.2)
                    self.assertIs(chat.lines[app._stream_start+1],first)
                    app._finish_answer(content+'追加行')
                    self.assertEqual(markdown.call_count,1)
                self.assertEqual('\n'.join(line.text for line in chat.lines).count('流式第199行'),1)

    async def test_clearing_tool_provisional_text_cancels_timer_and_does_not_duplicate_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.app(Path(temporary))
            async with app.run_test() as pilot:
                chat = app.query_one('#chat',ChatLog)
                chat.clear()
                app._chat_blocks=[]
                app._welcome_only=False
                app._stream_answer('临时说明')
                app._stream_answer('临时说明，准备调用工具')
                app._stream_answer('')
                await pilot.pause(0.2)
                self.assertNotIn('临时说明','\n'.join(line.text for line in chat.lines))
                app._stream_answer('最终回答')
                app._finish_answer('最终回答')
                app._stream_answer('')
                self.assertEqual('\n'.join(line.text for line in chat.lines).count('最终回答'),1)
